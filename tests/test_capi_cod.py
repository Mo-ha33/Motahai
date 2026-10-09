"""
test_capi_cod.py — Unit tests for Meta CAPI, Rule D-005 (Coexist: custom DeliveredPurchase), Webhooks & Asset Matrix
"""

import time

import pytest
from src.ameen_workforce.capi_service import (
    MetaCAPISender,
    hash_sha256,
    normalize_phone
)
from src.ameen_workforce.webhook_listener import OrderWebhookProcessor
from src.ameen_workforce.asset_matrix import TrackingAssetMatrixBuilder

def test_hash_sha256_and_phone_normalization():
    phone_eg = "01012345678"
    norm_eg = normalize_phone(phone_eg, "EG")
    assert norm_eg == "201012345678"
    hashed_ph = hash_sha256(norm_eg)
    assert len(hashed_ph) == 64  # SHA-256 output length

    email = "TestUser@Example.com "
    hashed_em = hash_sha256(email)
    assert len(hashed_em) == 64
    assert hashed_em == hash_sha256("testuser@example.com")

def test_meta_capi_payload_builder():
    sender = MetaCAPISender()
    payload = sender.build_event_payload(
        event_name="Purchase",
        event_id="purchase_1001",
        order_id="1001",
        value=1500.0,
        currency="EGP",
        email="client@motahai.com",
        phone="01123456789",
        fbp="fb.1.123456",
        fbc="fb.1.654321",
        test_event_code="TEST12345"
    )

    assert "data" in payload
    assert payload["test_event_code"] == "TEST12345"
    event = payload["data"][0]
    assert event["event_name"] == "Purchase"
    assert event["event_id"] == "purchase_1001"
    assert event["custom_data"]["value"] == 1500.0
    assert event["custom_data"]["currency"] == "EGP"
    assert "em" in event["user_data"]
    assert "ph" in event["user_data"]
    assert event["user_data"]["fbp"] == "fb.1.123456"

def test_rule_d005_cod_order_decision():
    sender = MetaCAPISender()

    # 1. COD Order placed/shipped but not yet delivered -> MUST BE DEFERRED (held until delivery)
    for held_status in ("shipped", "fulfilled", "pending"):
        pending_decision = sender.process_cod_order_event(
            order_id="ORD-99",
            status=held_status,
            value=750.0,
            currency="SAR",
            is_cod=True
        )
        assert pending_decision["action"] == "DEFERRED"
        assert "OrderPlaced" not in str(pending_decision)
        assert pending_decision["held_event"] == "DeliveredPurchase"

    # 2. COD Order delivered -> MUST BE READY TO EMIT as the custom DeliveredPurchase (never standard Purchase)
    delivered_decision = sender.process_cod_order_event(
        order_id="ORD-99",
        status="delivered",
        value=750.0,
        currency="SAR",
        is_cod=True
    )
    assert delivered_decision["action"] == "READY_TO_EMIT"
    assert delivered_decision["event_name"] == "DeliveredPurchase"
    assert delivered_decision["event_id"] == "delivered_ORD-99"
    event = delivered_decision["payload"]["data"][0]
    assert event["event_name"] == "DeliveredPurchase"
    assert event["event_id"] == "delivered_ORD-99"
    assert event["custom_data"]["value"] == 750.0

    # 3. Prepaid order (Credit Card) -> emits once PAID, not before
    prepaid_decision = sender.process_cod_order_event(
        order_id="ORD-100",
        status="paid",
        value=1200.0,
        currency="EGP",
        is_cod=False
    )
    assert prepaid_decision["action"] == "READY_TO_EMIT"
    assert prepaid_decision["event_name"] == "DeliveredPurchase"
    assert prepaid_decision["event_id"] == "delivered_ORD-100"
    unpaid_decision = sender.process_cod_order_event(
        order_id="ORD-101", status="created", value=1200.0, currency="EGP", is_cod=False
    )
    assert unpaid_decision["action"] == "DEFERRED"

