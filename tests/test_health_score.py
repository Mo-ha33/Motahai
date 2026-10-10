"""
test_health_score.py — tracking health score (issue #42): each penalty in isolation, clamping, insufficient data,
and tenant isolation through GET /v1/tenants/{id}/stats/health-score.

Window: 2026-10-04 .. 2026-10-11 UTC (as_of = 2026-10-11). Tests that need a fresh heartbeat seed one explicitly.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import health_score as hs
from src.ameen_workforce.db import CapiEvent, Incident, JobRun, Order, StagedWebhook, create_tenant
from src.ameen_workforce.stats_routes import get_stats_session_factory, router

OPERATOR_KEY = "op-key-for-health-tests"
AUTH = {"Authorization": f"Bearer {OPERATOR_KEY}"}
WEEK = (datetime(2026, 10, 4, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
QS = "start=2026-10-04&end=2026-10-11"
_n = {"i": 0}


def utc(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def heartbeat(session, finished=None, ok=True, job="send_due_events"):
    finished = finished or WEEK[1] - timedelta(minutes=10)
    session.add(JobRun(job_name=job, started_at=finished - timedelta(seconds=5), finished_at=finished, ok=ok))
    session.commit()


def event(session, tenant, status="sent", flags=None, name="ConfirmedOrder"):
    _n["i"] += 1
    order = Order(tenant_id=tenant.id, platform_order_id=f"h{_n['i']}", is_cod=True, value=10.0, currency="EGP",
                  current_status="pending", created_at_platform=utc(5), created_at=utc(5))
    session.add(order)
    session.flush()
    session.add(CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=name, event_id=f"e{_n['i']}",
                          status=status, created_at=utc(6), sent_at=utc(6) if status == "sent" else None,
                          quality_flags=flags))
    session.commit()


def incident(session, tenant, severity, resolved=False, kind="x"):
    session.add(Incident(tenant_id=tenant.id, kind=kind, severity=severity, detected_at=utc(6),
                         resolved_at=utc(7) if resolved else None))
    session.commit()


def staged(session, tenant, status, received=None):
    _n["i"] += 1
    session.add(StagedWebhook(tenant_id=tenant.id, platform="salla", topic="order.created", order_ref=f"r{_n['i']}",
                              payload_ciphertext="" if status == "dead" else "x", received_at=received or utc(6),
                              next_attempt_at=utc(6), status=status))
    session.commit()


def names(result):
    return {p["name"]: p for p in result["penalties"]}


@pytest.fixture
def tenant(db_session):
    return create_tenant(db_session, name="A Shop", platform="shopify", shop_domain="a.myshopify.com")


def score(session, tenant):
    return hs.tenant_health_score(session, tenant.id, WEEK)


def test_insufficient_data_is_not_100(db_session, tenant):
    heartbeat(db_session)  # scheduler rows are global and do not count as tenant data
    assert score(db_session, tenant) == {"score": None, "status": "insufficient_data", "penalties": []}
    assert hs.compute_health_score(hs.HealthInputs())["score"] is None


def test_clean_tenant_scores_100(db_session, tenant):
    heartbeat(db_session)
    event(db_session, tenant)
    assert score(db_session, tenant) == {"score": 100, "status": "healthy", "penalties": []}


def test_flagged_events_penalty(db_session, tenant):
    heartbeat(db_session)
    event(db_session, tenant, flags="missing_user_agent")
    for _ in range(3):
        event(db_session, tenant)
    p = names(score(db_session, tenant))["flagged_events"]
    assert p["points"] == round(hs.FLAGGED_MAX_POINTS * 1 / 4)
    assert p["evidence"] == {"flagged": 1, "total": 4}


def test_late_delivery_penalty_and_cap(db_session, tenant):
    heartbeat(db_session)
    event(db_session, tenant)
    for _ in range(2):
        event(db_session, tenant, status="late_delivery", name="DeliveredPurchase")
    p = names(score(db_session, tenant))["late_delivery"]
    assert p["points"] == 2 * hs.LATE_DELIVERY_POINTS and p["evidence"] == {"count": 2}
    for _ in range(10):
        event(db_session, tenant, status="late_delivery", name="DeliveredPurchase")
    assert names(score(db_session, tenant))["late_delivery"]["points"] == hs.LATE_DELIVERY_MAX


def test_scheduler_stale_when_no_heartbeat(db_session, tenant):
    event(db_session, tenant)
    p = names(score(db_session, tenant))["scheduler_stale"]
    assert p["points"] == hs.SCHEDULER_STALE_POINTS and p["evidence"]["last_success_found"] is False


def test_scheduler_stale_when_old_and_fresh_ok(db_session, tenant):
    event(db_session, tenant)
    heartbeat(db_session, finished=WEEK[1] - timedelta(minutes=46))
    p = names(score(db_session, tenant))["scheduler_stale"]
    assert p["evidence"]["last_success_found"] is True and p["evidence"]["age_seconds"] == 46 * 60
    heartbeat(db_session, finished=WEEK[1] - timedelta(minutes=44))
    assert "scheduler_stale" not in names(score(db_session, tenant))


def test_failed_heartbeat_does_not_count_as_fresh(db_session, tenant):
    event(db_session, tenant)
    heartbeat(db_session, ok=False)
    assert "scheduler_stale" in names(score(db_session, tenant))


def test_job_failures_penalty_and_cap(db_session, tenant):
    event(db_session, tenant)
    heartbeat(db_session)
    heartbeat(db_session, finished=utc(6), ok=False, job="merge_pending_captures")
    heartbeat(db_session, finished=utc(7), ok=False, job="send_weekly_digests")
    p = names(score(db_session, tenant))["job_failures"]
    assert p["points"] == 2 * hs.JOB_FAILURE_POINTS and p["evidence"] == {"count": 2}
    for h in range(1, 8):
        heartbeat(db_session, finished=utc(8, h), ok=False)
    heartbeat(db_session, finished=datetime(2026, 9, 1, tzinfo=timezone.utc), ok=False)  # outside the window
    p = names(score(db_session, tenant))["job_failures"]
    assert p["points"] == hs.JOB_FAILURE_MAX and p["evidence"]["count"] == 9


@pytest.mark.parametrize("severity", ["critical", "error", "warning"])
def test_open_incident_penalty_by_severity(db_session, tenant, severity):
    heartbeat(db_session)
    incident(db_session, tenant, severity)
    incident(db_session, tenant, severity, resolved=True)  # resolved incidents are free
    per, cap = hs.INCIDENT_POINTS[severity]
    p = names(score(db_session, tenant))[f"incidents_{severity}"]
    assert p["points"] == per and p["evidence"] == {"open": 1}
    for _ in range(5):
        incident(db_session, tenant, severity)
    assert names(score(db_session, tenant))[f"incidents_{severity}"]["points"] == cap


def test_unknown_severity_is_data_but_no_penalty(db_session, tenant):
    heartbeat(db_session)
    incident(db_session, tenant, "info")
    assert score(db_session, tenant) == {"score": 100, "status": "healthy", "penalties": []}


def test_webhooks_dead_penalty(db_session, tenant):
    heartbeat(db_session)
    staged(db_session, tenant, "dead")
    p = names(score(db_session, tenant))["webhooks_dead"]
    assert p["points"] == hs.WEBHOOK_DEAD_POINTS and p["evidence"] == {"count": 1}
    for _ in range(5):
        staged(db_session, tenant, "dead")
    assert names(score(db_session, tenant))["webhooks_dead"]["points"] == hs.WEBHOOK_DEAD_MAX


def test_webhooks_stuck_penalty_ignores_fresh_rows(db_session, tenant):
    heartbeat(db_session)
    staged(db_session, tenant, "pending", received=WEEK[1] - timedelta(minutes=10))  # still inside the grace
    assert score(db_session, tenant) == {"score": 100, "status": "healthy", "penalties": []}
    staged(db_session, tenant, "pending", received=WEEK[1] - timedelta(hours=3))
    p = names(score(db_session, tenant))["webhooks_stuck"]
    assert p["points"] == hs.WEBHOOK_STUCK_POINTS and p["evidence"] == {"count": 1}


def test_clamped_to_zero_and_status_bands():
    worst = hs.HealthInputs(total_events=10, flagged_events=10, late_delivery=50, heartbeat_age_seconds=None,
                            job_failures=50, open_incidents={"critical": 9, "error": 9, "warning": 9},
                            webhooks_dead=9, webhooks_stuck=9, staged_webhooks=18)
    result = hs.compute_health_score(worst)
    assert sum(p["points"] for p in result["penalties"]) > 100
    assert result["score"] == 0 and result["status"] == "critical"

    def run(**open_incidents):
        return hs.compute_health_score(hs.HealthInputs(total_events=5, heartbeat_age_seconds=1.0,
                                                       open_incidents=open_incidents))
    assert (run()["score"], run()["status"]) == (100, "healthy")
    assert (run(critical=1)["score"], run(critical=1)["status"]) == (80, "healthy")
    assert (run(critical=1, warning=1)["score"], run(critical=1, warning=1)["status"]) == (76, "degraded")
    assert (run(critical=2, error=1)["score"], run(critical=2, error=1)["status"]) == (50, "degraded")
    assert (run(critical=2, error=2)["score"], run(critical=2, error=2)["status"]) == (40, "critical")


def test_no_pii_or_free_text_in_output(db_session, tenant):
    event(db_session, tenant, flags="missing_user_agent")
    incident(db_session, tenant, "error", kind="secret-kind")
    text = str(score(db_session, tenant))
    assert "secret-kind" not in text and "missing_user_agent" not in text


@pytest.fixture
def client(db_session, monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_stats_session_factory] = lambda: factory
    return TestClient(app)


def url(tid, qs=QS):
    return f"/v1/tenants/{tid}/stats/health-score?{qs}"


def test_endpoint_tenant_isolation_and_matches_function(client, db_session):
    a = create_tenant(db_session, name="A", platform="shopify", shop_domain="a.myshopify.com")
    b = create_tenant(db_session, name="B", platform="salla", shop_domain="555")
    c = create_tenant(db_session, name="C", platform="zid", shop_domain="777")  # no data at all
    heartbeat(db_session)
    event(db_session, a)
    event(db_session, b, flags="missing_user_agent")
    incident(db_session, b, "critical")
    staged(db_session, b, "dead")
    body_a = client.get(url(a.id), headers=AUTH).json()
    body_b = client.get(url(b.id), headers=AUTH).json()
    body_c = client.get(url(c.id), headers=AUTH).json()
    assert body_a["tenant_id"] == a.id and body_a["data"] == {"score": 100, "status": "healthy", "penalties": []}
    assert body_b["data"] == score(db_session, b) and body_b["data"]["score"] == 100 - 30 - 20 - 8
    assert body_c["data"] == {"score": None, "status": "insufficient_data", "penalties": []}
    assert body_a["window"]["start"].startswith("2026-10-04")
    spoof = client.get(url(a.id, QS + f"&tenant_id={b.id}"), headers={**AUTH, "X-Tenant-Id": str(b.id)})
    assert spoof.json()["data"] == body_a["data"]


def test_endpoint_auth_404_and_422(client, db_session):
    a = create_tenant(db_session, name="A", platform="shopify", shop_domain="a.myshopify.com")
    assert client.get(url(a.id)).status_code == 401
    assert client.get(url(a.id), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(url(99999), headers=AUTH).status_code == 404
    assert client.get(url(a.id, "start=2026-10-11&end=2026-10-04"), headers=AUTH).status_code == 422
