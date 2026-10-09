"""
test_confirmed_order.py — Unit tests for S2-2 ConfirmedOrder Signal Specialist.

Covers:
1. Event definition: ConfirmedOrder, confirmed_<order_id>, EVENT_TYPES
2. CAPI service eligibility & payload construction (ph, em, fbp, fbc, client_ip, client_user_agent, full value)
3. Webhook parsing for Shopify tags containing 'confirmed'
4. Webhook parsing for Salla status ('confirmed' / 'in_review') and tags
5. Full pipeline integration for Shopify & Salla (SENT / SHADOW)
6. Idempotency guarantee: ConfirmedOrder never fires more than once per order
7. 3-Step Signal Ladder progression: ConfirmedOrder -> DeliveredPurchase
8. Direct bot / call confirmation trigger: emit_confirmed_order
"""

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from src.ameen_workforce.capi_service import (
    CONFIRMED_EVENT,
    CONFIRMED_EVENT_NAME,
    CONFIRMED_STATUSES,
    DELIVERED_EVENT,
    DELIVERED_EVENT_NAME,
    EVENT_TYPES,
    MetaCAPISender,
    hash_sha256,
)
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import CapiEvent, Order, create_tenant
from src.ameen_workforce.order_pipeline import (
    emit_confirmed_order,
    process_webhook,
)
from src.ameen_workforce.webhook_listener import OrderWebhookProcessor

NOW = datetime.now(timezone.utc)
IP = "197.38.45.12"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
EMAIL = "customer@example.com"
PHONE = "01012345678"
ORDER_ID = "ORD-CONF-101"


class FakeSender(MetaCAPISender):
    """Real D-005 logic, in-memory call capture."""
    def __init__(self, results=None):
        super().__init__()
        self.calls = []
        self.results = list(results or [])

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append({"pixel_id": pixel_id, "access_token": access_token, "payload": payload})
        if self.results:
            return self.results.pop(0)
        return {"status": "success", "fbtrace_id": "TRACE-CONFIRMED-1"}

    def event(self, index=0):
        return self.calls[index]["payload"]["data"][0]


@pytest.fixture
def live_shop(db_session, fernet_key):
    tenant = create_tenant(
        db_session,
        name="Confirmed Live Store",
        platform="shopify",
        shop_domain="confirmed-live.myshopify.com",
        meta_dataset_id="DATASET-CONF-1",
        mode="live",
        settlement_hours=0,
    )
    store_credential(db_session, tenant.id, "meta_capi_token", "TOKEN-CONF-1")
    return tenant


@pytest.fixture
def shadow_shop(db_session):
    return create_tenant(
        db_session,
        name="Confirmed Shadow Store",
        platform="shopify",
        shop_domain="confirmed-shadow.myshopify.com",
        meta_dataset_id="DATASET-CONF-2",
        mode="shadow",
        settlement_hours=0,
    )


@pytest.fixture
def salla_shop(db_session, fernet_key):
    tenant = create_tenant(
        db_session,
        name="Confirmed Salla Store",
        platform="salla",
        shop_domain="1234567",
        meta_dataset_id="DATASET-SALLA-1",
        mode="live",
        settlement_hours=0,
    )
    store_credential(db_session, tenant.id, "meta_capi_token", "TOKEN-SALLA-1")
    return tenant


def shopify_payload(tags=None, financial_status="pending", fulfillment_status=None, **kw):
    order = {
        "id": ORDER_ID,
        "financial_status": financial_status,
        "fulfillment_status": fulfillment_status,
        "payment_gateway_names": ["Cash on Delivery (COD)"],
        "total_price": "1250.00",
        "currency": "EGP",
        "created_at": (NOW - timedelta(hours=3)).isoformat(),
        "updated_at": NOW.isoformat(),
        "tags": tags,
        "customer": {"id": 8801, "email": EMAIL, "phone": PHONE},
        "shipping_address": {"phone": PHONE, "first_name": "Tariq", "last_name": "Nour", "city": "Cairo"},
        "client_details": {"browser_ip": IP, "user_agent": UA},
        "note_attributes": [
            {"name": "_mt_fbp", "value": "fb.1.1609459200000.1234567890"},
            {"name": "_mt_fbc", "value": "fb.1.1609459200000.IwAR1234567890"},
        ],
    }
    order.update(kw)
    return order


