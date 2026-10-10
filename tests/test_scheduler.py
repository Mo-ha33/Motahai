"""
test_scheduler.py — Unit tests for S1-5 recurring background scheduler & job runner.

Verifies:
  1. `run_scheduler_tick()` runs `send_due_events()`, `retry_failed_events()`, and
     `purge_expired_checkout_context()`, logging each run and a heartbeat into `job_runs`.
  2. Scheduled events waiting for settlement window (e.g. 12h) are dispatched when due.
  3. Safe send deadline: orders close to placed + 6.5-day cutoff are sent before cutoff.
  4. Failed events are retried with retry_failed_events().
  5. Expired checkout context (IP/UA > 14 days old) is purged, unexpired context is kept.
  6. Robustness / error isolation: a failure in one job records ok=False with error_type in `job_runs`,
     and other jobs continue running.
  7. SchedulerRunner async lifecycle (start/stop, periodic execution).
  8. merge_pending_captures job: runs when the capture module exists, recorded as skipped when it does not.
  9. Heartbeat check (--check): stale after 45 minutes; daemon loop runs injected cycles without sleeping.
 10. Lifespan: the in-app scheduler is off by default and starts/stops only with MOTAHAI_RUN_SCHEDULER_IN_APP=1.
 11. The module entrypoint `python -m ameen_workforce.scheduler --check` exits 0 when fresh, 1 when stale.
"""

import asyncio
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import scheduler as sched_mod
from src.ameen_workforce import service
from src.ameen_workforce.capi_service import DELIVERED_EVENT, MetaCAPISender
from src.ameen_workforce.checkout_context import capture_checkout_context
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import CapiEvent, CheckoutContext, JobRun, Order, create_tenant, init_db, make_engine
from src.ameen_workforce.order_pipeline import (
    DEFAULT_SETTLEMENT_HOURS, DUE_SAFETY_MARGIN, compute_due_at, process_webhook
)
from src.ameen_workforce.scheduler import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS, SchedulerRunner, SchedulerStale, _wait_or_stop, check_scheduler_health,
    record_job_run, run_daemon, run_scheduler_tick
)
from src.ameen_workforce.scheduler import main as scheduler_main

ROOT = Path(__file__).resolve().parent.parent
MERGE_JOB = "merge_pending_captures"

NOW = datetime.now(timezone.utc)
EMAIL = "customer@example.com"
PHONE = "01012345678"
IP = "197.35.12.99"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class FakeSender(MetaCAPISender):
    """
    Real D-005 logic, fake network. `calls` holds the DeliveredPurchase sends only (what these tests are about);
    ConfirmedOrder sends (the funnel invariant sends one before every DeliveredPurchase) go to `confirmed_calls`
    and always succeed without consuming `results`. `all_calls` has both, in order.
    """
    def __init__(self, results=None):
        super().__init__()
        self.calls = []
        self.confirmed_calls = []
        self.all_calls = []
        self.results = list(results or [])

    async def send_event(self, pixel_id, access_token, payload):
        call = {"pixel_id": pixel_id, "access_token": access_token, "payload": payload}
        self.all_calls.append(call)
        if payload["data"][0]["event_name"] != "DeliveredPurchase":
            self.confirmed_calls.append(call)
            return {"status": "success", "fbtrace_id": "TRACE-CONFIRMED"}
        self.calls.append(call)
        if self.results:
            return self.results.pop(0)
        return {"status": "success", "fbtrace_id": "MOCK_TRACE_123"}


@pytest.fixture
def memory_db():
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def capture_module_absent(monkeypatch):
    """The merge job is deterministic here: the capture module counts as absent unless a test installs one."""
    monkeypatch.setitem(sys.modules, "src.ameen_workforce.capture", None)


def install_fake_capture(monkeypatch, merge_fn):
    module = types.ModuleType("src.ameen_workforce.capture")
    module.merge_pending_captures = merge_fn
    monkeypatch.setitem(sys.modules, "src.ameen_workforce.capture", module)


