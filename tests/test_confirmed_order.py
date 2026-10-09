"""
test_confirmed_order.py — Unit tests for S2-2 ConfirmedOrder Signal Specialist.

Covers:
1. Event definition: ConfirmedOrder, confirmed_<order_id>, EVENT_TYPES
2. CAPI service eligibility & payload construction (ph, em, fbp, fbc, client_ip, client_user_agent, full value)
3. Webhook parsing for Shopify tags (exact, case-insensitive match; never a substring)
4. Webhook parsing for Salla: review statuses are NOT confirmation; merchant-configured statuses and tags are
5. Full pipeline integration for Shopify & Salla (SENT / SHADOW), confirmation sources and per-tenant rules
5b. Cancelled / refunded orders never emit; handle_order_update is decision-only
6. Idempotency guarantee: ConfirmedOrder never fires more than once per order
7. 3-Step Signal Ladder progression: ConfirmedOrder -> DeliveredPurchase
8. Direct bot / call confirmation trigger: emit_confirmed_order
"""

import warnings
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
from src.ameen_workforce.checkout_context import load_checkout_context
from src.ameen_workforce.confirmation import (
    default_confirmation_rules,
    effective_confirmation_rules,
    set_confirmation_rules,
    validate_confirmation_rules,
)
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import CapiEvent, Order, Tenant, create_tenant
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


def _order(session, tenant, order_id=ORDER_ID):
    session.expire_all()
    return session.scalar(select(Order).where(Order.tenant_id == tenant.id, Order.platform_order_id == order_id))


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
    # Review statuses mean "awaiting review" (BEFORE confirmation): they must not be in any default list.
    for review in ("in_review", "under_review", "قيد المراجعة", "تحت المراجعة"):
        assert review not in CONFIRMED_STATUSES
        assert review not in default_confirmation_rules()["statuses"]
        assert review not in default_confirmation_rules()["tags"]


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
    "COD-CONFIRMED",
    "  Confirmed  , priority",
    "urgent, order-confirmed",
    "تم التأكيد",
    ["confirmed", "vip"],
    ["order-confirmed"],
])
def test_shopify_parser_detects_confirmed_tags(tag_input):
    payload = shopify_payload(tags=tag_input)
    parsed = OrderWebhookProcessor.parse_shopify_order(payload)
    assert parsed["is_confirmed"] is True
    assert parsed["confirmation_source"] == "tag"
    assert parsed["order_total"] == 1250.0


@pytest.mark.parametrize("tag_input", [
    "unconfirmed",
    "cod-confirmed-pending",
    "call_confirmed, express",
    "whatsapp_confirmed",
    ["not-confirmed"],
    "urgent, vip",
    "confirmed-later",
    None,
])
def test_shopify_parser_tags_are_exact_match_not_substring(tag_input):
    parsed = OrderWebhookProcessor.parse_shopify_order(shopify_payload(tags=tag_input))
    assert parsed["is_confirmed"] is False
    assert parsed["confirmation_source"] is None


@pytest.mark.parametrize("tags", ["confirmed, cod-cancelled", "COD-CONFIRMED,cancelled-by-customer", "confirmed, ملغي"])
def test_shopify_cancellation_tag_blocks_confirmation(tags):
    parsed = OrderWebhookProcessor.parse_shopify_order(shopify_payload(tags=tags))
    assert parsed["is_confirmed"] is False


def test_shopify_parser_tag_list_and_custom_tag_rules():
    p1 = shopify_payload(tags=None, tag_list=["confirmed"])
    assert OrderWebhookProcessor.parse_shopify_order(p1)["is_confirmed"] is True

    # A tenant that configured its own tag list uses it INSTEAD of the defaults
    rules = {**effective_confirmation_rules(None), "tags": ["bot-ok"]}
    assert OrderWebhookProcessor.parse_shopify_order(shopify_payload(tags="bot-ok"), rules)["is_confirmed"] is True
    assert OrderWebhookProcessor.parse_shopify_order(shopify_payload(tags="confirmed"), rules)["is_confirmed"] is False


@pytest.mark.parametrize("status_slug", [
    "confirmed",
    "in_review",
    "under_review",
    "مؤكد",
    "قيد المراجعة",
    "تحت المراجعة",
])
def test_salla_parser_default_rules_do_not_confirm_on_status(status_slug):
    """No Salla status confirms by default (the merchant's real confirmed status is configured per tenant)."""
    parsed = OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug=status_slug))
    assert parsed["is_confirmed"] is False
    assert parsed["confirmation_source"] is None
    assert parsed["order_total"] == 600.0