def salla_payload(status_slug="confirmed", tags=None, **kw):
    data = {
        "id": ORDER_ID,
        "status": {"slug": status_slug, "name": status_slug},
        "payment_method": "cash on delivery",
        "total": 600.0,
        "currency": "SAR",
        "created_at": (NOW - timedelta(hours=2)).isoformat(),
        "updated_at": NOW.isoformat(),
        "tags": tags,
        "customer": {"id": 9901, "email": EMAIL, "mobile": "0501234567"},
        "shipping": {"address": {"city": "Riyadh", "country_code": "SA"}},
        "client_ip": IP,
        "user_agent": UA,
    }
    data.update(kw)
    return {"data": data}


# =============================================================================
# 1. Event Type & Metadata Tests
# =============================================================================

def test_confirmed_event_type_definition():
    assert CONFIRMED_EVENT_NAME == "ConfirmedOrder"
    assert CONFIRMED_EVENT.name == "ConfirmedOrder"
    assert CONFIRMED_EVENT.id_prefix == "confirmed_"
    assert CONFIRMED_EVENT.event_id("98765") == "confirmed_98765"
    assert "ConfirmedOrder" in EVENT_TYPES
    assert EVENT_TYPES["ConfirmedOrder"] == CONFIRMED_EVENT
    assert "confirmed" in CONFIRMED_STATUSES
    assert "in_review" in CONFIRMED_STATUSES


# =============================================================================
# 2. CAPI Service Unit Tests
# =============================================================================

def test_process_confirmed_order_event_builds_full_payload():
    sender = MetaCAPISender()
    res = sender.process_confirmed_order_event(
        order_id="1001",
        status="pending",
        value=1500.0,
        currency="EGP",
        is_cod=True,
        email="test@example.com",
        phone="01012345678",
        fbp="fb.1.123.456",
        fbc="fb.1.123.789",
        client_ip=IP,
        user_agent=UA,
        is_confirmed=True,
        now=NOW.timestamp(),
    )
    assert res["action"] == "READY_TO_EMIT"
    assert res["event_name"] == "ConfirmedOrder"
    assert res["event_id"] == "confirmed_1001"

    data = res["payload"]["data"][0]
    assert data["event_name"] == "ConfirmedOrder"
    assert data["event_id"] == "confirmed_1001"
    assert data["custom_data"]["value"] == 1500.0
    assert data["custom_data"]["currency"] == "EGP"
    assert data["custom_data"]["order_id"] == "1001"

    # User matching data
    user_data = data["user_data"]
    assert user_data["em"] == [hash_sha256("test@example.com")]
    assert len(user_data["ph"][0]) == 64
    assert user_data["fbp"] == "fb.1.123.456"
    assert user_data["fbc"] == "fb.1.123.789"
    assert user_data["client_ip_address"] == IP
    assert user_data["client_user_agent"] == UA


def test_confirmed_order_suppressed_for_cancelled_or_refunded():
    sender = MetaCAPISender()
    for bad_status in ("cancelled", "canceled", "refunded", "voided"):
        res = sender.process_confirmed_order_event(
            order_id="1002",
            status=bad_status,
            value=500.0,
            currency="EGP",
            is_confirmed=True,
        )
        assert res["action"] == "SUPPRESSED"


def test_confirmed_order_suppressed_for_zero_or_negative_value():
    sender = MetaCAPISender()
    for zero_val in (0.0, -10.0):
        res = sender.process_confirmed_order_event(
            order_id="1003",
            status="pending",
            value=zero_val,
            currency="EGP",
            is_confirmed=True,
        )
        assert res["action"] == "SUPPRESSED"


def test_confirmed_order_deferred_if_not_confirmed():
    sender = MetaCAPISender()
    res = sender.process_confirmed_order_event(
        order_id="1004",
        status="pending",
        value=500.0,
        currency="EGP",
        is_confirmed=False,
    )
    assert res["action"] == "DEFERRED"
    assert res["held_event"] == "ConfirmedOrder"


# =============================================================================
# 3. Webhook Parser Tests (Tag & Status Triggers)
# =============================================================================

@pytest.mark.parametrize("tag_input", [
    "confirmed",
    "CONFIRMED",
    "confirmed, priority",
    "call_confirmed, express",
    "whatsapp_confirmed",
    ["confirmed", "vip"],
    ["order_confirmed"],
])
def test_shopify_parser_detects_confirmed_tags(tag_input):
    payload = shopify_payload(tags=tag_input)
    parsed = OrderWebhookProcessor.parse_shopify_order(payload)
    assert parsed["is_confirmed"] is True
    assert parsed["order_total"] == 1250.0