def _record_send_cycle(memory_db, finished_at, ok=True):
    with memory_db() as session:
        record_job_run(session, "send_due_events", finished_at - timedelta(seconds=1), finished_at, ok=ok, detail={})


@pytest.fixture
def live_tenant_12h(memory_db, fernet_key):
    with memory_db() as session:
        tenant = create_tenant(
            session,
            name="Store 12h Live",
            platform="shopify",
            shop_domain="store12h.myshopify.com",
            meta_dataset_id="DATASET_123",
            mode="live",
            settlement_hours=12.0,
        )
        store_credential(session, tenant.id, "meta_capi_token", "META_ACCESS_TOKEN_ABC")
        tenant_id = tenant.id
    return tenant_id


def make_order_payload(order_id=1001, placed=None, status="paid", fulfillment="fulfilled"):
    placed_dt = placed or (NOW - timedelta(hours=2))
    return {
        "id": order_id,
        "financial_status": status,
        "fulfillment_status": fulfillment,
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "950.00",
        "currency": "EGP",
        "created_at": placed_dt.isoformat(),
        "customer": {"email": EMAIL, "phone": PHONE},
        "client_details": {"browser_ip": IP, "user_agent": UA},
    }


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduler_tick_records_all_job_runs_and_heartbeat(memory_db):
    """A fresh tick with empty tables runs all 6 jobs (merge skipped when capture is absent) and writes JobRun entries."""
    sender = FakeSender()
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=sender)

    assert "send_due_events" in summary["jobs"]
    assert "retry_failed_events" in summary["jobs"]
    assert "purge_expired_checkout_context" in summary["jobs"]
    assert MERGE_JOB in summary["jobs"]
    assert "scheduler_heartbeat" in summary["jobs"]
    assert summary["jobs"]["scheduler_heartbeat"]["detail"]["all_jobs_succeeded"] is True
    assert summary["jobs"]["scheduler_heartbeat"]["detail"]["active_jobs_count"] == 5

    with memory_db() as session:
        runs = session.scalars(select(JobRun).order_by(JobRun.id)).all()
        assert len(runs) == 7  # + export_weekly_audiences (first tick)
        job_names = [r.job_name for r in runs]
        assert job_names == [
            "send_due_events",
            "retry_failed_events",
            "purge_expired_checkout_context",
            MERGE_JOB,
            "send_weekly_digests",
            "scheduler_heartbeat",
            "export_weekly_audiences",
        ]
        for r in runs:
            assert r.ok is True
            assert r.started_at is not None
            assert r.finished_at is not None


@pytest.mark.asyncio
async def test_scheduler_dispatches_due_events_after_settlement(memory_db, live_tenant_12h):
    """
    Events scheduled with a 12h settlement window are not sent immediately,
    but are dispatched once due_at has passed when run_scheduler_tick() runs.
    """
    sender = FakeSender()
    order_id = 8801
    delivered_time = NOW - timedelta(hours=14)
    placed_time = NOW - timedelta(hours=15)

    with memory_db() as session:
        from src.ameen_workforce.db import Tenant
        tenant = session.get(Tenant, live_tenant_12h)
        payload = make_order_payload(order_id=order_id, placed=placed_time)
        res = await process_webhook(
            session, tenant, "shopify", "orders/updated", payload, sender=sender, now=delivered_time
        )
        assert res["action"] == "SCHEDULED"
        assert res["capi_status"] == "scheduled"
        due_at = datetime.fromisoformat(res["due_at"])
        # due_at = delivered_time + 12h = NOW - 2h (which is in the past relative to NOW)
        assert due_at < NOW

    # Before tick: 0 CAPI calls were sent
    assert len(sender.calls) == 0

    # Run scheduler tick at NOW
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=sender)

    assert summary["jobs"]["send_due_events"]["counts"]["sent"] == 1
    assert len(sender.calls) == 1
    assert sender.calls[0]["pixel_id"] == "DATASET_123"

    with memory_db() as session:
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1, CapiEvent.event_name == "DeliveredPurchase"))
        assert event.status == "sent"
        assert event.http_code == 200


