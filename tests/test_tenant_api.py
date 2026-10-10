"""
test_tenant_api.py — read-only Core API for the Tariq agent (S3-1): /v1/tenants/{tenant_key}/status and /signal-health.

Auth is a Bearer HERMES_TENANT_API_KEY (fail closed; must differ from OPERATOR_API_KEY). Two tenants own similar rows
and must never leak into each other. Seeded PII-like values (email/phone hashes, IP/UA ciphertext, order ids, webhook
payload hashes) must never appear in any response.
"""

import json
import logging
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce.credentials import encrypt_value, store_credential
from src.ameen_workforce.db import (
    CapiEvent, CheckoutContext, Incident, Order, OrderStatusEvent, WebhookDelivery, create_tenant, utcnow
)
from src.ameen_workforce.tenant_api import get_tenant_api_session_factory, router

TENANT_KEY = "hermes-tenant-key-for-tests"
OPERATOR_KEY = "operator-key-for-tests"
AUTH = {"Authorization": f"Bearer {TENANT_KEY}"}

SECRET_EMAIL_HASH = "e" * 64
SECRET_PHONE_HASH = "f" * 64
SECRET_ORDER_ID = "ORDER-SECRET-98765"
SECRET_IP = "203.0.113.77"
SECRET_UA = "Mozilla/5.0 SecretBrowser/9.9"
SECRET_TOKEN = "SECRET-META-TOKEN-123"
SECRET_PAYLOAD_HASH = "d" * 64
_n = {"i": 0}


def add_order(session, tenant, placed, *, cod=True, status="pending", confirmed=False, shipped=False, pii=False):
    _n["i"] += 1
    order = Order(
        tenant_id=tenant.id, platform_order_id=SECRET_ORDER_ID if pii else f"o{_n['i']}", is_cod=cod, value=100.0,
        currency=tenant.currency, current_status=status, created_at_platform=placed, created_at=placed,
        confirmed_at=placed if confirmed else None, order_total=120.0,
        email_hash=SECRET_EMAIL_HASH if pii else None, phone_hash=SECRET_PHONE_HASH if pii else None)
    session.add(order)
    session.flush()
    if pii:
        session.add(CheckoutContext(order_id=order.id, ip_ciphertext=encrypt_value(SECRET_IP),
                                    ua_ciphertext=encrypt_value(SECRET_UA), expires_at=utcnow() + timedelta(days=1)))
    if shipped:
        session.add(OrderStatusEvent(order_id=order.id, status="shipped", received_at=placed))
    session.commit()
    return order


def add_event(session, tenant, order, name, status, when):
    session.add(CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=name, event_id=f"{name}_{order.id}",
                          status=status, created_at=when, sent_at=when if status == "sent" else None))
    session.commit()


def add_delivery(session, tenant, *, signature_ok=True, processed_ok=True, error_type=None, received_at=None,
                 duplicate=False, topic="orders/create"):
    _n["i"] += 1
    session.add(WebhookDelivery(
        tenant_id=tenant.id if tenant else None, platform="shopify", topic=topic, dedupe_key=f"k{_n['i']}",
        payload_sha256=SECRET_PAYLOAD_HASH, signature_ok=signature_ok, processed_ok=processed_ok,
        error_type=error_type, is_duplicate=duplicate, received_at=received_at or utcnow()))
    session.commit()