def test_shopify_parser_tag_list_and_status_confirmed():
    # Via tag_list array
    p1 = shopify_payload(tags=None, tag_list=["confirmed"])
    assert OrderWebhookProcessor.parse_shopify_order(p1)["is_confirmed"] is True

    # Via explicit status 'confirmed'
    p2 = shopify_payload(tags=None, status="confirmed")
    assert OrderWebhookProcessor.parse_shopify_order(p2)["is_confirmed"] is True

    # Unconfirmed order
    p3 = shopify_payload(tags="urgent, vip")
    assert OrderWebhookProcessor.parse_shopify_order(p3)["is_confirmed"] is False


@pytest.mark.parametrize("status_slug", [
    "confirmed",
    "in_review",
    "under_review",
    "مؤكد",
    "قيد المراجعة",
    "تحت المراجعة",
])
def test_salla_parser_detects_confirmed_and_in_review(status_slug):
    payload = salla_payload(status_slug=status_slug)
    parsed = OrderWebhookProcessor.parse_salla_order(payload)
    assert parsed["is_confirmed"] is True
    assert parsed["order_total"] == 600.0


def test_salla_parser_detects_tags():
    p1 = salla_payload(status_slug="pending", tags=["confirmed"])
    assert OrderWebhookProcessor.parse_salla_order(p1)["is_confirmed"] is True

    p2 = salla_payload(status_slug="pending", tags="call_confirmed")
    assert OrderWebhookProcessor.parse_salla_order(p2)["is_confirmed"] is True

    p3 = salla_payload(status_slug="pending", tags=None)
    assert OrderWebhookProcessor.parse_salla_order(p3)["is_confirmed"] is False


# =============================================================================
# 4. Pipeline Integration Tests (Shopify)
# =============================================================================

@pytest.mark.asyncio
async def test_pipeline_shopify_confirmed_tag_emits_confirmed_order(db_session, live_shop):
    sender = FakeSender()
    payload = shopify_payload(tags="confirmed, bot")
    result = await process_webhook(db_session, live_shop, "shopify", "orders/updated", payload, sender=sender)

    assert result["action"] == "SENT"
    assert result["capi_status"] == "sent"
    assert len(sender.calls) == 1

    ev = sender.event(0)
    assert ev["event_name"] == "ConfirmedOrder"
    assert ev["event_id"] == f"confirmed_{ORDER_ID}"
    assert ev["custom_data"]["value"] == 1250.0
    assert ev["custom_data"]["currency"] == "EGP"
    assert ev["user_data"]["fbp"] == "fb.1.1609459200000.1234567890"
    assert ev["user_data"]["fbc"] == "fb.1.1609459200000.IwAR1234567890"
    assert ev["user_data"]["client_ip_address"] == IP
    assert ev["user_data"]["client_user_agent"] == UA

    # Database verification
    capi_row = db_session.scalar(
        select(CapiEvent).where(CapiEvent.tenant_id == live_shop.id, CapiEvent.event_name == "ConfirmedOrder")
    )
    assert capi_row is not None
    assert capi_row.status == "sent"
    assert capi_row.event_id == f"confirmed_{ORDER_ID}"

    order_row = db_session.scalar(
        select(Order).where(Order.tenant_id == live_shop.id, Order.platform_order_id == ORDER_ID)
    )
    assert order_row.confirmed_at is not None
    assert order_row.order_total == 1250.0


@pytest.mark.asyncio
async def test_pipeline_confirmed_order_idempotency(db_session, live_shop):
    sender = FakeSender()
    payload = shopify_payload(tags="confirmed")

    # 1. First webhook: emits ConfirmedOrder
    first = await process_webhook(db_session, live_shop, "shopify", "orders/updated", payload, sender=sender)
    assert first["action"] == "SENT"
    assert len(sender.calls) == 1

    # 2. Duplicate exact webhook (different delivery ID or payload update)
    second_payload = shopify_payload(tags="confirmed", note="Customer changed delivery time")
    second = await process_webhook(db_session, live_shop, "shopify", "orders/updated", second_payload, sender=sender)

    assert second["action"] == "ALREADY_EMITTED"
    # Never dispatched more than once!
    assert len(sender.calls) == 1

    rows = db_session.scalars(
        select(CapiEvent).where(CapiEvent.tenant_id == live_shop.id, CapiEvent.event_name == "ConfirmedOrder")
    ).all()
    assert len(rows) == 1


# =============================================================================
# 5. Pipeline Integration Tests (Salla)
# =============================================================================

