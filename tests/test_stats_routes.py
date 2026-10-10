"""
test_stats_routes.py — tenant-scoped stats endpoints (issue #41).

Each endpoint must return exactly what the digest q_* function returns for the same tenant and window (the cohort
queries use the window shifted back 7 days, as build_digest does). Fixed window: week = 2026-10-04 .. 2026-10-11 UTC,
cohort = 2026-09-27 .. 2026-10-04 UTC. Tenant B owns similar rows in the same windows and must never leak into A.
"""

from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import digest
from src.ameen_workforce.db import CapiEvent, Order, OrderStatusEvent, create_tenant
from src.ameen_workforce.stats_routes import get_stats_session_factory, router

OPERATOR_KEY = "op-key-for-stats-tests"
AUTH = {"Authorization": f"Bearer {OPERATOR_KEY}"}
WEEK = (datetime(2026, 10, 4, tzinfo=timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
COHORT = (datetime(2026, 9, 27, tzinfo=timezone.utc), datetime(2026, 10, 4, tzinfo=timezone.utc))
QS = "start=2026-10-04&end=2026-10-11"
_n = {"i": 0}


def utc(month, day, hour=12):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


def add_order(session, tenant, placed, *, cod=True, value=100.0, status="pending", confirmed=False, order_total=None,
              ad_id=None, shipped=False):
    _n["i"] += 1
    order = Order(tenant_id=tenant.id, platform_order_id=f"o{_n['i']}", is_cod=cod, value=value,
                  currency=tenant.currency, current_status=status, order_total=order_total, ad_id=ad_id,
                  created_at_platform=placed, created_at=placed, confirmed_at=placed if confirmed else None)
    session.add(order)
    session.flush()
    if shipped:
        session.add(OrderStatusEvent(order_id=order.id, status="shipped", received_at=placed))
    session.commit()
    return order


def add_event(session, tenant, order, name, status, when, flags=None):
    session.add(CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=name, event_id=f"{name}_{order.id}",
                          status=status, created_at=when, sent_at=when if status == "sent" else None,
                          quality_flags=flags))
    session.commit()


def seed(session, tenant, scale=1):
    """Week orders, cohort orders (incl. refused COD, 10 on one ad), signal events. `scale` varies the volume."""
    w1 = add_order(session, tenant, utc(10, 5), cod=True, confirmed=True)
    add_order(session, tenant, utc(10, 7), cod=False)
    for _ in range(scale - 1):
        add_order(session, tenant, utc(10, 8), cod=True)
    add_event(session, tenant, w1, "ConfirmedOrder", "sent", utc(10, 5, 13), flags="missing_user_agent")
    for i in range(10):
        c = add_order(session, tenant, utc(9, 27 + i % 4), cod=True, value=100.0 + scale, order_total=120.0,
                      status="delivered" if i < 6 * scale % 10 + 3 else "pending", ad_id="adX")
        if i == 0:
            add_event(session, tenant, c, "DeliveredPurchase", "sent", utc(10, 6))
    add_order(session, tenant, utc(10, 1), cod=True, value=0, order_total=250.0 * scale, status="cancelled",
              shipped=True)


@pytest.fixture
def tenants(db_session):
    a = create_tenant(db_session, name="A Shop", platform="shopify", shop_domain="a.myshopify.com")
    b = create_tenant(db_session, name="B Shop", platform="salla", shop_domain="555")
    seed(db_session, a, scale=1)
    seed(db_session, b, scale=3)
    return a, b


@pytest.fixture
def client(db_session, fernet_key, monkeypatch, tenants):
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_stats_session_factory] = lambda: factory
    return TestClient(app)


def expected(session, tenant_id):
    return {
        "week-orders": digest.q_week_orders(session, tenant_id, WEEK),
        "cohort-delivery": {"all": digest.q_cohort_delivery(session, tenant_id, COHORT),
                            "ad_driven": digest.q_cohort_delivery(session, tenant_id, COHORT, ad_only=True)},
        "refused-cod": digest.q_refused_cod(session, tenant_id, COHORT),
        "signal-health": digest.q_signal_health(session, tenant_id, WEEK),
        "creatives": digest.q_creatives(session, tenant_id, COHORT),
    }


def url(tenant_id, name, qs=QS):
    return f"/v1/tenants/{tenant_id}/stats/{name}" + (f"?{qs}" if qs else "")