@pytest.mark.parametrize("status", ["cancelled", "canceled", "refunded", "voided", "restored"])
@pytest.mark.parametrize("is_cod", [True, False])
def test_rule_d005_never_emits_for_cancelled_or_refunded(status, is_cod):
    decision = MetaCAPISender().process_cod_order_event(
        order_id="ORD-7", status=status, value=100.0, currency="EGP", is_cod=is_cod
    )
    assert decision["action"] == "SUPPRESSED"
    assert "payload" not in decision

@pytest.mark.parametrize("raw,country,expected", [
    ("0020 10 1234 5678", "EG", "201012345678"),    # 00 international prefix, EG
    ("+201098765432", "EG", "201098765432"),
    ("00966551234567", "SA", "966551234567"),       # 00 international prefix, SA
    ("551234567", "SA", "966551234567"),            # 9-digit Saudi mobile without trunk zero
    ("551234567", "EG", "966551234567"),
    ("01012345678", "EG", "201012345678"),          # EG local
    ("0551234567", "SA", "966551234567"),           # SA local
    ("", "EG", None),
])
def test_phone_normalization_cases(raw, country, expected):
    assert normalize_phone(raw, country) == expected

def test_capi_payload_phone_uses_currency_country():
    payload = MetaCAPISender().build_event_payload(
        event_name="DeliveredPurchase", event_id="delivered_1", order_id="1",
        value=10.0, currency="SAR", phone="0551234567"
    )
    assert payload["data"][0]["user_data"]["ph"] == [hash_sha256("966551234567")]

def _shopify(**overrides):
    base = {
        "id": 987654321,
        "financial_status": "pending",
        "fulfillment_status": None,
        "payment_gateway_names": ["cash_on_delivery"],
        "total_price": "850.00",
        "currency": "EGP",
        "customer": {"email": "ahmed@example.com"},
        "shipping_address": {"phone": "+201098765432"}
    }
    base.update(overrides)
    return base

def test_shopify_webhook_parser():
    processor = OrderWebhookProcessor()
    # COD, cash not yet collected, handed to courier -> SHIPPED, never delivered
    parsed = processor.parse_shopify_order(_shopify(fulfillment_status="fulfilled"))
    assert parsed["platform"] == "shopify"
    assert parsed["order_id"] == "987654321"
    assert parsed["is_cod"] is True
    assert parsed["status"] == "shipped"
    assert parsed["total_price"] == 850.0

    # COD, merchant marked cash collected -> delivered
    assert processor.parse_shopify_order(_shopify(financial_status="paid", fulfillment_status="fulfilled"))["status"] == "delivered"
    # COD, nothing happened yet
    assert processor.parse_shopify_order(_shopify())["status"] == "pending"
    # Prepaid and paid
    prepaid = processor.parse_shopify_order(_shopify(financial_status="paid", payment_gateway_names=["shopify_payments"]))
    assert prepaid["is_cod"] is False and prepaid["status"] == "paid"

@pytest.mark.parametrize("overrides,expected", [
    ({"cancelled_at": "2026-10-01T10:00:00Z"}, "cancelled"),
    ({"cancelled_at": "2026-10-01T10:00:00Z", "financial_status": "paid"}, "cancelled"),
    ({"financial_status": "refunded"}, "refunded"),
    ({"financial_status": "partially_refunded"}, "refunded"),
    ({"financial_status": "voided"}, "voided"),
])
def test_shopify_cancelled_refunded_never_delivered(overrides, expected):
    parsed = OrderWebhookProcessor().parse_shopify_order(_shopify(fulfillment_status="fulfilled", **overrides))
    assert parsed["status"] == expected