@pytest.mark.asyncio
async def test_scheduler_skips_events_not_yet_due(memory_db, live_tenant_12h):
    """Events whose settlement window has not passed are not dispatched."""
    sender = FakeSender()
    order_id = 8802
    # Delivered 2 hours ago: with 12h settlement, due in 10 hours
    delivered_time = NOW - timedelta(hours=2)
    placed_time = NOW - timedelta(hours=3)

    with memory_db() as session:
        from src.ameen_workforce.db import Tenant
        tenant = session.get(Tenant, live_tenant_12h)
        payload = make_order_payload(order_id=order_id, placed=placed_time)
        res = await process_webhook(
            session, tenant, "shopify", "orders/updated", payload, sender=sender, now=delivered_time
        )
        assert res["action"] == "SCHEDULED"

    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=sender)

    assert summary["jobs"]["send_due_events"]["counts"]["due"] == 0
    assert summary["jobs"]["send_due_events"]["counts"]["sent"] == 0
    assert len(sender.calls) == 0

    with memory_db() as session:
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1, CapiEvent.event_name == "DeliveredPurchase"))
        assert event.status == "scheduled"


@pytest.mark.asyncio
async def test_scheduler_retries_failed_events(memory_db, live_tenant_12h):
    """
    Events in `failed` state with attempts < max_attempts are retried during the tick.
    """
    failing_sender = FakeSender([
        {"status": "error", "http_code": 500, "error": "upstream_500"},
        {"status": "error", "http_code": 500, "error": "upstream_500"},
    ])
    recovering_sender = FakeSender([{"status": "success", "fbtrace_id": "RECOVERED_TRACE"}])

    order_id = 9901
    time_zero = NOW - timedelta(hours=14)

    with memory_db() as session:
        from src.ameen_workforce.db import Tenant
        tenant = session.get(Tenant, live_tenant_12h)
        payload = make_order_payload(order_id=order_id, placed=time_zero - timedelta(hours=1))
        # Initial delivery scheduled
        await process_webhook(session, tenant, "shopify", "orders/updated", payload, sender=failing_sender, now=time_zero)

    # First tick at NOW fails send_due_events (attempt 1) and retry_failed_events also fails (attempt 2)
    summary1 = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=failing_sender)
    assert summary1["jobs"]["send_due_events"]["counts"]["failed"] == 1
    assert summary1["jobs"]["retry_failed_events"]["counts"]["failed"] == 1

    with memory_db() as session:
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1, CapiEvent.event_name == "DeliveredPurchase"))
        assert event.status == "failed"
        assert event.attempts == 2

    # Second tick with recovering sender retries failed events
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW + timedelta(minutes=15), sender=recovering_sender)

    assert summary["jobs"]["retry_failed_events"]["counts"]["retried"] == 1
    assert summary["jobs"]["retry_failed_events"]["counts"]["sent"] == 1

    with memory_db() as session:
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1, CapiEvent.event_name == "DeliveredPurchase"))
        assert event.status == "sent"
        assert event.attempts == 3


@pytest.mark.asyncio
async def test_scheduler_safety_cutoff_window(memory_db, live_tenant_12h):
    """
    Verifies that when an order is delivered near the 6.5-day cutoff,
    due_at is capped at placed + 6.5d - 1h safety margin, and the scheduler
    dispatches it safely before the 6.5-day cutoff is breached.
    """
    sender = FakeSender()
    order_id = 8809
    # Placed 6 days ago (144 hours ago)
    placed_time = NOW - timedelta(days=6)
    # Delivered 5 days 18 hours ago (138 hours ago)
    # Without safety cutoff, 12h settlement would put due_at at 150h (which is past 6 days 6 hours = 150h, right at cutoff!)
    # But compute_due_at enforces min(delivered + 12h, placed + 6.5d - 1h) = placed + 156h - 1h = placed + 155h
    delivered_time = placed_time + timedelta(hours=143)

    with memory_db() as session:
        from src.ameen_workforce.db import Tenant
        tenant = session.get(Tenant, live_tenant_12h)
        payload = make_order_payload(order_id=order_id, placed=placed_time)
        res = await process_webhook(
            session, tenant, "shopify", "orders/updated", payload, sender=sender, now=delivered_time
        )
        assert res["action"] == "SCHEDULED"
        due_at = datetime.fromisoformat(res["due_at"])
        cutoff_deadline = placed_time + timedelta(days=6.5) - timedelta(hours=1)
        assert due_at <= cutoff_deadline

    # Run tick after due_at has passed (e.g. at placed_time + 155.5 hours, which is still < placed_time + 6.5d = 156h)
    tick_time = placed_time + timedelta(hours=155, minutes=10)
    summary = await run_scheduler_tick(session_factory=memory_db, now=tick_time, sender=sender)
    assert summary["jobs"]["send_due_events"]["counts"]["sent"] == 1