@pytest.mark.parametrize("name", ["week-orders", "cohort-delivery", "refused-cod", "signal-health", "creatives"])
def test_endpoint_matches_query_function(client, db_session, tenants, name):
    a, _ = tenants
    resp = client.get(url(a.id, name), headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    want = expected(db_session, a.id)[name]
    assert body["data"] == want
    assert body["tenant_id"] == a.id
    assert body["window"] == {"start": WEEK[0].isoformat(), "end": WEEK[1].isoformat()}
    # sanity: the seeded dataset is non-trivial, so equality above is meaningful
    assert want not in ({}, [], {"count": 0, "value": 0.0})


def test_summary_matches_all_five(client, db_session, tenants):
    a, _ = tenants
    resp = client.get(url(a.id, "summary"), headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == expected(db_session, a.id)
    assert body["cohort_window"] == {"start": COHORT[0].isoformat(), "end": COHORT[1].isoformat()}


def test_default_window_is_last_seven_full_utc_days(client, db_session, tenants):
    a, _ = tenants
    midnight = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    before = digest.q_week_orders(db_session, a.id, (midnight - timedelta(days=7), midnight))["placed"]
    add_order(db_session, a, midnight - timedelta(days=1, hours=-12), cod=True)
    add_order(db_session, a, midnight + timedelta(hours=1), cod=True)  # today: outside the window
    body = client.get(url(a.id, "week-orders", ""), headers=AUTH).json()
    assert body["window"] == {"start": (midnight - timedelta(days=7)).isoformat(), "end": midnight.isoformat()}
    assert body["data"] == digest.q_week_orders(db_session, a.id, (midnight - timedelta(days=7), midnight))
    assert body["data"]["placed"] == before + 1  # yesterday counted, today not


def test_timezone_aware_bounds_are_converted_to_utc(client, tenants):
    a, _ = tenants
    body = client.get(url(a.id, "week-orders", "start=2026-10-04T02:00:00%2B02:00&end=2026-10-11T00:00:00Z"),
                      headers=AUTH).json()
    assert body["window"] == {"start": WEEK[0].isoformat(), "end": WEEK[1].isoformat()}


@pytest.mark.parametrize("qs", [
    "start=2026-10-11&end=2026-10-04",             # end before start
    "start=2026-10-04&end=2026-10-04",             # empty window
    "start=2026-01-01&end=2026-10-01",             # > 92 days
    "start=not-a-date&end=2026-10-11",
    "start=2026-10-04&end=garbage",
])
def test_bad_window_is_422(client, tenants, qs):
    a, _ = tenants
    for name in ("week-orders", "summary"):
        assert client.get(url(a.id, name, qs), headers=AUTH).status_code == 422


def test_window_of_exactly_92_days_is_accepted(client, tenants):
    a, _ = tenants
    assert client.get(url(a.id, "week-orders", "start=2026-07-01&end=2026-10-01"), headers=AUTH).status_code == 200


def test_requires_operator_key(client, tenants, monkeypatch):
    a, _ = tenants
    for name in ("week-orders", "cohort-delivery", "refused-cod", "signal-health", "creatives", "summary"):
        assert client.get(url(a.id, name)).status_code == 401
        assert client.get(url(a.id, name), headers={"Authorization": "Bearer wrong"}).status_code == 401
    monkeypatch.delenv("OPERATOR_API_KEY")
    assert client.get(url(a.id, "summary"), headers=AUTH).status_code == 401  # fails closed when unconfigured


def test_unknown_tenant_is_404(client, tenants):
    assert client.get(url(99999, "summary"), headers=AUTH).status_code == 404
    assert client.get(url(99999, "week-orders"), headers=AUTH).status_code == 404


def test_tenant_isolation(client, db_session, tenants):
    a, b = tenants
    want_a, want_b = expected(db_session, a.id), expected(db_session, b.id)
    assert want_a != want_b  # B's data really differs, so a leak would be visible
    body_a = client.get(url(a.id, "summary"), headers=AUTH).json()
    body_b = client.get(url(b.id, "summary"), headers=AUTH).json()
    assert body_a["data"] == want_a and body_a["tenant_id"] == a.id
    assert body_b["data"] == want_b and body_b["tenant_id"] == b.id
    # A's id returns only A's numbers even though B's rows sit in the same tables and windows
    total_week = db_session.query(Order).filter(Order.created_at >= WEEK[0], Order.created_at < WEEK[1]).count()
    assert body_a["data"]["week-orders"]["placed"] + body_b["data"]["week-orders"]["placed"] == total_week
    # the tenant can only come from the path: a spoofed query param or header changes nothing
    spoof = client.get(url(a.id, "summary", QS + f"&tenant_id={b.id}"), headers={**AUTH, "X-Tenant-Id": str(b.id)})
    assert spoof.json()["data"] == want_a