@pytest.mark.parametrize("gateway,expected", [
    ("cash_on_delivery", True),
    ("Cash on Delivery (COD)", True),
    ("COD", True),
    ("cod", True),
    ("الدفع عند الاستلام", True),
    ("manual", False),                 # also used for bank transfer
    ("Bank Transfer (manual)", False),
    ("cash", False),
    ("decoder_pay", False),            # 'cod' inside a word is not a token
    ("shopify_payments", False),
])
def test_cod_gateway_detection(gateway, expected):
    parsed = OrderWebhookProcessor().parse_shopify_order(_shopify(payment_gateway_names=[gateway]))
    assert parsed["is_cod"] is expected

def test_shopify_fulfillment_parser():
    processor = OrderWebhookProcessor()
    delivered = processor.parse_shopify_fulfillment(
        {"id": 555, "order_id": 987654321, "status": "success", "shipment_status": "delivered"})
    assert delivered["platform"] == "shopify_fulfillment"
    assert delivered["order_id"] == "987654321"
    assert delivered["status"] == "delivered"
    assert delivered["total_price"] is None and delivered["needs_order_context"] is True
    assert processor.parse_shopify_fulfillment(
        {"order_id": 1, "status": "success", "shipment_status": "in_transit"})["status"] == "shipped"
    assert processor.parse_shopify_fulfillment(
        {"order_id": 1, "status": "success", "shipment_status": "failure"})["status"] == "failed_delivery"
    assert processor.parse_shopify_fulfillment(
        {"order_id": 1, "status": "cancelled", "shipment_status": "delivered"})["status"] == "cancelled"

@pytest.fixture
def sent_events(monkeypatch):
    sent = []
    async def fake_send(self, pixel_id, access_token, payload):
        sent.append(payload)
        return {"status": "success"}
    monkeypatch.setattr(MetaCAPISender, "send_event", fake_send)
    return sent

@pytest.mark.asyncio
async def test_handle_shopify_cod_paid_emits_delivered_purchase(sent_events):
    result = await OrderWebhookProcessor().handle_order_update(
        "shopify", _shopify(financial_status="paid", fulfillment_status="fulfilled"),
        pixel_id="123", access_token="tok")
    decision = result["d005_decision"]
    assert decision["action"] == "READY_TO_EMIT"
    assert decision["event_name"] == "DeliveredPurchase"
    assert decision["event_id"] == "delivered_987654321"
    assert len(sent_events) == 1
    assert sent_events[0]["data"][0]["event_name"] == "DeliveredPurchase"
    assert sent_events[0]["data"][0]["event_id"] == "delivered_987654321"

@pytest.mark.asyncio
async def test_handle_shopify_cod_shipped_and_cancelled_do_not_emit(sent_events):
    processor = OrderWebhookProcessor()
    shipped = await processor.handle_order_update(
        "shopify", _shopify(fulfillment_status="fulfilled"), pixel_id="123", access_token="tok")
    assert shipped["d005_decision"]["action"] == "DEFERRED"
    for overrides in ({"cancelled_at": "2026-10-01T10:00:00Z"}, {"financial_status": "refunded"}, {"financial_status": "voided"}):
        res = await processor.handle_order_update(
            "shopify", _shopify(fulfillment_status="fulfilled", **overrides), pixel_id="123", access_token="tok")
        assert res["d005_decision"]["action"] == "SUPPRESSED"
    assert sent_events == []

@pytest.mark.asyncio
async def test_handle_shopify_prepaid_paid_emits(sent_events):
    order = _shopify(financial_status="paid", payment_gateway_names=["shopify_payments"])
    result = await OrderWebhookProcessor().handle_order_update("shopify", order, pixel_id="123", access_token="tok")
    assert result["d005_decision"]["event_id"] == "delivered_987654321"
    assert len(sent_events) == 1

