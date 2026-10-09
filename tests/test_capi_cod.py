"""
test_capi_cod.py — Unit tests for Meta CAPI, Rule D-005, Webhooks & Asset Matrix
"""

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
    
    # 1. COD Order placed but not yet delivered -> MUST BE DEFERRED
    pending_decision = sender.process_cod_order_event(
        order_id="ORD-99",
        status="shipped",
        value=750.0,
        currency="SAR",
        is_cod=True
    )
    assert pending_decision["action"] == "DEFERRED"
    assert pending_decision["event_type"] == "OrderPlaced"

    # 2. COD Order delivered -> MUST BE READY TO EMIT as Purchase
    delivered_decision = sender.process_cod_order_event(
        order_id="ORD-99",
        status="delivered",
        value=750.0,
        currency="SAR",
        is_cod=True
    )
    assert delivered_decision["action"] == "READY_TO_EMIT"
    assert delivered_decision["event_name"] == "Purchase"
    assert delivered_decision["event_id"] == "purchase_ORD-99"

    # 3. Prepaid order (Credit Card) -> Emits Purchase immediately
    prepaid_decision = sender.process_cod_order_event(
        order_id="ORD-100",
        status="created",
        value=1200.0,
        currency="EGP",
        is_cod=False
    )
    assert prepaid_decision["action"] == "READY_TO_EMIT"
    assert prepaid_decision["event_name"] == "Purchase"

def test_shopify_webhook_parser():
    processor = OrderWebhookProcessor()
    mock_shopify = {
        "id": 987654321,
        "financial_status": "pending",
        "fulfillment_status": "fulfilled",
        "payment_gateway_names": ["cash_on_delivery"],
        "total_price": "850.00",
        "currency": "EGP",
        "customer": {"email": "ahmed@example.com"},
        "shipping_address": {"phone": "+201098765432"}
    }

    parsed = processor.parse_shopify_order(mock_shopify)
    assert parsed["platform"] == "shopify"
    assert parsed["order_id"] == "987654321"
    assert parsed["is_cod"] is True
    assert parsed["status"] == "delivered"
    assert parsed["total_price"] == 850.0

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
