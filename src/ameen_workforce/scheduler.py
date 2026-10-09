"""
scheduler.py — Recurring background runner & job scheduler for Motahai Core (S1-5).

Handles periodic maintenance and conversion pipeline duties:
  1. send_due_events(): Dispatches settled DeliveredPurchase events (due after settlement window,
     safely before the placed + 6.5-day cutoff).
  2. retry_failed_events(): Retries failed CAPI requests with backoff / orphaned pending claims.
  3. purge_expired_checkout_context(): Purges Fernet-encrypted IP/UA older than 14 days (D-006).
  4. Heartbeat / health check tracking into the `job_runs` table so operators know the scheduler is alive.

Execution modes:
  - run_scheduler_tick(): Single tick executing all scheduled jobs, recording JobRun rows.
  - SchedulerService / Background worker: Async loop running ticks every `interval_seconds` (default 15 minutes = 900s).
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session, sessionmaker

from .capi_service import capi_sender
from .checkout_context import purge_expired_checkout_context
from .db import JobRun, get_session_factory, session_scope, utcnow
from .order_pipeline import retry_failed_events, send_due_events

logger = logging.getLogger("ameen_workforce.scheduler")

DEFAULT_SCHEDULER_INTERVAL_SECONDS = 15 * 60  # 15 minutes
DEFAULT_DUE_EVENTS_LIMIT = 500


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


async def run_scheduler_tick(
    session_factory: Optional[sessionmaker] = None,
    now: Optional[datetime] = None,
    sender=capi_sender,
    due_limit: int = DEFAULT_DUE_EVENTS_LIMIT,
) -> Dict[str, Any]:
    """
    Executes one full iteration of the background jobs:
      1. send_due_events (dispatches settled conversions)
      2. retry_failed_events (retries failed CAPI requests)
      3. purge_expired_checkout_context (purges encrypted IP/UA older than 14 days)
      4. scheduler_heartbeat (system health check)

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

    # Job 1: send_due_events
    job_name = "send_due_events"
    job_start = now or utcnow()
    try:
        with session_scope(factory) as session:
            counts = await send_due_events(session, now=job_start, sender=sender, limit=due_limit)
        job_finish = now or utcnow()
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=True, detail=counts)
        summary["jobs"][job_name] = {"ok": True, "counts": counts}
    except Exception as exc:
        job_finish = now or utcnow()
        err_name = type(exc).__name__
        logger.exception("Scheduler job %s failed: %s", job_name, err_name)
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=False, error_type=err_name, detail={"error": str(exc)})
        summary["jobs"][job_name] = {"ok": False, "error_type": err_name}

    # Job 2: retry_failed_events
    job_name = "retry_failed_events"
    job_start = now or utcnow()
    try:
        with session_scope(factory) as session:
            counts = await retry_failed_events(session, sender=sender, now=job_start)
        job_finish = now or utcnow()
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=True, detail=counts)
        summary["jobs"][job_name] = {"ok": True, "counts": counts}
    except Exception as exc:
        job_finish = now or utcnow()
        err_name = type(exc).__name__
        logger.exception("Scheduler job %s failed: %s", job_name, err_name)
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=False, error_type=err_name, detail={"error": str(exc)})
        summary["jobs"][job_name] = {"ok": False, "error_type": err_name}

    # Job 3: purge_expired_checkout_context
    job_name = "purge_expired_checkout_context"
    job_start = now or utcnow()
    try:
        with session_scope(factory) as session:
            purged_count = purge_expired_checkout_context(session, now=job_start)
        job_finish = now or utcnow()
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=True, detail={"purged_count": purged_count})
        summary["jobs"][job_name] = {"ok": True, "counts": {"purged_count": purged_count}}
    except Exception as exc:
        job_finish = now or utcnow()
        err_name = type(exc).__name__
        logger.exception("Scheduler job %s failed: %s", job_name, err_name)
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=False, error_type=err_name, detail={"error": str(exc)})
        summary["jobs"][job_name] = {"ok": False, "error_type": err_name}

    # Job 4: Heartbeat / Health check
    job_name = "scheduler_heartbeat"
    job_start = now or utcnow()
    try:
        heartbeat_detail = {
            "status": "alive",
            "active_jobs_count": 3,
            "all_jobs_succeeded": all(j.get("ok") for j in summary["jobs"].values()),
        }
        job_finish = now or utcnow()
        with session_scope(factory) as session:
            record_job_run(session, job_name, job_start, job_finish, ok=True, detail=heartbeat_detail)
        summary["jobs"][job_name] = {"ok": True, "detail": heartbeat_detail}
    except Exception as exc:
        job_finish = now or utcnow()
        err_name = type(exc).__name__
        logger.exception("Scheduler job %s failed: %s", job_name, err_name)
        summary["jobs"][job_name] = {"ok": False, "error_type": err_name}

    summary["tick_finished_at"] = (now or utcnow()).isoformat()
    return summary


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


# Module-level default runner instance
scheduler_runner = SchedulerRunner()