@pytest.mark.asyncio
async def test_handle_fulfillment_delivered_needs_order_context_then_emits_with_context(sent_events):
    processor = OrderWebhookProcessor()
    fulfillment = {"id": 555, "order_id": 987654321, "status": "success", "shipment_status": "delivered"}

    # Fulfillment alone has no value/currency: nothing may be emitted
    res = await processor.handle_order_update("shopify_fulfillment", fulfillment, pixel_id="123", access_token="tok")
    assert res["d005_decision"]["action"] == "NEEDS_ORDER_CONTEXT"
    assert sent_events == []

    # Not delivered yet / failed delivery
    transit = await processor.handle_order_update(
        "shopify_fulfillment", {**fulfillment, "shipment_status": "in_transit"}, pixel_id="123", access_token="tok")
    assert transit["d005_decision"]["action"] == "DEFERRED"
    failed = await processor.handle_order_update(
        "shopify_fulfillment", {**fulfillment, "shipment_status": "failure"}, pixel_id="123", access_token="tok")
    assert failed["d005_decision"]["action"] == "SUPPRESSED"
    assert sent_events == []

    # Once the stored order is joined (S1-1), the delivered fulfillment emits
    joined = await processor.handle_order_update(
        "shopify_fulfillment", fulfillment, pixel_id="123", access_token="tok",
        order_context={"total_price": 850.0, "currency": "EGP", "is_cod": True, "email": "a@example.com"})
    assert joined["d005_decision"]["action"] == "READY_TO_EMIT"
    assert joined["d005_decision"]["event_id"] == "delivered_987654321"
    assert len(sent_events) == 1

@pytest.mark.asyncio
async def test_handle_unsupported_platform():
    res = await OrderWebhookProcessor().handle_order_update("woocommerce", {})
    assert res["status"] == "error"

def test_salla_webhook_parser():
    processor = OrderWebhookProcessor()
    mock_salla = {
        "data": {
            "id": "SAL-456",
            "status": {"slug": "delivered"},
            "payment_method": "الدفع عند الاستلام (COD)",
            "total": 350.0,
            "currency": "SAR",
            "customer": {"email": "sara@example.sa", "mobile": "0551234567"}
        }
    }

    parsed = processor.parse_salla_order(mock_salla)
    assert parsed["platform"] == "salla"
    assert parsed["order_id"] == "SAL-456"
    assert parsed["is_cod"] is True
    assert parsed["status"] == "delivered"
    assert parsed["currency"] == "SAR"

def test_tracking_asset_matrix_builder():
    matrix = TrackingAssetMatrixBuilder.build_matrix(
        client_name="Kinz Al Atfal",
        store_url="https://epxsmk-y0.myshopify.com",
        platform="Shopify",
        gtm_container_id="GTM-5C5N552P",
        ga4_measurement_id="G-AMEEN2026D",
        meta_pixel_id="284759302194857",
        meta_capi_enabled=True,
        cod_reconciliation_active=True
    )

    assert matrix["client_name"] == "Kinz Al Atfal"
    assert matrix["assets"]["GTM"]["status"] == "VALIDATED"
    assert matrix["assets"]["GA4"]["status"] == "VALIDATED"
    assert matrix["assets"]["Meta"]["capi_active"] is True

    sheet = TrackingAssetMatrixBuilder.export_markdown_sheet(matrix)
    assert "Kinz Al Atfal" in sheet
    assert "GTM-5C5N552P" in sheet
    assert "Rule D-005" in sheet

def test_salla_cancelled_and_prepaid_statuses():
    processor = OrderWebhookProcessor()
    base = {"data": {"id": "SAL-1", "status": {"slug": "canceled"}, "payment_method": "mada", "total": 100.0, "currency": "SAR"}}
    assert processor.parse_salla_order(base)["status"] == "cancelled"
    base["data"]["status"] = {"slug": "in_progress"}
    parsed = processor.parse_salla_order(base)
    assert parsed["is_cod"] is False and parsed["status"] == "paid"
    base["data"]["payment_method"] = "cod"
    assert processor.parse_salla_order(base)["status"] == "in_progress"   # COD still waiting for delivery


