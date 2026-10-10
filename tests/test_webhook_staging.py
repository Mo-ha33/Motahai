"""
test_webhook_staging.py — Durable webhook staging + replay, courier dedupe, OTO replay window, Bosta hardening.
"""

import asyncio
import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import webhook_routes
from src.ameen_workforce.capi_service import capi_sender
from src.ameen_workforce.credentials import FERNET_KEY_ENV, store_credential
from src.ameen_workforce.db import Incident, Order, StagedWebhook, WebhookDelivery, create_tenant
from src.ameen_workforce.scheduler import run_scheduler_tick
from src.ameen_workforce.service import app
from src.ameen_workforce.webhook_signatures import (
    BOSTA_MIN_SECRET_LENGTH, oto_timestamp_is_fresh, verify_bosta_auth
)
from src.ameen_workforce.webhook_staging import (
    BACKOFF, MAX_ATTEMPTS, STAGE_GRACE, process_staged, replay_staged_webhooks, stage_webhook
)
from tests.test_couriers import BOSTA_SECRET, OTO_SECRET, make_pending_order
from tests.test_webhook_routes import (
    EMAIL, PHONE, SHOPIFY_SECRET, SALLA_SECRET, SpySender, raw_json, shopify_headers, shopify_order
)


@pytest.fixture
def factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)


@pytest.fixture
def client(factory, fernet_key, monkeypatch):
    monkeypatch.setenv(webhook_routes.SHOPIFY_SECRET_ENV, SHOPIFY_SECRET)
    monkeypatch.setenv(webhook_routes.SALLA_SECRET_ENV, SALLA_SECRET)
    app.dependency_overrides[webhook_routes.get_session_factory_dep] = lambda: factory
    yield TestClient(app)
    app.dependency_overrides.pop(webhook_routes.get_session_factory_dep, None)


@pytest.fixture
def spy(monkeypatch):
    sender = SpySender()
    monkeypatch.setattr(capi_sender, "send_event", sender)
    return sender


@pytest.fixture
def lost_tasks(monkeypatch):
    """Simulates the process dying right after the 200: the background task never runs."""
    calls = []

    async def lost(*args, **kwargs):
        calls.append(args)
    monkeypatch.setattr(webhook_routes, "_process_in_background", lost)
    return calls


@pytest.fixture
def courier_tenant(db_session, fernet_key):
    tenant = create_tenant(db_session, name="EG Courier Shop", platform="shopify", shop_domain="eg-courier.myshopify.com",
                           meta_dataset_id="444", mode="live", settlement_hours=12.0, country="EG", currency="EGP")
    store_credential(db_session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-EG")
    store_credential(db_session, tenant.id, "bosta_webhook_secret", BOSTA_SECRET)
    return tenant


@pytest.fixture
def oto_tenant(db_session, fernet_key):
    tenant = create_tenant(db_session, name="SA OTO Shop", platform="salla", shop_domain="5550001",
                           meta_dataset_id="555", mode="live", settlement_hours=12.0, country="SA", currency="SAR")
    store_credential(db_session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-SA")
    store_credential(db_session, tenant.id, "oto_webhook_secret", OTO_SECRET)
    return tenant


def rows(factory, model):
    with factory() as s:
        return s.scalars(select(model).order_by(model.id)).all()


def oto_body(order_id, status="delivered", timestamp=None, secret=OTO_SECRET):
    timestamp = timestamp if timestamp is not None else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"{order_id}:{status}:{timestamp}".encode()
    sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
    return {"orderId": order_id, "status": status, "timestamp": timestamp, "signature": sig}


# --- staging before the 200 ---------------------------------------------------------------------------------

def test_verified_webhook_is_staged_encrypted_before_the_200(client, factory, live_tenant, lost_tasks):
    raw = raw_json(shopify_order())
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 200 and len(lost_tasks) == 1
    staged = rows(factory, StagedWebhook)
    assert len(staged) == 1 and staged[0].status == "pending" and staged[0].attempts == 0
    assert staged[0].tenant_id == live_tenant.id and staged[0].delivery_id == "wh-1"
    assert EMAIL not in staged[0].payload_ciphertext and PHONE not in staged[0].payload_ciphertext
    assert rows(factory, Order) == []  # nothing processed: the task was lost


@pytest.mark.asyncio
async def test_lost_background_task_is_replayed_by_the_scheduler(client, factory, live_tenant, lost_tasks, spy):
    raw = raw_json(shopify_order())
    assert client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw)).status_code == 200
    received = rows(factory, StagedWebhook)[0].received_at

    with factory() as s:  # inside the grace period the background task still owns the row
        assert (await replay_staged_webhooks(s, now=received + STAGE_GRACE - timedelta(seconds=1)))["due"] == 0

    summary = await run_scheduler_tick(session_factory=factory, now=received + STAGE_GRACE + timedelta(seconds=1))
    assert summary["jobs"]["replay_staged_webhooks"]["counts"]["processed"] == 1
    assert rows(factory, StagedWebhook) == []  # deleted once processed: no payload copy kept
    order = rows(factory, Order)[0]
    assert order.platform_order_id == "987654321" and order.current_status == "delivered"
    assert len(spy.calls) == 1