@pytest.mark.asyncio
async def test_scheduler_purges_expired_checkout_context(memory_db, live_tenant_12h):
    """
    Context older than 14 days is deleted by purge_expired_checkout_context job;
    unexpired context is retained.
    """
    with memory_db() as session:
        from src.ameen_workforce.db import Tenant
        tenant = session.get(Tenant, live_tenant_12h)
        # Create two dummy orders
        order1 = Order(tenant_id=tenant.id, platform_order_id="ORD-1", current_status="shipped", currency="EGP")
        order2 = Order(tenant_id=tenant.id, platform_order_id="ORD-2", current_status="shipped", currency="EGP")
        session.add_all([order1, order2])
        session.commit()

        # Capture context for order 1 15 days ago (expired)
        capture_checkout_context(session, order1.id, "1.1.1.1", "UA1", now=NOW - timedelta(days=15))
        # Capture context for order 2 5 days ago (active)
        capture_checkout_context(session, order2.id, "2.2.2.2", "UA2", now=NOW - timedelta(days=5))
        session.commit()

        assert session.scalar(select(CheckoutContext).where(CheckoutContext.order_id == order1.id)) is not None
        assert session.scalar(select(CheckoutContext).where(CheckoutContext.order_id == order2.id)) is not None

    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=FakeSender())

    assert summary["jobs"]["purge_expired_checkout_context"]["counts"]["purged_count"] == 1

    with memory_db() as session:
        # order 1 context deleted
        assert session.scalar(select(CheckoutContext).where(CheckoutContext.order_id == 1)) is None
        # order 2 context still exists
        assert session.scalar(select(CheckoutContext).where(CheckoutContext.order_id == 2)) is not None


@pytest.mark.asyncio
async def test_scheduler_error_isolation(memory_db, monkeypatch):
    """
    If one job raises an unhandled exception, it is recorded in JobRun as ok=False,
    and subsequent jobs continue to execute normally.
    """
    async def boom(*args, **kwargs):
        raise RuntimeError("Simulated database timeout")

    monkeypatch.setattr("src.ameen_workforce.scheduler.send_due_events", boom)

    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=FakeSender())

    assert summary["jobs"]["send_due_events"]["ok"] is False
    assert summary["jobs"]["send_due_events"]["error_type"] == "RuntimeError"

    # Subsequent jobs still succeeded
    assert summary["jobs"]["retry_failed_events"]["ok"] is True
    assert summary["jobs"]["purge_expired_checkout_context"]["ok"] is True
    assert summary["jobs"]["scheduler_heartbeat"]["ok"] is True
    assert summary["jobs"]["scheduler_heartbeat"]["detail"]["all_jobs_succeeded"] is False

    with memory_db() as session:
        runs = session.scalars(select(JobRun).order_by(JobRun.id)).all()
        assert len(runs) == 7  # + export_weekly_audiences (first tick)
        failed_run = next(r for r in runs if r.job_name == "send_due_events")
        assert failed_run.ok is False
        assert failed_run.error_type == "RuntimeError"
        assert "Simulated database timeout" in failed_run.detail


