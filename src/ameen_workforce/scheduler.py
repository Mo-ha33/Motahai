"""
scheduler.py — Recurring background runner & job scheduler for Motahai Core (S1-5).

Handles periodic maintenance and conversion pipeline duties:
  1. send_due_events(): Dispatches settled DeliveredPurchase events (due after settlement window,
     safely before the placed + 6.5-day cutoff).
  2. retry_failed_events(): Retries failed CAPI requests with backoff / orphaned pending claims.
  3. purge_expired_checkout_context(): Purges Fernet-encrypted IP/UA older than 14 days (D-006).
  4. merge_pending_captures(): Merges pending storefront captures into their orders (lazy import from
     `capture`; the job is recorded as skipped when that module is not available).
  5. Heartbeat / health check tracking into the `job_runs` table so operators know the scheduler is alive.

Execution modes:
  - Standalone daemon (production): `python -m ameen_workforce.scheduler` runs run_daemon(), one
    run_scheduler_tick() every 15 minutes, until SIGTERM/SIGINT. Run it as ONE process (see
    deployment/systemd/motahai-scheduler.service).
  - `python -m ameen_workforce.scheduler --check`: exits non-zero when the last successful send cycle is older
    than 45 minutes (for monitoring).
  - run_scheduler_tick(): Single tick executing all scheduled jobs, recording JobRun rows.
  - SchedulerRunner: Async loop running ticks every `interval_seconds` inside the web app. Only used when
    MOTAHAI_RUN_SCHEDULER_IN_APP=1 (see service.py); with several uvicorn workers each worker would run its own loop.
"""

import argparse
import asyncio
import inspect
import json
import logging
import signal
import sys
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .capi_service import capi_sender
from .checkout_context import purge_expired_checkout_context
from .digest import send_weekly_digests, sender_from_env
from .db import JobRun, get_session_factory, init_db, session_scope, utcnow
from .order_pipeline import retry_failed_events, send_due_events

logger = logging.getLogger("ameen_workforce.scheduler")

DEFAULT_SCHEDULER_INTERVAL_SECONDS = 15 * 60  # 15 minutes: keeps DeliveredPurchase sends well inside the 1h margin before the 6.5-day cutoff
DEFAULT_DUE_EVENTS_LIMIT = 500
HEARTBEAT_MAX_AGE = timedelta(minutes=45)  # three missed 15-minute cycles
SEND_JOB_NAME = "send_due_events"
MERGE_JOB_NAME = "merge_pending_captures"
DIGEST_JOB_NAME = "send_weekly_digests"
HEARTBEAT_JOB_NAME = "scheduler_heartbeat"


class SchedulerStale(RuntimeError):
    """The scheduler has not completed a successful send cycle within HEARTBEAT_MAX_AGE."""


def record_job_run(
    session: Session,
    job_name: str,
    started_at: datetime,
    finished_at: datetime,
    ok: bool,
    error_type: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> JobRun:
    """Inserts a JobRun audit / heartbeat record into the database and commits."""
    detail_str = json.dumps(detail, default=str) if detail is not None else None
    run = JobRun(
        job_name=job_name,
        started_at=started_at,
        finished_at=finished_at,
        ok=ok,
        error_type=error_type,
        detail=detail_str,
    )
    session.add(run)
    session.commit()
    return run


def _load_merge_pending_captures() -> Optional[Callable[..., Any]]:
    """The capture module is written separately (S2-8); a missing module means the merge job is skipped."""
    try:
        from .capture import merge_pending_captures
    except ImportError:
        return None
    return merge_pending_captures


async def _run_job(
    factory: sessionmaker,
    job_name: str,
    action: Callable[[Session, datetime], Any],
    now: Optional[datetime],
) -> Dict[str, Any]:
    """
    Runs one job in its own session and records a JobRun row. `action(session, job_start)` may return a
    dict of counts or an awaitable of one. Never raises: a failure is recorded as ok=False and returned.
    """
    job_start = now or utcnow()
    try:
        with session_scope(factory) as session:
            counts = action(session, job_start)
            if inspect.isawaitable(counts):
                counts = await counts
    except Exception as exc:
        err_name = type(exc).__name__
        logger.exception("Scheduler job %s failed: %s", job_name, err_name)
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, now or utcnow(), ok=False, error_type=err_name, detail={"error": str(exc)})
        return {"ok": False, "error_type": err_name}

    with session_scope(factory) as session:
        record_job_run(session, job_name, job_start, now or utcnow(), ok=True, detail=counts)
    return {"ok": True, "counts": counts}