@pytest.mark.parametrize("status_slug", ["in_review", "under_review", "قيد المراجعة", "تحت المراجعة"])
def test_salla_review_statuses_do_not_confirm_when_other_status_is_configured(status_slug):
    rules = {**effective_confirmation_rules(None), "statuses": ["confirmed", "مؤكد"]}
    assert OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug=status_slug), rules)["is_confirmed"] is False


def test_salla_parser_configured_status_confirms_by_slug_name_or_custom_name():
    rules = {**effective_confirmation_rules(None), "statuses": ["confirmed", "تم الاتصال"]}
    by_slug = OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug="confirmed"), rules)
    assert by_slug["confirmation_source"] == "status"
    by_name = OrderWebhookProcessor.parse_salla_order(
        salla_payload(status={"slug": "", "name": "تم الاتصال"}, payment_method="cod"), rules)
    assert by_name["confirmation_source"] == "status"
    by_custom = OrderWebhookProcessor.parse_salla_order(
        salla_payload(status={"slug": "under_review", "name": "x", "customized": {"id": 1, "name": "Confirmed"}}), rules)
    assert by_custom["confirmation_source"] == "status"


def test_salla_parser_detects_tags():
    p1 = salla_payload(status_slug="pending", tags=["confirmed"])
    assert OrderWebhookProcessor.parse_salla_order(p1)["confirmation_source"] == "tag"

    p2 = salla_payload(status_slug="pending", tags="Cod-Confirmed")
    assert OrderWebhookProcessor.parse_salla_order(p2)["confirmation_source"] == "tag"

    for tags in (None, "call_confirmed", ["unconfirmed"], ["confirmed", "cod-cancelled"]):
        p = salla_payload(status_slug="pending", tags=tags)
        assert OrderWebhookProcessor.parse_salla_order(p)["is_confirmed"] is False


def test_salla_parser_implicit_sources():
    shipped = OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug="shipped"))
    assert shipped["confirmation_source"] == "implicit_shipped"
    prepaid = OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug="in_progress", payment_method="mada"))
    assert prepaid["confirmation_source"] == "implicit_paid"
    off = {**effective_confirmation_rules(None), "implicit_on_ship": False}
    assert OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug="shipped"), off)["is_confirmed"] is False
    assert OrderWebhookProcessor.parse_salla_order(
        salla_payload(status_slug="in_progress", payment_method="mada"), off)["is_confirmed"] is False
    cancelled = OrderWebhookProcessor.parse_salla_order(salla_payload(status_slug="canceled", tags=["confirmed"]))
    assert cancelled["is_confirmed"] is False


# =============================================================================
# 3b. Per-tenant confirmation rules
# =============================================================================

def test_default_rules_and_validation():
    rules = effective_confirmation_rules(None)
    assert rules["statuses"] == []
    assert rules["implicit_on_ship"] is True
    assert set(rules["tags"]) == {"confirmed", "cod-confirmed", "order-confirmed", "تم التأكيد", "مؤكد"}

    assert validate_confirmation_rules({"tags": [" COD-OK "], "implicit_on_ship": False}) == {
        "tags": ["cod-ok"], "implicit_on_ship": False}
    for bad in ("x", {"tags": "confirmed"}, {"tags": [1]}, {"tags": [""]}, {"tags": ["x" * 65]},
                {"statuses": None}, {"implicit_on_ship": "yes"}, {"implicit_on_ship": 1}, {"bogus": []}):
        with pytest.raises(ValueError):
            validate_confirmation_rules(bad)


def test_set_confirmation_rules_persists_validates_and_clears(db_session, shadow_shop):
    assert shadow_shop.confirmation_rules is None
    effective = set_confirmation_rules(db_session, shadow_shop, {"statuses": ["Confirmed"], "implicit_on_ship": False})
    assert effective["statuses"] == ["confirmed"] and effective["implicit_on_ship"] is False
    assert effective["tags"] == default_confirmation_rules()["tags"]  # missing key keeps its default

    db_session.expire_all()
    stored = db_session.get(Tenant, shadow_shop.id)
    assert stored.confirmation_rules == {"statuses": ["confirmed"], "implicit_on_ship": False}

    with pytest.raises(ValueError):
        set_confirmation_rules(db_session, shadow_shop, {"tags": "confirmed"})
    db_session.rollback()
    assert db_session.get(Tenant, shadow_shop.id).confirmation_rules == {"statuses": ["confirmed"], "implicit_on_ship": False}

    set_confirmation_rules(db_session, shadow_shop, None)
    db_session.expire_all()
    assert db_session.get(Tenant, shadow_shop.id).confirmation_rules is None


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
async def test_pipeline_salla_configured_status_emits_confirmed_order(db_session, salla_shop):
    set_confirmation_rules(db_session, salla_shop, {"statuses": ["confirmed"]})
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
    assert _order(db_session, salla_shop).confirmation_source == "status"


