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
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce.capi_service import DELIVERED_EVENT, MetaCAPISender
from src.ameen_workforce.checkout_context import capture_checkout_context
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import CapiEvent, CheckoutContext, JobRun, Order, create_tenant, init_db, make_engine
from src.ameen_workforce.order_pipeline import (
    DEFAULT_SETTLEMENT_HOURS, DUE_SAFETY_MARGIN, compute_due_at, process_webhook
)
from src.ameen_workforce.scheduler import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS, SchedulerRunner, record_job_run, run_scheduler_tick
)

NOW = datetime.now(timezone.utc)
EMAIL = "customer@example.com"
PHONE = "01012345678"
IP = "197.35.12.99"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class FakeSender(MetaCAPISender):
    """Real D-005 logic, mock Meta network call."""
    def __init__(self, results=None):
        super().__init__()
        self.calls = []
        self.results = list(results or [])

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append({"pixel_id": pixel_id, "access_token": access_token, "payload": payload})
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
    """A fresh tick with empty tables runs all 4 jobs and writes JobRun entries."""
    sender = FakeSender()
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW, sender=sender)

    assert "send_due_events" in summary["jobs"]
    assert "retry_failed_events" in summary["jobs"]
    assert "purge_expired_checkout_context" in summary["jobs"]
    assert "scheduler_heartbeat" in summary["jobs"]
    assert summary["jobs"]["scheduler_heartbeat"]["detail"]["all_jobs_succeeded"] is True

    with memory_db() as session:
        runs = session.scalars(select(JobRun).order_by(JobRun.id)).all()
        assert len(runs) == 4
        job_names = [r.job_name for r in runs]
        assert job_names == [
            "send_due_events",
            "retry_failed_events",
            "purge_expired_checkout_context",
            "scheduler_heartbeat",
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
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1))
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
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1))
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
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1))
        assert event.status == "failed"
        assert event.attempts == 2

    # Second tick with recovering sender retries failed events
    summary = await run_scheduler_tick(session_factory=memory_db, now=NOW + timedelta(minutes=15), sender=recovering_sender)

    assert summary["jobs"]["retry_failed_events"]["counts"]["retried"] == 1
    assert summary["jobs"]["retry_failed_events"]["counts"]["sent"] == 1

    with memory_db() as session:
        event = session.scalar(select(CapiEvent).where(CapiEvent.order_id == 1))
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
        assert len(runs) == 4
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