async def run_scheduler_tick(
    session_factory: Optional[sessionmaker] = None,
    now: Optional[datetime] = None,
    sender=capi_sender,
    due_limit: int = DEFAULT_DUE_EVENTS_LIMIT,
    digest_sender=None,
) -> Dict[str, Any]:
    """
    Executes one full iteration of the background jobs:
      1. send_due_events (dispatches settled conversions)
      2. retry_failed_events (retries failed CAPI requests)
      3. purge_expired_checkout_context (purges encrypted IP/UA older than 14 days)
      4. merge_pending_captures (skipped, and recorded as such, when the capture module is unavailable)
      5. send_weekly_digests (Sunday Signal Hygiene email; a cheap no-op except on Sunday >= 10:00 tenant-local;
         `digest_sender` defaults to SMTP from the environment, and the job is recorded as skipped when SMTP is unset)
      6. scheduler_heartbeat (system health check)

    Each job records its start/finish times, success status, and details in `job_runs`.
    If an individual job raises an exception, it is caught, recorded as ok=False in `job_runs`,
    and subsequent jobs continue to execute.

    Returns a summary dictionary of all executed job results.
    """
    factory = session_factory or get_session_factory()
    tick_start = now or utcnow()
    summary: Dict[str, Any] = {
        "tick_started_at": tick_start.isoformat(),
        "jobs": {},
    }

    summary["jobs"][SEND_JOB_NAME] = await _run_job(
        factory, SEND_JOB_NAME,
        lambda s, t: send_due_events(s, now=t, sender=sender, limit=due_limit),
        now,
    )
    summary["jobs"]["retry_failed_events"] = await _run_job(
        factory, "retry_failed_events",
        lambda s, t: retry_failed_events(s, sender=sender, now=t),
        now,
    )
    summary["jobs"]["purge_expired_checkout_context"] = await _run_job(
        factory, "purge_expired_checkout_context",
        lambda s, t: _purge_counts(s, t),
        now,
    )

    merge_fn = _load_merge_pending_captures()
    if merge_fn is None:
        logger.info("Scheduler job %s skipped: capture module is not available", MERGE_JOB_NAME)
        skip_at = now or utcnow()
        with session_scope(factory) as session:
            record_job_run(session, MERGE_JOB_NAME, skip_at, skip_at, ok=True, detail={"skipped": "capture_module_unavailable"})
        summary["jobs"][MERGE_JOB_NAME] = {"ok": True, "skipped": True}
    else:
        summary["jobs"][MERGE_JOB_NAME] = await _run_job(
            factory, MERGE_JOB_NAME,
            lambda s, t: merge_fn(s, now=t),
            now,
        )

    summary["jobs"][DIGEST_JOB_NAME] = await _run_job(
        factory, DIGEST_JOB_NAME,
        lambda s, t: _digest_counts(s, t, digest_sender),
        now,
    )

    # Heartbeat / health check (records the cycle even when a job above failed; all_jobs_succeeded says which)
    heartbeat_start = now or utcnow()
    try:
        heartbeat_detail = {
            "status": "alive",
            "active_jobs_count": len(summary["jobs"]),
            "all_jobs_succeeded": all(j.get("ok") for j in summary["jobs"].values()),
        }
        with session_scope(factory) as session:
            record_job_run(session, HEARTBEAT_JOB_NAME, heartbeat_start, now or utcnow(), ok=True, detail=heartbeat_detail)
        summary["jobs"][HEARTBEAT_JOB_NAME] = {"ok": True, "detail": heartbeat_detail}
    except Exception as exc:
        err_name = type(exc).__name__
        logger.exception("Scheduler job %s failed: %s", HEARTBEAT_JOB_NAME, err_name)
        summary["jobs"][HEARTBEAT_JOB_NAME] = {"ok": False, "error_type": err_name}

    summary["tick_finished_at"] = (now or utcnow()).isoformat()
    return summary


def _purge_counts(session: Session, now: datetime) -> Dict[str, int]:
    return {"purged_count": purge_expired_checkout_context(session, now=now)}


def _digest_counts(session: Session, now: datetime, digest_sender) -> Dict[str, Any]:
    sender = digest_sender or sender_from_env()
    if sender is None:
        return {"skipped": "digest_sender_not_configured"}
    return send_weekly_digests(session, now, sender)