@pytest.mark.asyncio
async def test_pipeline_salla_unconfigured_status_does_not_confirm(db_session, salla_shop):
    sender = FakeSender()
    payload = salla_payload(status_slug="confirmed")  # tenant has NOT configured it
    result = await process_webhook(db_session, salla_shop, "salla", "order.status.updated", payload, sender=sender)
    assert result["action"] == "DEFERRED"
    assert sender.calls == []
    assert _order(db_session, salla_shop).confirmed_at is None
    assert _order(db_session, salla_shop).confirmation_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_slug", ["in_review", "under_review", "قيد المراجعة", "تحت المراجعة"])
@pytest.mark.parametrize("configured", [False, True])
async def test_pipeline_salla_review_statuses_never_emit_confirmed_order(db_session, salla_shop, status_slug, configured):
    if configured:  # even a tenant that configured a real confirmed status gets nothing for review statuses
        set_confirmation_rules(db_session, salla_shop, {"statuses": ["confirmed", "مؤكد"]})
    sender = FakeSender()
    result = await process_webhook(db_session, salla_shop, "salla", "order.status.updated",
                                   salla_payload(status_slug=status_slug), sender=sender)
    assert result["action"] == "DEFERRED"
    assert sender.calls == []
    assert db_session.scalars(select(CapiEvent)).all() == []
    assert _order(db_session, salla_shop).confirmed_at is None


@pytest.mark.asyncio
async def test_pipeline_salla_shipped_is_implicit_confirmation_then_delivered(db_session, salla_shop):
    sender = FakeSender()
    shipped = await process_webhook(db_session, salla_shop, "salla", "order.status.updated",
                                    salla_payload(status_slug="shipped"), sender=sender)
    assert shipped["action"] == "SENT"
    assert sender.event(0)["event_name"] == "ConfirmedOrder"
    assert _order(db_session, salla_shop).confirmation_source == "implicit_shipped"

    delivered = await process_webhook(db_session, salla_shop, "salla", "order.status.updated",
                                      salla_payload(status_slug="delivered"), sender=sender)
    assert delivered["action"] == "SENT"
    assert [c["payload"]["data"][0]["event_name"] for c in sender.calls] == ["ConfirmedOrder", "DeliveredPurchase"]
    assert _order(db_session, salla_shop).confirmation_source == "implicit_shipped"  # first source wins


@pytest.mark.asyncio
async def test_pipeline_implicit_on_ship_false_sends_no_confirmed_order(db_session, salla_shop):
    set_confirmation_rules(db_session, salla_shop, {"implicit_on_ship": False})
    sender = FakeSender()
    shipped = await process_webhook(db_session, salla_shop, "salla", "order.status.updated",
                                    salla_payload(status_slug="shipped"), sender=sender)
    assert shipped["action"] == "DEFERRED"
    assert sender.calls == []
    assert _order(db_session, salla_shop).confirmed_at is None
    assert db_session.scalars(select(CapiEvent).where(CapiEvent.event_name == "ConfirmedOrder")).all() == []

    # ... and delivery still sends the DeliveredPurchase only
    delivered = await process_webhook(db_session, salla_shop, "salla", "order.status.updated",
                                      salla_payload(status_slug="delivered"), sender=sender)
    assert delivered["action"] == "SENT"
    assert [c["payload"]["data"][0]["event_name"] for c in sender.calls] == ["DeliveredPurchase"]


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


# =============================================================================
# 8. Shopify confirmation sources through the pipeline (FX-3)
# =============================================================================