def seed_a(session, tenant):
    now = utcnow()
    o1 = add_order(session, tenant, now - timedelta(days=1), cod=True, confirmed=True, pii=True)
    o2 = add_order(session, tenant, now - timedelta(days=2), cod=True, status="delivered", confirmed=True)
    add_order(session, tenant, now - timedelta(days=3), cod=False)
    add_order(session, tenant, now - timedelta(days=4), cod=True, status="cancelled", shipped=True)
    add_order(session, tenant, now - timedelta(days=20), cod=True)  # outside a 7-day window
    add_event(session, tenant, o1, "ConfirmedOrder", "sent", now - timedelta(hours=5))
    add_event(session, tenant, o1, "DeliveredPurchase", "failed", now - timedelta(hours=4))
    add_event(session, tenant, o2, "ConfirmedOrder", "shadow", now - timedelta(hours=3))
    add_event(session, tenant, o2, "DeliveredPurchase", "late_delivery", now - timedelta(hours=2))
    add_event(session, tenant, o2, "Other", "stale", now - timedelta(hours=1))  # not a signal event: ignored
    session.add(Incident(tenant_id=tenant.id, kind="webhook_failed", severity="error",
                         detected_at=now - timedelta(days=2), detail=f"secret {SECRET_IP}"))
    session.add(Incident(tenant_id=tenant.id, kind="old_resolved", severity="error",
                         detected_at=now - timedelta(days=2), resolved_at=now - timedelta(days=1)))
    session.commit()
    add_delivery(session, tenant, received_at=now - timedelta(days=2))
    add_delivery(session, tenant, received_at=now - timedelta(hours=6))
    add_delivery(session, tenant, processed_ok=False, error_type="RuntimeError", received_at=now - timedelta(days=1))
    add_delivery(session, tenant, signature_ok=False, processed_ok=False, error_type="invalid_signature")
    add_delivery(session, tenant, processed_ok=False, error_type="topic_not_handled")  # ignored topic: not a failure
    add_delivery(session, tenant, processed_ok=True, duplicate=True)
    add_delivery(session, tenant, processed_ok=False, error_type="RuntimeError", received_at=now - timedelta(days=10))


def seed_b(session, tenant):
    now = utcnow()
    for i in range(5):
        o = add_order(session, tenant, now - timedelta(days=1, hours=i), cod=True, confirmed=True)
        add_event(session, tenant, o, "ConfirmedOrder", "sent", now - timedelta(hours=i + 1))
    session.add(Incident(tenant_id=tenant.id, kind="b_only_incident", severity="error", detected_at=now))
    session.commit()
    add_delivery(session, tenant, processed_ok=False, error_type="RuntimeError")


@pytest.fixture
def tenants(db_session, fernet_key):
    a = create_tenant(db_session, name="A Shop", platform="shopify", shop_domain="a.myshopify.com",
                      meta_dataset_id="111", mode="live")
    b = create_tenant(db_session, name="B Shop", platform="salla", shop_domain="555")
    store_credential(db_session, a.id, "meta_capi_token", SECRET_TOKEN)
    seed_a(db_session, a)
    seed_b(db_session, b)
    return a, b


@pytest.fixture
def client(db_session, monkeypatch, tenants):
    monkeypatch.setenv("HERMES_TENANT_API_KEY", TENANT_KEY)
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_tenant_api_session_factory] = lambda: factory
    return TestClient(app)


PATHS = ["/v1/tenants/a.myshopify.com/status", "/v1/tenants/a.myshopify.com/signal-health"]


# --- Auth ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PATHS)
def test_401_without_token(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PATHS)
def test_401_with_wrong_token(client, path):
    assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(path, headers={"Authorization": TENANT_KEY}).status_code == 401  # no Bearer scheme
    assert client.get(path, headers={"Authorization": f"Bearer {OPERATOR_KEY}"}).status_code == 401


@pytest.mark.parametrize("path", PATHS)
def test_401_when_key_unset(client, monkeypatch, path):
    monkeypatch.delenv("HERMES_TENANT_API_KEY")
    assert client.get(path, headers=AUTH).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer "}).status_code == 401
    monkeypatch.setenv("HERMES_TENANT_API_KEY", "")
    assert client.get(path, headers={"Authorization": "Bearer "}).status_code == 401


@pytest.mark.parametrize("path", PATHS)
def test_401_when_key_equals_operator_key(client, monkeypatch, path):
    monkeypatch.setenv("OPERATOR_API_KEY", TENANT_KEY)
    assert client.get(path, headers=AUTH).status_code == 401


@pytest.mark.parametrize("path", PATHS)
def test_200_with_correct_token(client, path):
    assert client.get(path, headers=AUTH).status_code == 200


