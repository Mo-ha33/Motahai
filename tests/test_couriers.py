"""
test_couriers.py — Courier tracking integration for Bosta & OTO (S2-5).
Tests status parsers, webhook authentication/signatures, order pipeline reconciliation,
12-hour settlement window scheduling, and FastAPI webhook routes.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import webhook_routes
from src.ameen_workforce.capi_service import DELIVERED_EVENT_NAME, MetaCAPISender, hash_email, hash_phone
from src.ameen_workforce.credentials import FERNET_KEY_ENV, store_credential
from src.ameen_workforce.db import CapiEvent, Order, OrderStatusEvent, Tenant, WebhookDelivery, create_tenant
from src.ameen_workforce.order_pipeline import process_webhook, send_due_events
from src.ameen_workforce.service import app
from src.ameen_workforce.webhook_listener import OrderWebhookProcessor, order_webhook_processor
from src.ameen_workforce.webhook_signatures import (
    BOSTA_AUTH_HEADER, OTO_SIGNATURE_HEADER, verify_bosta_auth, verify_oto_signature
)

EMAIL = "courier.customer@example.com"
PHONE = "01012345678"
NOW = datetime.now(timezone.utc)
PLACED_AT = (NOW - timedelta(hours=3)).isoformat()
BOSTA_SECRET = "bosta_api_key_secret_test_123"
OTO_SECRET = "oto_webhook_secret_key_test_456"


class SpySender(MetaCAPISender):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append({"pixel_id": pixel_id, "access_token": access_token, "payload": payload})
        return {"status": "success", "fbtrace_id": "TRACE_COURIER_1"}


def make_pending_order(session, tenant, order_id="order-cod-100", value=850.0, is_cod=True, status="pending"):
    order = Order(
        tenant_id=tenant.id,
        platform_order_id=order_id,
        current_status=status,
        value=value,
        currency="EGP" if tenant.country == "EG" else "SAR",
        is_cod=is_cod,
        email_hash=hash_email(EMAIL),
        phone_hash=hash_phone(PHONE, "EGP", tenant.country),
        created_at_platform=NOW - timedelta(hours=4),
    )
    session.add(order)
    session.commit()
    return order


# =============================================================================
# 1. Bosta Parser Unit Tests
# =============================================================================

def test_bosta_parser_delivered_state_45():
    payload = {
        "_id": "bosta_del_001",
        "trackingNumber": "TRK12345",
        "state": 45,
        "businessReference": "ORD-EG-999",
        "cod": 650.0,
        "isConfirmedDelivery": True,
        "timeStamp": int(NOW.timestamp() * 1000)
    }
    parsed = OrderWebhookProcessor.parse_bosta_delivery(payload)
    assert parsed["platform"] == "bosta"
    assert parsed["order_id"] == "ORD-EG-999"
    assert parsed["status"] == "delivered"
    assert parsed["state"] == 45
    assert parsed["cod_amount"] == 650.0
    assert parsed["is_cod"] is True
    assert parsed["tracking_number"] == "TRK12345"
    assert parsed["is_confirmed_delivery"] is True
    assert parsed["needs_order_context"] is True


def test_bosta_parser_delivered_string_and_subfield():
    payload = {
        "state": {"code": "45", "value": "Delivered"},
        "orderId": "ORD-EG-888",
        "cod": "420.50"
    }
    parsed = OrderWebhookProcessor.parse_bosta_delivery(payload)
    assert parsed["order_id"] == "ORD-EG-888"
    assert parsed["status"] == "delivered"
    assert parsed["cod_amount"] == 420.50


def test_bosta_parser_cancelled_states():
    for state in (46, 48, 49, 60, "46", "49", "canceled", "cancelled", "returned"):
        payload = {"businessReference": "ORD-1", "state": state}
        parsed = OrderWebhookProcessor.parse_bosta_delivery(payload)
        assert parsed["status"] == "cancelled", f"Failed for state {state}"


def test_bosta_parser_failed_states():
    for state in (47, 100, 101, "47", "exception", "failed", "damaged"):
        payload = {"businessReference": "ORD-1", "state": state}
        parsed = OrderWebhookProcessor.parse_bosta_delivery(payload)
        assert parsed["status"] == "failed_delivery", f"Failed for state {state}"


def test_bosta_parser_in_transit_states():
    for state in (10, 20, 21, 24, 25, 30, 40, 41):
        payload = {"businessReference": "ORD-1", "state": state}
        parsed = OrderWebhookProcessor.parse_bosta_delivery(payload)
        assert parsed["status"] == "shipped", f"Failed for state {state}"


def test_bosta_parser_nested_payload():
    payload = {
        "data": {
            "businessReference": "ORD-NESTED-1",
            "state": 45,
            "cod": 500.0,
            "trackingNumber": "TRK_NESTED"
        }
    }
    parsed = OrderWebhookProcessor.parse_bosta_delivery(payload)
    assert parsed["order_id"] == "ORD-NESTED-1"
    assert parsed["status"] == "delivered"
    assert parsed["cod_amount"] == 500.0


def test_bosta_parser_order_id_fallbacks():
    assert OrderWebhookProcessor.parse_bosta_delivery({"business_reference": "ref1"})["order_id"] == "ref1"
    assert OrderWebhookProcessor.parse_bosta_delivery({"order_id": "ref2"})["order_id"] == "ref2"
    assert OrderWebhookProcessor.parse_bosta_delivery({"reference": "ref3"})["order_id"] == "ref3"
    assert OrderWebhookProcessor.parse_bosta_delivery({"_id": "ref4"})["order_id"] == "ref4"
    assert OrderWebhookProcessor.parse_bosta_delivery({"trackingNumber": "ref5"})["order_id"] == "ref5"


# =============================================================================
# 2. OTO Parser Unit Tests
# =============================================================================

def test_oto_parser_delivered_status():
    payload = {
        "orderId": "OTO-KSA-100",
        "status": "delivered",
        "trackingNumber": "OTO_TRK_99",
        "deliveryCompany": "SMSA",
        "timestamp": "2026-10-10T01:00:00Z"
    }
    parsed = OrderWebhookProcessor.parse_oto_order_status(payload)
    assert parsed["platform"] == "oto"
    assert parsed["order_id"] == "OTO-KSA-100"
    assert parsed["status"] == "delivered"
    assert parsed["tracking_number"] == "OTO_TRK_99"
    assert parsed["delivery_company"] == "SMSA"
    assert parsed["needs_order_context"] is True


def test_oto_parser_status_variants():
    for status in ("DELIVERED", "completed", "تم التوصيل", "مكتمل"):
        payload = {"orderId": "OTO-1", "status": status}
        assert OrderWebhookProcessor.parse_oto_order_status(payload)["status"] == "delivered"

    for status in ("cancelled", "canceled", "returned", "rejected", "ملغي"):
        payload = {"orderId": "OTO-1", "status": status}
        assert OrderWebhookProcessor.parse_oto_order_status(payload)["status"] == "cancelled"

    for status in ("failed", "shipmentError", "undelivered", "delivery_failed", "error"):
        payload = {"orderId": "OTO-1", "status": status}
        assert OrderWebhookProcessor.parse_oto_order_status(payload)["status"] == "failed_delivery"

    for status in ("shipmentProcessing", "picked_up", "out_for_delivery", "in_transit"):
        payload = {"orderId": "OTO-1", "status": status}
        assert OrderWebhookProcessor.parse_oto_order_status(payload)["status"] == "shipped"


def test_oto_parser_nested_payload_and_fallbacks():
    payload = {"data": {"order_id": "OTO-NESTED-1", "status": "delivered"}}
    parsed = OrderWebhookProcessor.parse_oto_order_status(payload)
    assert parsed["order_id"] == "OTO-NESTED-1"
    assert parsed["status"] == "delivered"

    assert OrderWebhookProcessor.parse_oto_order_status({"reference_id": "r1"})["order_id"] == "r1"
    assert OrderWebhookProcessor.parse_oto_order_status({"orderNumber": "r2"})["order_id"] == "r2"


# =============================================================================
# 3. Signature & Auth Verifiers Unit Tests
# =============================================================================

def test_oto_signature_verification():
    order_id = "12345"
    status = "delivered"
    timestamp = "2026-10-10T01:00:00Z"
    msg = f"{order_id}:{status}:{timestamp}".encode("utf-8")
    sig = base64.b64encode(hmac.new(OTO_SECRET.encode("utf-8"), msg, hashlib.sha256).digest()).decode("ascii")

    assert verify_oto_signature(order_id, status, timestamp, sig, OTO_SECRET) is True
    assert verify_oto_signature(order_id, status, timestamp, "wrong-sig", OTO_SECRET) is False
    assert verify_oto_signature(order_id, status, timestamp, sig, "wrong-secret") is False
    assert verify_oto_signature(order_id, "cancelled", timestamp, sig, OTO_SECRET) is False
    assert verify_oto_signature(None, status, timestamp, sig, OTO_SECRET) is False


def test_bosta_auth_verification():
    assert verify_bosta_auth(BOSTA_SECRET, BOSTA_SECRET) is True
    assert verify_bosta_auth(f"Bearer {BOSTA_SECRET}", BOSTA_SECRET) is True
    assert verify_bosta_auth(f"Basic {BOSTA_SECRET}", BOSTA_SECRET) is True
    assert verify_bosta_auth("wrong-key", BOSTA_SECRET) is False
    assert verify_bosta_auth(None, BOSTA_SECRET) is False
    assert verify_bosta_auth(BOSTA_SECRET, "") is False


# =============================================================================
# 4. Order Pipeline Integration (12-hour Settlement & Reconciliation)
# =============================================================================

@pytest.fixture
def settlement_tenant(db_session, fernet_key):
    """Tenant with default 12-hour settlement window."""
    tenant = create_tenant(db_session, name="COD Egypt Store", platform="shopify",
                           shop_domain="egypt-cod.myshopify.com", meta_dataset_id="DATASET_EG",
                           mode="live", settlement_hours=12.0, country="EG", currency="EGP")
    store_credential(db_session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-EG")
    return tenant


@pytest.fixture
def oto_tenant(db_session, fernet_key):
    """Tenant with default 12-hour settlement window for OTO (KSA)."""
    tenant = create_tenant(db_session, name="KSA Salla Store", platform="salla",
                           shop_domain="ksa-salla.com", meta_dataset_id="DATASET_KSA",
                           mode="live", settlement_hours=12.0, country="SA", currency="SAR")
    store_credential(db_session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-KSA")
    return tenant


@pytest.mark.asyncio
async def test_bosta_delivery_enters_12h_settlement_window(db_session, settlement_tenant):
    order = make_pending_order(db_session, settlement_tenant, order_id="ORD-BOSTA-1", status="shipped")
    sender = SpySender()

    bosta_payload = {
        "businessReference": "ORD-BOSTA-1",
        "state": 45,
        "cod": 850.0,
        "trackingNumber": "BOSTA_TRK_001"
    }

    t0 = NOW
    res = await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                                bosta_payload, delivery_id="deliv-bosta-1", sender=sender, now=t0)

    assert res["action"] == "SCHEDULED"
    assert res["capi_status"] == "scheduled"
    assert res["order_id"] == "ORD-BOSTA-1"

    # Order transitioned to delivered
    db_session.refresh(order)
    assert order.current_status == "delivered"
    assert order.delivered_at is not None
    assert order.delivered_at == t0

    # Scheduled 12 hours out
    due_at = datetime.fromisoformat(res["due_at"])
    expected_due = t0 + timedelta(hours=12)
    assert abs((due_at - expected_due).total_seconds()) < 5

    # CAPI sender has NOT been dispatched yet (held in 12h settlement)
    assert len(sender.calls) == 0

    # Event exists in DB with status scheduled
    events = db_session.scalars(select(CapiEvent).where(CapiEvent.order_id == order.id)).all()
    assert len(events) == 1
    assert events[0].status == "scheduled"
    assert events[0].event_name == DELIVERED_EVENT_NAME
    assert events[0].due_at == due_at


@pytest.mark.asyncio
async def test_oto_delivery_enters_12h_settlement_window(db_session, oto_tenant):
    order = make_pending_order(db_session, oto_tenant, order_id="ORD-OTO-1", value=320.0, status="pending")
    sender = SpySender()

    oto_payload = {
        "orderId": "ORD-OTO-1",
        "status": "delivered",
        "trackingNumber": "OTO_SA_001",
        "deliveryCompany": "Aramex"
    }

    t0 = NOW
    res = await process_webhook(db_session, oto_tenant, "oto", "orderStatus",
                                oto_payload, delivery_id="deliv-oto-1", sender=sender, now=t0)

    assert res["action"] == "SCHEDULED"
    assert res["capi_status"] == "scheduled"
    assert res["order_id"] == "ORD-OTO-1"

    db_session.refresh(order)
    assert order.current_status == "delivered"
    assert order.delivered_at == t0

    due_at = datetime.fromisoformat(res["due_at"])
    assert abs((due_at - (t0 + timedelta(hours=12))).total_seconds()) < 5
    assert len(sender.calls) == 0


@pytest.mark.asyncio
async def test_courier_delivery_dispatches_after_settlement_elapsed(db_session, settlement_tenant):
    order = make_pending_order(db_session, settlement_tenant, order_id="ORD-BOSTA-SWEEP", status="shipped")
    sender = SpySender()

    t0 = NOW
    await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                                {"businessReference": "ORD-BOSTA-SWEEP", "state": 45}, sender=sender, now=t0)
    assert len(sender.calls) == 0

    # After 6 hours: not yet due
    counts_6h = await send_due_events(db_session, now=t0 + timedelta(hours=6), sender=sender)
    assert counts_6h["due"] == 0
    assert len(sender.calls) == 0

    # After 12.5 hours: due and dispatched
    counts_12h = await send_due_events(db_session, now=t0 + timedelta(hours=12, minutes=30), sender=sender)
    assert counts_12h["due"] == 1
    assert counts_12h["sent"] == 1
    assert len(sender.calls) == 1

    call = sender.calls[0]
    assert call["pixel_id"] == "DATASET_EG"
    assert call["payload"]["data"][0]["event_name"] == DELIVERED_EVENT_NAME
    assert call["payload"]["data"][0]["custom_data"]["value"] == 850.0

    event = db_session.scalars(select(CapiEvent).where(CapiEvent.order_id == order.id)).one()
    assert event.status == "sent"


@pytest.mark.asyncio
async def test_courier_delivery_immediate_dispatch_when_zero_settlement(db_session, live_tenant):
    """When settlement_hours is 0, delivery dispatches immediately in-line."""
    order = make_pending_order(db_session, live_tenant, order_id="ORD-FAST-1", status="shipped")
    sender = SpySender()

    res = await process_webhook(db_session, live_tenant, "bosta", "delivery_update",
                                {"businessReference": "ORD-FAST-1", "state": 45}, sender=sender)
    assert res["action"] == "SENT"
    assert res["capi_status"] == "sent"
    assert len(sender.calls) == 1
    assert sender.calls[0]["payload"]["data"][0]["event_name"] == DELIVERED_EVENT_NAME


@pytest.mark.asyncio
async def test_cancellation_during_settlement_window_suppresses_conversion(db_session, settlement_tenant):
    """
    If customer refuses package or item is returned during the 12-hour window,
    bosta state 46 (returned) transitions the order to cancelled, and send_due_events suppresses conversion.
    """
    order = make_pending_order(db_session, settlement_tenant, order_id="ORD-RETURN-1", status="shipped")
    sender = SpySender()

    t0 = NOW
    # 1. Delivery update arrives
    await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                          {"businessReference": "ORD-RETURN-1", "state": 45}, sender=sender, now=t0)

    # 2. Package returned 2 hours later
    cancel_payload = {"businessReference": "ORD-RETURN-1", "state": 46}
    cancel_res = await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                                       cancel_payload, delivery_id="cancel-1", sender=sender, now=t0 + timedelta(hours=2))

    assert cancel_res["action"] == "SUPPRESSED"
    db_session.refresh(order)
    assert order.current_status == "cancelled"

    # 3. 12 hours later, send_due_events evaluates current order
    counts = await send_due_events(db_session, now=t0 + timedelta(hours=13), sender=sender)
    assert counts["due"] == 1
    assert counts["stale"] == 1
    assert counts["sent"] == 0
    assert len(sender.calls) == 0  # Zero CAPI calls emitted!

    event = db_session.scalars(select(CapiEvent).where(CapiEvent.order_id == order.id)).one()
    assert event.status == "stale"
    assert event.error_type == "no_longer_eligible"


@pytest.mark.asyncio
async def test_courier_delivery_for_unknown_order_needs_context(db_session, settlement_tenant):
    sender = SpySender()
    res = await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                                {"businessReference": "UNKNOWN-999", "state": 45}, sender=sender)
    assert res["action"] == "NEEDS_ORDER_CONTEXT"
    assert len(sender.calls) == 0
    assert len(db_session.scalars(select(CapiEvent)).all()) == 0


@pytest.mark.asyncio
async def test_courier_late_shipped_does_not_regress_delivered_order(db_session, settlement_tenant):
    order = make_pending_order(db_session, settlement_tenant, order_id="ORD-REGRESS-1", status="pending")
    sender = SpySender()

    # Deliver via Bosta
    await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                          {"businessReference": "ORD-REGRESS-1", "state": 45}, sender=sender)
    db_session.refresh(order)
    assert order.current_status == "delivered"

    # Out-of-order in-transit update
    await process_webhook(db_session, settlement_tenant, "bosta", "delivery_update",
                          {"businessReference": "ORD-REGRESS-1", "state": 30}, sender=sender, delivery_id="late-wh")
    db_session.refresh(order)
    assert order.current_status == "delivered"


@pytest.mark.asyncio
async def test_courier_bosta_backfills_zero_order_value(db_session, live_tenant):
    order = make_pending_order(db_session, live_tenant, order_id="ORD-ZERO-1", value=0.0, status="shipped")
    sender = SpySender()

    res = await process_webhook(db_session, live_tenant, "bosta", "delivery_update",
                                {"businessReference": "ORD-ZERO-1", "state": 45, "cod": 550.0}, sender=sender)
    assert res["action"] == "SENT"
    db_session.refresh(order)
    assert order.value == 550.0
    assert sender.calls[0]["payload"]["data"][0]["custom_data"]["value"] == 550.0


# =============================================================================
# 5. Stateless OrderWebhookProcessor.handle_order_update
# =============================================================================

@pytest.mark.asyncio
async def test_stateless_bosta_with_order_context():
    context = {"total_price": 500.0, "currency": "EGP", "is_cod": True, "email": EMAIL, "phone": PHONE}
    res = await order_webhook_processor.handle_order_update(
        platform="bosta",
        payload={"businessReference": "ORD-STATELESS", "state": 45},
        order_context=context
    )
    assert res["status"] == "processed"
    assert res["d005_decision"]["action"] == "READY_TO_EMIT"
    assert res["parsed_order"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_stateless_bosta_without_order_context():
    res = await order_webhook_processor.handle_order_update(
        platform="bosta",
        payload={"businessReference": "ORD-STATELESS", "state": 45}
    )
    assert res["status"] == "processed"
    assert res["d005_decision"]["action"] == "NEEDS_ORDER_CONTEXT"


@pytest.mark.asyncio
async def test_stateless_oto_with_order_context():
    context = {"total_price": 250.0, "currency": "SAR", "is_cod": True, "email": EMAIL, "phone": PHONE}
    res = await order_webhook_processor.handle_order_update(
        platform="oto",
        payload={"orderId": "ORD-STATELESS-OTO", "status": "delivered"},
        order_context=context
    )
    assert res["status"] == "processed"
    assert res["d005_decision"]["action"] == "READY_TO_EMIT"


# =============================================================================
# 6. FastAPI Webhook Routes (/webhooks/bosta & /webhooks/oto)
# =============================================================================

@pytest.fixture
def client_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)


@pytest.fixture
def courier_client(client_factory, monkeypatch):
    monkeypatch.setenv(webhook_routes.BOSTA_SECRET_ENV, BOSTA_SECRET)
    monkeypatch.setenv(webhook_routes.OTO_SECRET_ENV, OTO_SECRET)
    app.dependency_overrides[webhook_routes.get_session_factory_dep] = lambda: client_factory
    yield TestClient(app)
    app.dependency_overrides.pop(webhook_routes.get_session_factory_dep, None)


def test_bosta_route_accepts_valid_auth(courier_client, db_session, settlement_tenant):
    make_pending_order(db_session, settlement_tenant, order_id="ORD-API-BOSTA-1", status="shipped")

    payload = {"businessReference": "ORD-API-BOSTA-1", "state": 45, "cod": 850.0}
    headers = {
        "Content-Type": "application/json",
        BOSTA_AUTH_HEADER: BOSTA_SECRET,
        "X-Shop-Domain": settlement_tenant.shop_domain
    }
    response = courier_client.post("/webhooks/bosta", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_bosta_route_finds_tenant_by_order_in_db(courier_client, db_session, settlement_tenant):
    make_pending_order(db_session, settlement_tenant, order_id="ORD-API-BOSTA-2", status="shipped")

    payload = {"businessReference": "ORD-API-BOSTA-2", "state": 45}
    headers = {"Content-Type": "application/json", BOSTA_AUTH_HEADER: BOSTA_SECRET}
    # No shop domain in header/query; route resolves tenant from order in DB
    response = courier_client.post("/webhooks/bosta", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_bosta_route_rejects_invalid_auth(courier_client):
    payload = {"businessReference": "ORD-1", "state": 45}
    headers = {"Content-Type": "application/json", BOSTA_AUTH_HEADER: "wrong-secret"}
    response = courier_client.post("/webhooks/bosta", json=payload, headers=headers)
    assert response.status_code == 401
    assert "Invalid webhook authentication" in response.json()["detail"]


def test_oto_route_accepts_valid_signature(courier_client, db_session, oto_tenant):
    make_pending_order(db_session, oto_tenant, order_id="ORD-API-OTO-1", status="pending")

    order_id = "ORD-API-OTO-1"
    status = "delivered"
    timestamp = "2026-10-10T01:00:00Z"
    msg = f"{order_id}:{status}:{timestamp}".encode("utf-8")
    sig = base64.b64encode(hmac.new(OTO_SECRET.encode("utf-8"), msg, hashlib.sha256).digest()).decode("ascii")

    payload = {
        "orderId": order_id,
        "status": status,
        "timestamp": timestamp,
        "signature": sig
    }
    response = courier_client.post(f"/webhooks/oto/{oto_tenant.shop_domain}", json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_oto_route_rejects_invalid_signature(courier_client, oto_tenant):
    payload = {
        "orderId": "ORD-1",
        "status": "delivered",
        "timestamp": "2026-10-10T01:00:00Z",
        "signature": "invalid-sig"
    }
    response = courier_client.post(f"/webhooks/oto/{oto_tenant.shop_domain}", json=payload)
    assert response.status_code == 401
    assert "Invalid webhook signature" in response.json()["detail"]


def test_courier_route_unknown_shop_or_order(courier_client):
    headers = {"Content-Type": "application/json", BOSTA_AUTH_HEADER: BOSTA_SECRET}
    response = courier_client.post("/webhooks/bosta", json={"businessReference": "NON-EXISTENT"}, headers=headers)
    assert response.status_code == 404