@pytest.mark.asyncio
async def test_pipeline_shopify_exact_tag_any_case_confirms_with_source_tag(db_session, live_shop):
    sender = FakeSender()
    result = await process_webhook(db_session, live_shop, "shopify", "orders/updated",
                                   shopify_payload(tags="COD-CONFIRMED"), sender=sender)
    assert result["action"] == "SENT"
    assert sender.event(0)["event_name"] == "ConfirmedOrder"
    assert _order(db_session, live_shop).confirmation_source == "tag"


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", ["unconfirmed", "cod-confirmed-pending", "confirmed, cod-cancelled", "ملغي"])
async def test_pipeline_shopify_non_matching_or_cancellation_tags_do_not_confirm(db_session, live_shop, tags):
    sender = FakeSender()
    result = await process_webhook(db_session, live_shop, "shopify", "orders/updated",
                                   shopify_payload(tags=tags), sender=sender)
    assert result["action"] == "DEFERRED"
    assert sender.calls == []
    assert _order(db_session, live_shop).confirmed_at is None
    assert db_session.scalars(select(CapiEvent)).all() == []


@pytest.mark.asyncio
async def test_pipeline_shopify_shipped_cod_is_implicit_shipped(db_session, live_shop):
    sender = FakeSender()
    result = await process_webhook(db_session, live_shop, "shopify", "orders/updated",
                                   shopify_payload(fulfillment_status="fulfilled"), sender=sender)
    assert result["action"] == "SENT"
    assert sender.event(0)["event_name"] == "ConfirmedOrder"
    assert _order(db_session, live_shop).confirmation_source == "implicit_shipped"


@pytest.mark.asyncio
async def test_pipeline_shopify_shipped_without_implicit_on_ship_sends_nothing(db_session, live_shop):
    set_confirmation_rules(db_session, live_shop, {"implicit_on_ship": False})
    sender = FakeSender()
    result = await process_webhook(db_session, live_shop, "shopify", "orders/updated",
                                   shopify_payload(fulfillment_status="fulfilled"), sender=sender)
    assert result["action"] == "DEFERRED"
    assert sender.calls == []
    assert _order(db_session, live_shop).confirmation_source is None


@pytest.mark.asyncio
async def test_pipeline_fulfillment_delivered_is_implicit_shipped_for_known_order(db_session, live_shop):
    set_confirmation_rules(db_session, live_shop, {"implicit_on_ship": False})
    sender = FakeSender()
    await process_webhook(db_session, live_shop, "shopify", "orders/updated", shopify_payload(), sender=sender)
    set_confirmation_rules(db_session, live_shop, None)  # defaults again: shipping now confirms
    res = await process_webhook(
        db_session, live_shop, "shopify", "fulfillments/update",
        {"id": 77, "order_id": ORDER_ID, "status": "success", "shipment_status": "in_transit"}, sender=sender)
    assert res["action"] == "SENT"
    assert sender.event(0)["event_name"] == "ConfirmedOrder"
    assert _order(db_session, live_shop).confirmation_source == "implicit_shipped"


@pytest.mark.asyncio
async def test_pipeline_prepaid_paid_sends_confirmed_order_then_delivered_purchase(db_session, live_shop):
    """For a prepaid order payment IS the confirmation: ConfirmedOrder (implicit_paid) goes out before DeliveredPurchase."""
    sender = FakeSender()
    payload = shopify_payload(financial_status="paid", payment_gateway_names=["shopify_payments"])
    result = await process_webhook(db_session, live_shop, "shopify", "orders/paid", payload, sender=sender)
    assert result["action"] == "SENT"
    assert [c["payload"]["data"][0]["event_name"] for c in sender.calls] == ["ConfirmedOrder", "DeliveredPurchase"]
    order = _order(db_session, live_shop)
    assert order.confirmation_source == "implicit_paid"
    assert order.confirmed_at is not None
    # event_time = the first observation of the confirmation (confirmed_at, here the platform's updated_at)
    assert sender.event(0)["event_time"] == int(order.confirmed_at.timestamp())


@pytest.mark.asyncio
async def test_pipeline_prepaid_paid_without_implicit_on_ship_sends_delivered_purchase_only(db_session, live_shop):
    set_confirmation_rules(db_session, live_shop, {"implicit_on_ship": False})  # explicit-only merchant
    sender = FakeSender()
    payload = shopify_payload(financial_status="paid", payment_gateway_names=["shopify_payments"])
    await process_webhook(db_session, live_shop, "shopify", "orders/paid", payload, sender=sender)
    assert [c["payload"]["data"][0]["event_name"] for c in sender.calls] == ["DeliveredPurchase"]
    assert _order(db_session, live_shop).confirmation_source is None