def test_auth_runs_before_validation_and_tenant_lookup(client):
    assert client.get("/v1/tenants/a.myshopify.com/signal-health?window_days=0").status_code == 401
    assert client.get("/v1/tenants/ghost.myshopify.com/status").status_code == 401


# --- 404 / validation ------------------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", ["status", "signal-health"])
def test_404_unknown_tenant_is_generic(client, suffix):
    resp = client.get(f"/v1/tenants/ghost.myshopify.com/{suffix}", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not found"}


def test_tenant_key_is_normalized(client):
    assert client.get("/v1/tenants/A.MyShopify.com/status", headers=AUTH).json()["tenant_key"] == "a.myshopify.com"


@pytest.mark.parametrize("days", ["0", "29", "-1", "abc"])
def test_window_days_rejected(client, days):
    assert client.get(f"/v1/tenants/a.myshopify.com/signal-health?window_days={days}", headers=AUTH).status_code == 422


@pytest.mark.parametrize("days", [1, 7, 28])
def test_window_days_accepted(client, days):
    body = client.get(f"/v1/tenants/a.myshopify.com/signal-health?window_days={days}", headers=AUTH).json()
    assert body["window"]["days"] == days


# --- /status ---------------------------------------------------------------------------------------------

def test_status_tenant_a(client):
    body = client.get("/v1/tenants/a.myshopify.com/status", headers=AUTH).json()
    assert set(body) == {
        "tenant_key", "platform", "mode", "active", "created_at", "has_meta_credentials", "last_webhook_at",
        "webhook_failures_7d", "last_capi_event_at", "webhooks_verified", "onboarded"}
    assert body["tenant_key"] == "a.myshopify.com"
    assert body["platform"] == "shopify"
    assert body["mode"] == "live"
    assert body["active"] is True
    assert body["has_meta_credentials"] is True
    assert body["webhooks_verified"] is True
    assert body["onboarded"] is True
    # failures: RuntimeError (1d ago) + invalid_signature; excludes topic_not_handled, the duplicate, and the 10d-old row
    assert body["webhook_failures_7d"] == 2
    assert body["last_webhook_at"] is not None and body["last_capi_event_at"] is not None


def test_status_tenant_b_is_independent(client):
    body = client.get("/v1/tenants/555/status", headers=AUTH).json()
    assert body["platform"] == "salla" and body["mode"] == "shadow"
    assert body["has_meta_credentials"] is False
    assert body["onboarded"] is False  # no credentials and no dataset id
    assert body["webhooks_verified"] is True  # one signature_ok row (processing failed, but the signature verified)
    assert body["webhook_failures_7d"] == 1


def test_webhooks_verified_requires_a_verified_signature(db_session, client):
    c = create_tenant(db_session, name="C", platform="shopify", shop_domain="c.myshopify.com")
    path = "/v1/tenants/c.myshopify.com/status"
    assert client.get(path, headers=AUTH).json()["webhooks_verified"] is False
    add_delivery(db_session, c, signature_ok=False, processed_ok=False, error_type="invalid_signature")
    add_delivery(db_session, None, signature_ok=True)  # unattributed delivery (unknown tenant) never counts
    body = client.get(path, headers=AUTH).json()
    assert body["webhooks_verified"] is False and body["last_webhook_at"] is None
    add_delivery(db_session, c, signature_ok=True)
    body = client.get(path, headers=AUTH).json()
    assert body["webhooks_verified"] is True and body["last_webhook_at"] is not None
    assert body["last_capi_event_at"] is None


def test_inactive_tenant_not_onboarded(db_session, client, fernet_key):
    c = create_tenant(db_session, name="C", platform="shopify", shop_domain="c.myshopify.com", meta_dataset_id="9",
                      active=False)
    store_credential(db_session, c.id, "meta_capi_token", SECRET_TOKEN)
    body = client.get("/v1/tenants/c.myshopify.com/status", headers=AUTH).json()
    assert body["active"] is False and body["has_meta_credentials"] is True and body["onboarded"] is False


# --- /signal-health --------------------------------------------------------------------------------------

def test_signal_health_tenant_a(client):
    body = client.get("/v1/tenants/a.myshopify.com/signal-health", headers=AUTH).json()
    assert set(body) == {"tenant_key", "window", "orders", "capi_events", "would_have_sent", "open_incidents"}
    assert set(body["window"]) == {"start", "end", "days"} and body["window"]["days"] == 7
    assert body["orders"] == {"placed": 4, "cod": 3, "confirmed": 2, "delivered": 1, "refused": 1}
    assert body["capi_events"] == {"sent": 1, "shadow": 1, "failed": 1, "stale": 0, "late_delivery": 1}
    assert body["would_have_sent"] == 1
    assert [i["type"] for i in body["open_incidents"]] == ["webhook_failed"]
    assert set(body["open_incidents"][0]) == {"type", "opened_at"}


def test_signal_health_window_changes_counts(client):
    body = client.get("/v1/tenants/a.myshopify.com/signal-health?window_days=28", headers=AUTH).json()
    assert body["orders"]["placed"] == 5  # now includes the 20-day-old order
    body = client.get("/v1/tenants/a.myshopify.com/signal-health?window_days=1", headers=AUTH).json()
    assert body["orders"]["placed"] == 0  # the newest order was placed exactly 1 day before seeding


def test_signal_health_tenant_b_only_counts_its_own(client):
    body = client.get("/v1/tenants/555/signal-health", headers=AUTH).json()
    assert body["tenant_key"] == "555"
    assert body["orders"] == {"placed": 5, "cod": 5, "confirmed": 5, "delivered": 0, "refused": 0}
    assert body["capi_events"] == {"sent": 5, "shadow": 0, "failed": 0, "stale": 0, "late_delivery": 0}
    assert [i["type"] for i in body["open_incidents"]] == ["b_only_incident"]


# --- PII -------------------------------------------------------------------------------------------------

def test_no_pii_or_secrets_in_responses(client):
    blobs = []
    for key in ("a.myshopify.com", "555"):
        for suffix in ("status", "signal-health"):
            resp = client.get(f"/v1/tenants/{key}/{suffix}", headers=AUTH)
            assert resp.status_code == 200
            blobs.append(json.dumps(resp.json()) + resp.text)
    blob = "\n".join(blobs)
    for secret in (SECRET_EMAIL_HASH, SECRET_PHONE_HASH, SECRET_ORDER_ID, SECRET_IP, SECRET_UA, SECRET_TOKEN,
                   SECRET_PAYLOAD_HASH, encrypt_value(SECRET_IP)[:12], "ciphertext"):
        assert secret not in blob
    assert "@" not in blob.replace("a.myshopify.com", "")  # no email-like strings


def test_no_pii_in_logs(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get("/v1/tenants/a.myshopify.com/status", headers=AUTH)
        client.get("/v1/tenants/a.myshopify.com/signal-health", headers=AUTH)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (SECRET_EMAIL_HASH, SECRET_PHONE_HASH, SECRET_ORDER_ID, SECRET_IP, SECRET_UA, SECRET_TOKEN, TENANT_KEY):
        assert secret not in text


def test_read_only_no_rows_written(db_session, client):
    before = (db_session.query(Order).count(), db_session.query(CapiEvent).count(),
              db_session.query(WebhookDelivery).count(), db_session.query(Incident).count())
    client.get("/v1/tenants/a.myshopify.com/status", headers=AUTH)
    client.get("/v1/tenants/a.myshopify.com/signal-health", headers=AUTH)
    db_session.expire_all()
    after = (db_session.query(Order).count(), db_session.query(CapiEvent).count(),
             db_session.query(WebhookDelivery).count(), db_session.query(Incident).count())
    assert before == after


def test_service_app_includes_router():
    from src.ameen_workforce.service import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/tenants/{tenant_key}/status" in paths
    assert "/v1/tenants/{tenant_key}/signal-health" in paths