def test_successful_background_task_removes_the_staged_row(client, factory, live_tenant, spy):
    raw = raw_json(shopify_order())
    assert client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw)).status_code == 200
    assert rows(factory, StagedWebhook) == []
    assert rows(factory, WebhookDelivery)[0].processed_ok is True


def test_staging_failure_returns_503_so_the_platform_retries(client, factory, live_tenant, monkeypatch):
    monkeypatch.delenv(FERNET_KEY_ENV)  # vault unavailable -> cannot stage safely
    raw = raw_json(shopify_order())
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 503
    assert rows(factory, StagedWebhook) == [] and rows(factory, Order) == []


def test_background_failure_keeps_the_row_with_backoff_then_replay_succeeds(client, factory, live_tenant, monkeypatch,
                                                                            spy):
    real_process_webhook = webhook_routes.process_webhook

    async def boom(*a, **k):
        raise RuntimeError("transient")
    monkeypatch.setattr(webhook_routes, "process_webhook", boom)
    raw = raw_json(shopify_order())
    assert client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw)).status_code == 200
    staged = rows(factory, StagedWebhook)[0]
    assert staged.attempts == 1 and staged.status == "pending" and staged.last_error_type == "RuntimeError"
    assert rows(factory, Incident)[0].kind == "webhook_processing_error"

    monkeypatch.setattr(webhook_routes, "process_webhook", real_process_webhook)  # the transient problem is gone
    with factory() as s:
        counts = asyncio.run(replay_staged_webhooks(s, now=staged.next_attempt_at + timedelta(seconds=1)))
    assert counts["processed"] == 1 and rows(factory, StagedWebhook) == []
    assert rows(factory, Order)[0].current_status == "delivered"


@pytest.mark.asyncio
async def test_repeated_failures_end_as_dead_letter_without_pii(db_session, factory, live_tenant):
    staged_id = stage_webhook(db_session, live_tenant.id, "shopify", "orders/updated", shopify_order(), "wh-dead")

    async def boom(*a, **k):
        raise ValueError("always")

    now = datetime.now(timezone.utc)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with factory() as s:
            with pytest.raises(ValueError):
                await process_staged(s, staged_id, processor=boom, now=now)
            row = s.get(StagedWebhook, staged_id)
            assert row.attempts == attempt
            if attempt < MAX_ATTEMPTS:
                assert row.status == "pending" and row.next_attempt_at == now + BACKOFF[attempt - 1]
    row = rows(factory, StagedWebhook)[0]
    assert row.status == "dead" and row.payload_ciphertext == ""
    incident = rows(factory, Incident)[0]
    assert incident.kind == "webhook_dead_letter" and EMAIL not in incident.detail
    with factory() as s:  # dead rows are never replayed
        assert (await replay_staged_webhooks(s, now=now + timedelta(days=1)))["due"] == 0


# --- courier dedupe -------------------------------------------------------------------------------------------