@pytest.mark.asyncio
async def test_pipeline_prepaid_paid_with_cancellation_tag_sends_no_confirmed_order(db_session, live_shop):
    sender = FakeSender()
    payload = shopify_payload(tags="cod-cancelled", financial_status="paid", payment_gateway_names=["shopify_payments"])
    await process_webhook(db_session, live_shop, "shopify", "orders/paid", payload, sender=sender)
    assert "ConfirmedOrder" not in [c["payload"]["data"][0]["event_name"] for c in sender.calls]
    assert _order(db_session, live_shop).confirmed_at is None


@pytest.mark.asyncio
async def test_pipeline_prepaid_paid_with_tag_sends_confirmed_then_delivered(db_session, live_shop):
    sender = FakeSender()
    payload = shopify_payload(tags="confirmed", financial_status="paid", payment_gateway_names=["shopify_payments"])
    await process_webhook(db_session, live_shop, "shopify", "orders/paid", payload, sender=sender)
    assert [c["payload"]["data"][0]["event_name"] for c in sender.calls] == ["ConfirmedOrder", "DeliveredPurchase"]
    assert _order(db_session, live_shop).confirmation_source == "tag"


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"cancelled_at": "2026-10-01T10:00:00Z"},
    {"financial_status": "refunded"},
    {"financial_status": "voided"},
])
async def test_cancelled_refunded_voided_orders_never_confirm_even_when_tagged(db_session, live_shop, overrides):
    sender = FakeSender()
    payload = shopify_payload(tags="confirmed", fulfillment_status="fulfilled", **overrides)
    result = await process_webhook(db_session, live_shop, "shopify", "orders/updated", payload, sender=sender)
    assert result["action"] == "SUPPRESSED"
    assert sender.calls == []
    order = _order(db_session, live_shop)
    assert order.confirmed_at is None and order.confirmation_source is None
    assert db_session.scalars(select(CapiEvent)).all() == []


@pytest.mark.asyncio
async def test_emit_confirmed_order_records_manual_source_and_skips_cancelled(db_session, live_shop):
    sender = FakeSender()
    await process_webhook(db_session, live_shop, "shopify", "orders/create", shopify_payload(), sender=sender)
    res = await emit_confirmed_order(db_session, live_shop, ORDER_ID, sender=sender)
    assert res["action"] == "SENT"
    assert _order(db_session, live_shop).confirmation_source == "manual"

    cancelled = shopify_payload(cancelled_at="2026-10-01T10:00:00Z", id="ORD-CANCELLED-1")
    await process_webhook(db_session, live_shop, "shopify", "orders/updated", cancelled, sender=sender)
    res = await emit_confirmed_order(db_session, live_shop, "ORD-CANCELLED-1", sender=sender)
    assert res["action"] == "SUPPRESSED"
    assert len(sender.calls) == 1
    order = _order(db_session, live_shop, "ORD-CANCELLED-1")
    assert order.confirmed_at is None and order.confirmation_source is None


@pytest.mark.asyncio
async def test_confirmation_does_not_purge_checkout_context(db_session, live_shop):
    """D-006: only the final DeliveredPurchase send purges the encrypted IP/UA, never ConfirmedOrder."""
    sender = FakeSender()
    await process_webhook(db_session, live_shop, "shopify", "orders/updated", shopify_payload(tags="confirmed"), sender=sender)
    assert sender.event(0)["event_name"] == "ConfirmedOrder"
    order = _order(db_session, live_shop)
    assert load_checkout_context(db_session, order.id, datetime.now(timezone.utc)) == (IP, UA)

    await process_webhook(db_session, live_shop, "shopify", "orders/updated",
                          shopify_payload(tags="confirmed", financial_status="paid", fulfillment_status="fulfilled"),
                          sender=sender)
    assert sender.event(1)["event_name"] == "DeliveredPurchase"
    assert load_checkout_context(db_session, order.id, datetime.now(timezone.utc)) == (None, None)


# =============================================================================
# 9. handle_order_update is decision-only (FX-3)
# =============================================================================