def check_scheduler_health(
    session_factory: Optional[sessionmaker] = None,
    now: Optional[datetime] = None,
    max_age: timedelta = HEARTBEAT_MAX_AGE,
) -> Dict[str, Any]:
    """
    Heartbeat check: the most recent successful `send_due_events` JobRun must have finished within `max_age`.
    Returns {"last_success_at", "age_seconds"} when healthy; raises SchedulerStale otherwise.
    """
    factory = session_factory or get_session_factory()
    current = now or utcnow()
    with session_scope(factory) as session:
        last = session.scalar(
            select(JobRun.finished_at)
            .where(JobRun.job_name == SEND_JOB_NAME, JobRun.ok.is_(True))
            .order_by(JobRun.id.desc())
            .limit(1)
        )
    if last is None:
        raise SchedulerStale("no successful send cycle has been recorded")
    age = current - last
    if age > max_age:
        raise SchedulerStale(
            f"last successful send cycle was {int(age.total_seconds())}s ago (limit {int(max_age.total_seconds())}s)"
        )
    return {"last_success_at": last.isoformat(), "age_seconds": age.total_seconds()}


async def _wait_or_stop(seconds: float, stop: asyncio.Event) -> None:
    """Sleeps for `seconds`, returning early when `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_daemon(
    interval_seconds: float = DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    cycle: Optional[Callable[[], Awaitable[Any]]] = None,
    stop_event: Optional[asyncio.Event] = None,
    max_cycles: Optional[int] = None,
    wait: Optional[Callable[[float, asyncio.Event], Awaitable[None]]] = None,
    sender=capi_sender,
) -> int:
    """
    Standalone loop: runs `cycle` (default: one run_scheduler_tick) immediately, then again every
    `interval_seconds`, until `stop_event` is set or `max_cycles` cycles have run. A cycle that raises is logged
    and the loop continues. Returns the number of cycles run. `cycle` and `wait` are injectable for tests.
    """
    stop = stop_event or asyncio.Event()
    run_cycle = cycle or (lambda: run_scheduler_tick(sender=sender))
    wait_fn = wait or _wait_or_stop
    cycles = 0
    while not stop.is_set():
        try:
            await run_cycle()
        except Exception as exc:
            logger.exception("Scheduler cycle failed: %s", type(exc).__name__)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        await wait_fn(interval_seconds, stop)
    return cycles


def _install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT request a clean stop: the running cycle finishes, then the loop exits."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows (no loop signal handlers) or not the main thread
            signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(stop.set))


def main(argv: Optional[Sequence[str]] = None, session_factory: Optional[sessionmaker] = None) -> int:
    parser = argparse.ArgumentParser(description="Motahai conversion scheduler daemon (run one instance only).")
    parser.add_argument("--check", action="store_true",
                        help="Heartbeat check for monitoring: exit 0 if the last send cycle is fresh, 1 if stale.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [MotahaiScheduler] %(message)s")

    if args.check:
        try:
            info = check_scheduler_health(session_factory=session_factory)
        except SchedulerStale as exc:
            print(f"UNHEALTHY: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"UNHEALTHY: health check could not run ({type(exc).__name__})", file=sys.stderr)
            return 1
        print(f"OK: last successful send cycle {info['last_success_at']} ({int(info['age_seconds'])}s ago)")
        return 0

    try:
        init_db()
    except Exception as exc:
        logger.error("Database initialisation failed: %s", type(exc).__name__)
        return 1

    async def _serve() -> int:
        stop = asyncio.Event()
        _install_stop_handlers(asyncio.get_running_loop(), stop)
        logger.info("Scheduler daemon started (interval=%ss)", DEFAULT_SCHEDULER_INTERVAL_SECONDS)
        cycles = await run_daemon(stop_event=stop)
        logger.info("Scheduler daemon stopped after %d cycle(s)", cycles)
        return 0

    return asyncio.run(_serve())


class SchedulerRunner:
    """
    Async background runner managing periodic executions of run_scheduler_tick().
    Can be started and stopped cleanly as part of an async application lifespan.
    """

    def __init__(
        self,
        session_factory: Optional[sessionmaker] = None,
        interval_seconds: float = DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        sender=capi_sender,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.session_factory = session_factory
        self.interval_seconds = interval_seconds
        self.sender = sender
        self.now_fn = now_fn
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Starts the background worker task if not already running."""
        if self.is_running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("SchedulerRunner started with interval=%ss", self.interval_seconds)

    async def stop(self) -> None:
        """Gracefully stops the background worker task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("SchedulerRunner stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                current_time = self.now_fn() if self.now_fn else None
                await run_scheduler_tick(
                    session_factory=self.session_factory,
                    now=current_time,
                    sender=self.sender,
                )
            except Exception as exc:
                logger.exception("Error in scheduler tick execution: %s", type(exc).__name__)

            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break


# Module-level default runner instance (used only when MOTAHAI_RUN_SCHEDULER_IN_APP=1)
scheduler_runner = SchedulerRunner()


if __name__ == "__main__":
    sys.exit(main())