def test_bosta_new_status_with_same_tracking_number_is_not_a_duplicate(client, factory, db_session, courier_tenant):
    make_pending_order(db_session, courier_tenant, order_id="ORD-TRK", status="pending")
    url = f"/webhooks/bosta/{courier_tenant.shop_domain}"
    headers = {"Authorization": BOSTA_SECRET}
    shipped = {"businessReference": "ORD-TRK", "state": 24, "trackingNumber": "TRK-1"}
    delivered = {"businessReference": "ORD-TRK", "state": 45, "cod": 850.0, "trackingNumber": "TRK-1"}

    assert client.post(url, json=shipped, headers=headers).status_code == 200
    assert client.post(url, json=delivered, headers=headers).status_code == 200
    db_session.expire_all()
    order = db_session.scalar(select(Order).where(Order.platform_order_id == "ORD-TRK"))
    assert order.current_status == "delivered"  # used to be dropped as DUPLICATE_DELIVERY

    assert client.post(url, json=delivered, headers=headers).status_code == 200  # exact redelivery
    bosta = [d for d in rows(factory, WebhookDelivery) if d.platform == "bosta"]
    assert [d.is_duplicate for d in bosta] == [False, False, True]


# --- OTO replay window --------------------------------------------------------------------------------------

def test_oto_timestamp_window_unit():
    now = 1_800_000_000.0
    assert oto_timestamp_is_fresh(str(int(now) - 60), 3600, 300, now=now)
    assert oto_timestamp_is_fresh(int((now - 60) * 1000), 3600, 300, now=now)  # milliseconds
    assert oto_timestamp_is_fresh(datetime.fromtimestamp(now - 60, timezone.utc).isoformat(), 3600, 300, now=now)
    assert oto_timestamp_is_fresh(datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  3600, 300, now=now)
    assert not oto_timestamp_is_fresh(str(int(now) - 3601), 3600, 300, now=now)  # too old
    assert not oto_timestamp_is_fresh(str(int(now) + 301), 3600, 300, now=now)  # too far in the future
    for bad in ("", "yesterday", None, True, "12:00"):
        assert not oto_timestamp_is_fresh(bad, 3600, 300, now=now)


def test_oto_stale_signed_timestamp_is_rejected(client, factory, oto_tenant):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = client.post(f"/webhooks/oto/{oto_tenant.shop_domain}", json=oto_body("O-1", timestamp=old))
    assert res.status_code == 401
    delivery = rows(factory, WebhookDelivery)[-1]
    assert delivery.error_type == "stale_timestamp" and delivery.signature_ok is True
    assert rows(factory, StagedWebhook) == []


def test_oto_max_age_is_configurable(client, factory, oto_tenant, monkeypatch, lost_tasks):
    monkeypatch.setenv(webhook_routes.OTO_MAX_AGE_ENV, str(3 * 3600))
    old = str(int(time.time()) - 2 * 3600)
    assert client.post(f"/webhooks/oto/{oto_tenant.shop_domain}", json=oto_body("O-2", timestamp=old)).status_code == 200


def test_oto_fresh_timestamp_is_accepted_and_staged(client, factory, oto_tenant, lost_tasks):
    res = client.post(f"/webhooks/oto/{oto_tenant.shop_domain}", json=oto_body("O-3"))
    assert res.status_code == 200 and len(rows(factory, StagedWebhook)) == 1


# --- Bosta hardening ----------------------------------------------------------------------------------------

def test_bosta_auth_rejects_short_secrets_and_accepts_bearer():
    strong = "x" * BOSTA_MIN_SECRET_LENGTH
    assert verify_bosta_auth(strong, strong)
    assert verify_bosta_auth(f"Bearer {strong}", strong)
    assert not verify_bosta_auth("short", "short")
    assert not verify_bosta_auth("Bearer ", strong)
    assert not verify_bosta_auth(strong + "y", strong)


def test_bosta_tenant_with_weak_secret_fails_closed(client, factory, db_session, courier_tenant):
    store_credential(db_session, courier_tenant.id, "bosta_webhook_secret", "weak123")
    res = client.post(f"/webhooks/bosta/{courier_tenant.shop_domain}",
                      json={"businessReference": "X", "state": 45}, headers={"Authorization": "weak123"})
    assert res.status_code == 401
    assert rows(factory, WebhookDelivery)[-1].error_type == "secret_too_weak"