@pytest.mark.asyncio
async def test_handle_order_update_never_sends_even_with_credentials(monkeypatch):
    calls = []

    async def boom(self, pixel_id, access_token, payload):
        calls.append((pixel_id, access_token))
        raise AssertionError("handle_order_update must never call send_event")

    monkeypatch.setattr(MetaCAPISender, "send_event", boom)
    processor = OrderWebhookProcessor()
    payload = shopify_payload(tags="confirmed")

    with pytest.warns(DeprecationWarning):
        res = await processor.handle_order_update("shopify", payload, pixel_id="123", access_token="tok")
    assert res["d005_decision"]["action"] == "READY_TO_EMIT"
    assert res["d005_decision"]["event_name"] == "ConfirmedOrder"
    assert "capi_dispatch_result" not in res["d005_decision"]
    assert res["parsed_order"]["confirmation_source"] == "tag"

    delivered = shopify_payload(financial_status="paid", fulfillment_status="fulfilled")
    with pytest.warns(DeprecationWarning):
        res = await processor.handle_order_update("shopify", delivered, pixel_id="123", access_token="tok")
    assert res["d005_decision"]["event_name"] == "DeliveredPurchase"
    assert calls == []


@pytest.mark.asyncio
async def test_handle_order_update_without_credentials_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        res = await OrderWebhookProcessor().handle_order_update("shopify", shopify_payload(tags="confirmed"))
    assert res["d005_decision"]["event_name"] == "ConfirmedOrder"


@pytest.mark.asyncio
async def test_handle_order_update_salla_review_status_is_deferred_with_tenant_rules():
    processor = OrderWebhookProcessor()
    res = await processor.handle_order_update("salla", salla_payload(status_slug="under_review"))
    assert res["d005_decision"]["action"] == "DEFERRED"
    rules = {**effective_confirmation_rules(None), "statuses": ["under_review"]}  # a merchant may opt in explicitly
    res = await processor.handle_order_update("salla", salla_payload(status_slug="under_review"), confirmation_rules=rules)
    assert res["d005_decision"]["event_name"] == "ConfirmedOrder"


# =============================================================================
# 10. Funnel invariant: Confirmed contains Delivered
# =============================================================================

@pytest.mark.asyncio
async def test_every_order_with_delivered_purchase_also_has_confirmed_order(db_session, live_shop, salla_shop):
    """With implicit_on_ship on (default), no order may produce a DeliveredPurchase without a ConfirmedOrder."""
    sender = FakeSender()
    shop = lambda **kw: process_webhook(db_session, live_shop, "shopify", "orders/updated", shopify_payload(**kw), sender=sender)
    salla = lambda **kw: process_webhook(db_session, salla_shop, "salla", "order.status.updated", salla_payload(**kw), sender=sender)
    prepaid = dict(payment_gateway_names=["shopify_payments"])

    # COD tagged confirmed (still pending), later delivered
    await shop(id="INV-TAG", tags="confirmed")
    await shop(id="INV-TAG", tags="confirmed", financial_status="paid", fulfillment_status="fulfilled")
    # COD shipped, then delivered
    await shop(id="INV-SHIP", fulfillment_status="fulfilled")
    await shop(id="INV-SHIP", financial_status="paid", fulfillment_status="fulfilled")
    # COD first seen already delivered
    await shop(id="INV-DELIVERED", financial_status="paid", fulfillment_status="fulfilled")
    # prepaid paid at creation
    await shop(id="INV-PREPAID", financial_status="paid", **prepaid)
    # Salla: COD first seen delivered, COD shipped then delivered, prepaid in progress (paid)
    await salla(id="INV-S-DELIVERED", status_slug="delivered")
    await salla(id="INV-S-SHIP", status_slug="shipped")
    await salla(id="INV-S-SHIP", status_slug="delivered")
    await salla(id="INV-S-PREPAID", status_slug="in_progress", payment_method="mada")
    # an order that is only tagged/pending never reaches DeliveredPurchase
    await shop(id="INV-PENDING", tags="confirmed")

    rows = db_session.scalars(select(CapiEvent)).all()
    orders = {o.id: o.platform_order_id for o in db_session.scalars(select(Order)).all()}
    by_order = {}
    for r in rows:
        by_order.setdefault(orders[r.order_id], set()).add(r.event_name)

    delivered_orders = {o for o, names in by_order.items() if "DeliveredPurchase" in names}
    assert delivered_orders == {"INV-TAG", "INV-SHIP", "INV-DELIVERED", "INV-PREPAID",
                                "INV-S-DELIVERED", "INV-S-SHIP", "INV-S-PREPAID"}
    for order_id in delivered_orders:
        assert "ConfirmedOrder" in by_order[order_id], order_id
    assert by_order["INV-PENDING"] == {"ConfirmedOrder"}
    # one ConfirmedOrder per order at most (idempotent) and each row was actually sent
    assert all(r.status == "sent" for r in rows)
    assert len(rows) == len(delivered_orders) * 2 + 1