@pytest.mark.asyncio
async def test_scheduler_runner_lifecycle(memory_db):
    """Tests starting, running, and stopping the SchedulerRunner background worker."""
    ticks_executed = 0

    async def mock_tick(**kwargs):
        nonlocal ticks_executed
        ticks_executed += 1
        return {"jobs": {}}

    runner = SchedulerRunner(session_factory=memory_db, interval_seconds=0.05)
    runner._run_loop = None  # We'll test actual loop with small sleep

    # Test runner start and stop
    runner = SchedulerRunner(session_factory=memory_db, interval_seconds=0.01)
    # Monkeypatch run_scheduler_tick
    import src.ameen_workforce.scheduler as sched_mod
    orig_tick = sched_mod.run_scheduler_tick
    sched_mod.run_scheduler_tick = mock_tick

    try:
        assert runner.is_running is False
        await runner.start()
        assert runner.is_running is True

        # Let it run a few iterations
        await asyncio.sleep(0.05)
        assert ticks_executed >= 2

        await runner.stop()
        assert runner.is_running is False
    finally:
        sched_mod.run_scheduler_tick = orig_tick
        await runner.stop()


# -----------------------------------------------------------------------------
# merge_pending_captures job
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_job_skipped_gracefully_when_capture_module_missing(memory_db):
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=FakeSender())

    assert summary["jobs"][MERGE_JOB] == {"ok": True, "skipped": True}
    assert summary["jobs"]["scheduler_heartbeat"]["detail"]["all_jobs_succeeded"] is True
    with memory_db() as session:
        run = session.scalar(select(JobRun).where(JobRun.job_name == MERGE_JOB))
        assert run.ok is True
        assert json.loads(run.detail) == {"skipped": "capture_module_unavailable"}


@pytest.mark.asyncio
async def test_merge_job_runs_capture_merge_with_session_and_clock(memory_db, monkeypatch):
    calls = []

    def fake_merge(session, now=None):
        calls.append((session, now))
        return {"merged": 2}

    install_fake_capture(monkeypatch, fake_merge)
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=FakeSender())

    assert summary["jobs"][MERGE_JOB] == {"ok": True, "counts": {"merged": 2}}
    assert len(calls) == 1
    assert calls[0][1] == NOW


@pytest.mark.asyncio
async def test_merge_job_error_is_recorded_and_isolated(memory_db, monkeypatch):
    def broken_merge(session, now=None):
        raise ValueError("bad capture row")

    install_fake_capture(monkeypatch, broken_merge)
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=FakeSender())

    assert summary["jobs"][MERGE_JOB] == {"ok": False, "error_type": "ValueError"}
    assert summary["jobs"]["send_due_events"]["ok"] is True
    assert summary["jobs"]["scheduler_heartbeat"]["detail"]["all_jobs_succeeded"] is False


# -----------------------------------------------------------------------------
# Heartbeat check and --check flag
# -----------------------------------------------------------------------------

def test_check_scheduler_health_fresh_cycle_is_healthy(memory_db):
    now = datetime.now(timezone.utc)
    _record_send_cycle(memory_db, now - timedelta(minutes=10))

    info = check_scheduler_health(session_factory=memory_db, now=now)

    assert info["age_seconds"] == pytest.approx(600)


def test_check_scheduler_health_stale_cycle_raises(memory_db):
    now = datetime.now(timezone.utc)
    _record_send_cycle(memory_db, now - timedelta(minutes=60))

    with pytest.raises(SchedulerStale, match="limit 2700s"):
        check_scheduler_health(session_factory=memory_db, now=now)


def test_check_scheduler_health_ignores_failed_cycles(memory_db):
    now = datetime.now(timezone.utc)
    _record_send_cycle(memory_db, now - timedelta(minutes=60), ok=True)
    _record_send_cycle(memory_db, now - timedelta(minutes=1), ok=False)

    with pytest.raises(SchedulerStale):
        check_scheduler_health(session_factory=memory_db, now=now)


def test_check_scheduler_health_without_any_cycle_is_unhealthy(memory_db):
    with pytest.raises(SchedulerStale, match="no successful send cycle"):
        check_scheduler_health(session_factory=memory_db, now=datetime.now(timezone.utc))