def test_shopify_cod_paid_requires_fulfillment():
    processor = OrderWebhookProcessor()
    # Some COD setups mark orders paid at creation: paid alone is NOT delivery
    held = processor.parse_shopify_order(_shopify(financial_status="paid", fulfillment_status=None))
    assert held["status"] == "paid_unfulfilled"
    delivered_partial = processor.parse_shopify_order(_shopify(financial_status="paid", fulfillment_status="partial"))
    assert delivered_partial["status"] == "delivered"
    by_list = processor.parse_shopify_order(_shopify(financial_status="paid", fulfillments=[{"id": 1}]))
    assert by_list["status"] == "delivered"
    # Prepaid is unaffected: paid is enough
    prepaid = processor.parse_shopify_order(_shopify(financial_status="paid", payment_gateway_names=["stripe"]))
    assert prepaid["status"] == "paid"

@pytest.mark.asyncio
async def test_cod_paid_unfulfilled_is_held_and_paid_fulfilled_emits(sent_events):
    processor = OrderWebhookProcessor()
    held = await processor.handle_order_update(
        "shopify", _shopify(financial_status="paid"), pixel_id="123", access_token="tok")
    assert held["d005_decision"]["action"] == "DEFERRED"
    assert sent_events == []
    emitted = await processor.handle_order_update(
        "shopify", _shopify(financial_status="paid", fulfillment_status="fulfilled"), pixel_id="123", access_token="tok")
    assert emitted["d005_decision"]["action"] == "READY_TO_EMIT"
    assert len(sent_events) == 1

@pytest.mark.asyncio
async def test_send_event_uses_access_token_param_not_authorization_header(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"events_received": 1, "fbtrace_id": "abc"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def post(self, url, headers=None, json=None, params=None):
            captured.update(url=url, headers=headers or {}, json=json, params=params)
            return FakeResponse()

    monkeypatch.setattr("src.ameen_workforce.capi_service.httpx.AsyncClient", FakeClient)
    payload = MetaCAPISender().build_event_payload("DeliveredPurchase", "delivered_1", "1", 10.0, "EGP")
    result = await MetaCAPISender().send_event("DATASET1", "SECRET-TOKEN", payload)
    assert result["status"] == "success"
    assert captured["json"]["access_token"] == "SECRET-TOKEN"
    assert not any(k.lower() == "authorization" for k in captured["headers"])
    assert "SECRET-TOKEN" not in captured["url"]          # kept out of URLs so client/proxy logs never see it
    assert "SECRET-TOKEN" not in str(result)

@pytest.mark.asyncio
async def test_send_event_failure_never_logs_token(monkeypatch, caplog):
    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def post(self, *args, **kwargs):
            raise RuntimeError("boom SECRET-TOKEN")

    monkeypatch.setattr("src.ameen_workforce.capi_service.httpx.AsyncClient", BoomClient)
    with caplog.at_level("DEBUG"):
        result = await MetaCAPISender().send_event("D", "SECRET-TOKEN", {"data": []})
    assert result["status"] == "failed"
    assert "SECRET-TOKEN" not in caplog.text and "SECRET-TOKEN" not in str(result)

def test_stale_event_time_guard():
    sender = MetaCAPISender()
    now = int(time.time())
    for bad in (now - 8 * 24 * 3600, now + 3600):
        decision = sender.process_cod_order_event(
            order_id="ORD-1", status="delivered", value=10.0, currency="EGP", is_cod=True, event_time=bad)
        assert decision["action"] == "STALE"
        assert "payload" not in decision
        with pytest.raises(ValueError):
            sender.build_event_payload("DeliveredPurchase", "delivered_1", "1", 10.0, "EGP", event_time=bad)
    ok_time = now - 6 * 24 * 3600
    decision = sender.process_cod_order_event(
        order_id="ORD-1", status="delivered", value=10.0, currency="EGP", is_cod=True, event_time=ok_time)
    assert decision["action"] == "READY_TO_EMIT"
    assert decision["payload"]["data"][0]["event_time"] == ok_time
    # Default stays "now"
    default = sender.build_event_payload("DeliveredPurchase", "delivered_1", "1", 10.0, "EGP")
    assert abs(default["data"][0]["event_time"] - now) < 5