@pytest.mark.asyncio
async def test_pipeline_salla_confirmed_status_emits_event(db_session, salla_shop):
    sender = FakeSender()
    payload = salla_payload(status_slug="confirmed")
    result = await process_webhook(db_session, salla_shop, "salla", "order.status.updated", payload, sender=sender)

    assert result["action"] == "SENT"
    assert result["capi_status"] == "sent"
    assert len(sender.calls) == 1

    ev = sender.event(0)
    assert ev["event_name"] == "ConfirmedOrder"
    assert ev["event_id"] == f"confirmed_{ORDER_ID}"
    assert ev["custom_data"]["value"] == 600.0
    assert ev["custom_data"]["currency"] == "SAR"


@pytest.mark.asyncio
async def test_pipeline_salla_in_review_status_emits_event(db_session, salla_shop):
    sender = FakeSender()
    payload = salla_payload(status_slug="in_review")
    result = await process_webhook(db_session, salla_shop, "salla", "order.status.updated", payload, sender=sender)

    assert result["action"] == "SENT"
    ev = sender.event(0)
    assert ev["event_name"] == "ConfirmedOrder"


# =============================================================================
# 6. Ladder Progression: ConfirmedOrder -> DeliveredPurchase
# =============================================================================

@pytest.mark.asyncio
async def test_ladder_confirmed_order_then_delivered_purchase(db_session, live_shop):
    """
    Validates Rule D-005 Signal Ladder:
    - Step 2: ConfirmedOrder fires when merchant tags 'confirmed' (order still COD pending)
    - Step 3: DeliveredPurchase fires when courier marks delivered & paid
    Both events coexist in CAPI and capi_events without conflict.
    """
    sender = FakeSender()

    # Step 2: Confirmation
    step2_payload = shopify_payload(
        tags="confirmed",
        financial_status="pending",
        fulfillment_status=None,
    )
    res2 = await process_webhook(db_session, live_shop, "shopify", "orders/updated", step2_payload, sender=sender)
    assert res2["action"] == "SENT"
    assert len(sender.calls) == 1
    assert sender.event(0)["event_name"] == "ConfirmedOrder"

    # Step 3: Delivery
    step3_payload = shopify_payload(
        tags="confirmed",
        financial_status="paid",
        fulfillment_status="fulfilled",
    )
    res3 = await process_webhook(db_session, live_shop, "shopify", "orders/updated", step3_payload, sender=sender)
    assert res3["action"] == "SENT"
    assert len(sender.calls) == 2
    assert sender.event(1)["event_name"] == "DeliveredPurchase"
    assert sender.event(1)["event_id"] == f"delivered_{ORDER_ID}"

    # Both events recorded in capi_events
    events_in_db = db_session.scalars(
        select(CapiEvent).where(CapiEvent.tenant_id == live_shop.id).order_by(CapiEvent.id)
    ).all()
    event_names = [e.event_name for e in events_in_db]
    assert event_names == ["ConfirmedOrder", "DeliveredPurchase"]


# =============================================================================
# 7. Shadow Mode & Direct Bot Trigger Tests
# =============================================================================

@pytest.mark.asyncio
async def test_pipeline_confirmed_order_shadow_mode(db_session, shadow_shop):
    sender = FakeSender()
    payload = shopify_payload(tags="confirmed")
    result = await process_webhook(db_session, shadow_shop, "shopify", "orders/updated", payload, sender=sender)

    assert result["action"] == "SHADOW"
    assert result["capi_status"] == "shadow"
    assert len(sender.calls) == 0  # No HTTP call in shadow mode

    row = db_session.scalar(
        select(CapiEvent).where(CapiEvent.tenant_id == shadow_shop.id, CapiEvent.event_name == "ConfirmedOrder")
    )
    assert row is not None
    assert row.status == "shadow"


@pytest.mark.asyncio
async def test_emit_confirmed_order_direct_trigger(db_session, live_shop):
    """
    Tests direct confirmation by WhatsApp bot or call-center without a platform webhook.
    """
    sender = FakeSender()
    # 1. Create order in DB (unconfirmed)
    initial_payload = shopify_payload(tags=None, financial_status="pending")
    await process_webhook(db_session, live_shop, "shopify", "orders/create", initial_payload, sender=sender)
    assert len(sender.calls) == 0

    # 2. WhatsApp bot calls emit_confirmed_order
    res = await emit_confirmed_order(db_session, live_shop, ORDER_ID, sender=sender)
    assert res["action"] == "SENT"
    assert len(sender.calls) == 1
    assert sender.event(0)["event_name"] == "ConfirmedOrder"

    # 3. Repeated bot call is idempotent
    res_repeat = await emit_confirmed_order(db_session, live_shop, ORDER_ID, sender=sender)
    assert res_repeat["action"] == "ALREADY_EMITTED"
    assert len(sender.calls) == 1