def test_check_flag_exits_zero_when_fresh_and_one_when_stale(memory_db, capsys):
    now = datetime.now(timezone.utc)
    _record_send_cycle(memory_db, now - timedelta(minutes=5))
    assert scheduler_main(["--check"], session_factory=memory_db) == 0
    assert "OK" in capsys.readouterr().out

    with memory_db() as session:
        session.query(JobRun).delete()
        session.commit()
    _record_send_cycle(memory_db, now - timedelta(minutes=50))
    assert scheduler_main(["--check"], session_factory=memory_db) == 1
    assert "UNHEALTHY" in capsys.readouterr().err


def test_module_entrypoint_check_exit_codes(tmp_path):
    """`python -m ameen_workforce.scheduler --check` works as a real process against a file database."""
    db_url = f"sqlite:///{(tmp_path / 'motahai.db').as_posix()}"
    engine = make_engine(db_url)
    init_db(engine)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), DATABASE_URL=db_url)
    cmd = [sys.executable, "-m", "ameen_workforce.scheduler", "--check"]

    stale = subprocess.run(cmd, env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert stale.returncode == 1
    assert "UNHEALTHY" in stale.stderr

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        record_job_run(session, "send_due_events", datetime.now(timezone.utc) - timedelta(minutes=6),
                       datetime.now(timezone.utc) - timedelta(minutes=5), ok=True, detail={})
    engine.dispose()

    fresh = subprocess.run(cmd, env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert fresh.returncode == 0, fresh.stderr
    assert "OK" in fresh.stdout


# -----------------------------------------------------------------------------
# Standalone daemon loop (injectable cycle, no real sleeping)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_daemon_runs_injected_cycles_without_sleeping():
    cycles = 0
    waits = []

    async def cycle():
        nonlocal cycles
        cycles += 1

    async def fake_wait(seconds, stop):
        waits.append(seconds)

    ran = await run_daemon(interval_seconds=900, cycle=cycle, wait=fake_wait, max_cycles=3)

    assert ran == 3
    assert cycles == 3
    assert waits == [900, 900]  # no wait after the final cycle


@pytest.mark.asyncio
async def test_run_daemon_stops_when_stop_event_is_set():
    stop = asyncio.Event()
    calls = 0

    async def cycle():
        nonlocal calls
        calls += 1
        if calls == 2:
            stop.set()

    async def fake_wait(seconds, _stop):
        pass

    ran = await run_daemon(cycle=cycle, wait=fake_wait, stop_event=stop)

    assert ran == 2


@pytest.mark.asyncio
async def test_run_daemon_keeps_looping_after_a_failing_cycle():
    calls = 0

    async def cycle():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")

    async def fake_wait(seconds, _stop):
        pass

    ran = await run_daemon(cycle=cycle, wait=fake_wait, max_cycles=2)

    assert ran == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_wait_or_stop_returns_at_once_when_already_stopped():
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(_wait_or_stop(DEFAULT_SCHEDULER_INTERVAL_SECONDS, stop), timeout=1)


# -----------------------------------------------------------------------------
# In-app scheduler gate in the FastAPI lifespan
# -----------------------------------------------------------------------------

def _fake_runner_class(events):
    class FakeRunner:
        def __init__(self, *args, **kwargs):
            events.append("init")

        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    return FakeRunner


def test_lifespan_does_not_start_scheduler_by_default(monkeypatch):
    events = []
    monkeypatch.delenv("MOTAHAI_RUN_SCHEDULER_IN_APP", raising=False)
    monkeypatch.setattr(sched_mod, "SchedulerRunner", _fake_runner_class(events))
    monkeypatch.setattr(service, "init_db", lambda *a, **k: None)

    with TestClient(service.app):
        pass

    assert events == []


def test_lifespan_starts_and_stops_scheduler_when_enabled(monkeypatch):
    events = []
    monkeypatch.setenv("MOTAHAI_RUN_SCHEDULER_IN_APP", "1")
    monkeypatch.setattr(sched_mod, "SchedulerRunner", _fake_runner_class(events))
    monkeypatch.setattr(service, "init_db", lambda *a, **k: None)

    with TestClient(service.app):
        assert events == ["init", "start"]

    assert events == ["init", "start", "stop"]
