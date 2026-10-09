"""
test_webhook_routes.py — S1-2: signed Shopify/Salla webhook routes (raw-body HMAC, fail closed, background processing).
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import webhook_routes
from src.ameen_workforce.capi_service import capi_sender
from src.ameen_workforce.db import CapiEvent, Incident, Order, WebhookDelivery, create_tenant
from src.ameen_workforce.service import app
from src.ameen_workforce.webhook_signatures import verify_salla_signature, verify_shopify_hmac

SHOPIFY_SECRET = "shpss_TEST_APP_SECRET_do_not_log"
SALLA_SECRET = "salla_TEST_WEBHOOK_SECRET_do_not_log"
EMAIL = "Private.Customer@example.com"
PHONE = "01012345678"
SALLA_MERCHANT = 1234567
# Placed shortly before the test runs: a fixed date would eventually fall past the placed+6.5d send cutoff.
PLACED_AT = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def shopify_order(**overrides):
    order = {
        "id": 987654321, "financial_status": "paid", "fulfillment_status": "fulfilled",
        "payment_gateway_names": ["Cash on Delivery (COD)"], "total_price": "850.00", "currency": "EGP",
        "created_at": PLACED_AT, "customer": {"email": EMAIL, "phone": PHONE},
    }
    order.update(overrides)
    return order


def salla_event(event="order.status.updated", **data_overrides):
    data = {"id": 77, "status": {"slug": "delivered"}, "payment_method": "cod",
            "customer": {"email": EMAIL, "mobile": "0501234567"},
            "amounts": {"total": {"amount": 120, "currency": "SAR"}}}
    data.update(data_overrides)
    return {"event": event, "merchant": SALLA_MERCHANT, "data": data}


def shopify_sig(raw: bytes, secret: str = SHOPIFY_SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()


def salla_sig(raw: bytes, secret: str = SALLA_SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def shopify_headers(raw: bytes, webhook_id="wh-1", shop="live.myshopify.com", sig=None):
    headers = {"Content-Type": "application/json", "X-Shopify-Shop-Domain": shop, "X-Shopify-Webhook-Id": webhook_id}
    headers["X-Shopify-Hmac-Sha256"] = shopify_sig(raw) if sig is None else sig
    return headers


def raw_json(obj) -> bytes:
    return json.dumps(obj).encode()


class SpySender:
    def __init__(self):
        self.calls = []

    async def __call__(self, pixel_id, access_token, payload):
        self.calls.append(payload)
        return {"status": "success", "fbtrace_id": "TRACE1"}


@pytest.fixture
def factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)


@pytest.fixture
def client(factory, monkeypatch):
    monkeypatch.setenv(webhook_routes.SHOPIFY_SECRET_ENV, SHOPIFY_SECRET)
    monkeypatch.setenv(webhook_routes.SALLA_SECRET_ENV, SALLA_SECRET)
    app.dependency_overrides[webhook_routes.get_session_factory_dep] = lambda: factory
    yield TestClient(app)  # no `with`: lifespan (init_db on the default file DB) is not run in tests
    app.dependency_overrides.pop(webhook_routes.get_session_factory_dep, None)


@pytest.fixture
def spy(monkeypatch):
    sender = SpySender()
    monkeypatch.setattr(capi_sender, "send_event", sender)
    return sender


@pytest.fixture
def no_process(monkeypatch):
    calls = []

    async def fake(*a, **k):
        calls.append((a, k))
    monkeypatch.setattr(webhook_routes, "process_webhook", fake)
    return calls


@pytest.fixture
def salla_tenant(db_session):
    return create_tenant(db_session, name="Salla Shop", platform="salla", shop_domain=str(SALLA_MERCHANT),
                         meta_dataset_id="333", country="SA", currency="SAR")


def rows(factory, model):
    with factory() as s:
        return s.scalars(select(model).order_by(model.id)).all()


# --- signature primitives ----------------------------------------------------------------------------------

def test_signature_verifiers_fail_closed():
    raw = b'{"a":1}'
    assert verify_shopify_hmac(raw, shopify_sig(raw), SHOPIFY_SECRET)
    assert not verify_shopify_hmac(raw, shopify_sig(raw), "")
    assert not verify_shopify_hmac(raw, None, SHOPIFY_SECRET)
    assert not verify_shopify_hmac(raw, "", SHOPIFY_SECRET)
    assert not verify_shopify_hmac(raw, shopify_sig(raw, "other"), SHOPIFY_SECRET)
    assert verify_salla_signature(raw, salla_sig(raw), SALLA_SECRET)
    assert verify_salla_signature(raw, "sha256=" + salla_sig(raw).upper(), SALLA_SECRET)
    assert not verify_salla_signature(raw, salla_sig(raw), "")
    assert not verify_salla_signature(raw, None, SALLA_SECRET)
    assert not verify_salla_signature(raw, salla_sig(b"x"), SALLA_SECRET)


# --- Shopify -----------------------------------------------------------------------------------------------

def test_shopify_valid_hmac_processes_in_background(client, factory, live_tenant, spy):
    raw = raw_json(shopify_order())
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 200 and res.json() == {"status": "accepted"}
    order = rows(factory, Order)[0]
    assert order.platform_order_id == "987654321" and order.current_status == "delivered"
    delivery = rows(factory, WebhookDelivery)[0]
    assert delivery.signature_ok is True and delivery.processed_ok is True
    assert delivery.delivery_id == "wh-1" and delivery.tenant_id == live_tenant.id and delivery.topic == "orders/updated"
    assert len(spy.calls) == 1


def test_shopify_tampered_body_is_401_and_recorded_unprocessed(client, factory, live_tenant, no_process):
    raw = raw_json(shopify_order())
    headers = shopify_headers(raw)
    tampered = raw_json(shopify_order(total_price="1.00"))
    res = client.post("/webhooks/shopify/orders/updated", content=tampered, headers=headers)
    assert res.status_code == 401
    delivery = rows(factory, WebhookDelivery)[0]
    assert delivery.signature_ok is False and delivery.processed_ok is False
    assert delivery.error_type == "invalid_signature" and delivery.tenant_id == live_tenant.id
    assert delivery.payload_sha256 == hashlib.sha256(tampered).hexdigest()
    assert no_process == [] and rows(factory, Order) == []


def test_shopify_missing_header_or_secret_fails_closed(client, factory, live_tenant, no_process, monkeypatch):
    raw = raw_json(shopify_order())
    headers = shopify_headers(raw)
    del headers["X-Shopify-Hmac-Sha256"]
    assert client.post("/webhooks/shopify/orders/updated", content=raw, headers=headers).status_code == 401

    monkeypatch.delenv(webhook_routes.SHOPIFY_SECRET_ENV)
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 401
    monkeypatch.setenv(webhook_routes.SHOPIFY_SECRET_ENV, "")
    assert client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw)).status_code == 401

    deliveries = rows(factory, WebhookDelivery)
    assert [d.error_type for d in deliveries] == ["invalid_signature", "signature_not_configured",
                                                  "signature_not_configured"]
    assert all(d.signature_ok is False for d in deliveries) and no_process == []


def test_shopify_unknown_shop_domain_is_404(client, factory, live_tenant, no_process):
    raw = raw_json(shopify_order())
    res = client.post("/webhooks/shopify/orders/updated", content=raw,
                      headers=shopify_headers(raw, shop="nobody.myshopify.com"))
    assert res.status_code == 404
    delivery = rows(factory, WebhookDelivery)[0]
    assert delivery.tenant_id is None and delivery.signature_ok is True and delivery.error_type == "unknown_tenant"
    assert no_process == []


def test_shopify_route_does_not_resolve_a_salla_tenant(client, factory, db_session, no_process):
    create_tenant(db_session, name="S", platform="salla", shop_domain="cross.myshopify.com")
    raw = raw_json(shopify_order())
    res = client.post("/webhooks/shopify/orders/updated", content=raw,
                      headers=shopify_headers(raw, shop="cross.myshopify.com"))
    assert res.status_code == 404


def test_same_webhook_id_twice_is_duplicate_and_meta_called_once(client, factory, live_tenant, spy):
    raw = raw_json(shopify_order())
    for _ in range(2):
        assert client.post("/webhooks/shopify/orders/updated", content=raw,
                           headers=shopify_headers(raw, webhook_id="wh-dup")).status_code == 200
    first, second = rows(factory, WebhookDelivery)
    assert first.is_duplicate is False and second.is_duplicate is True
    assert len(spy.calls) == 1 and len(rows(factory, CapiEvent)) == 1


def test_reserialized_json_fails_hmac_raw_body_is_what_is_verified(client, live_tenant, no_process):
    original = b'{"id":1,"financial_status":"paid"}'
    reserialized = json.dumps(json.loads(original)).encode()  # same data, different whitespace
    assert original != reserialized
    headers = shopify_headers(original)
    assert client.post("/webhooks/shopify/orders/updated", content=original, headers=headers).status_code == 200
    assert client.post("/webhooks/shopify/orders/updated", content=reserialized, headers=headers).status_code == 401


def test_unhandled_shopify_topic_is_200_recorded_not_processed(client, factory, live_tenant, no_process):
    raw = raw_json({"id": 5})
    res = client.post("/webhooks/shopify/products/update", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 200 and res.json() == {"status": "ignored"}
    delivery = rows(factory, WebhookDelivery)[0]
    assert delivery.topic == "products/update" and delivery.signature_ok is True
    assert delivery.processed_ok is False and delivery.error_type == "topic_not_handled"
    assert no_process == []


def test_invalid_json_with_valid_signature_is_400(client, factory, live_tenant, no_process):
    raw = b"not json"
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 400
    assert rows(factory, WebhookDelivery)[0].error_type == "invalid_json" and no_process == []


def test_body_over_one_mb_is_413(client, factory, live_tenant, no_process):
    raw = b'{"pad":"' + b"a" * webhook_routes.MAX_BODY_BYTES + b'"}'
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 413
    assert rows(factory, WebhookDelivery) == [] and no_process == []


def test_background_exception_records_incident_and_still_200(client, factory, live_tenant, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError(f"db exploded for {EMAIL} using {SHOPIFY_SECRET}")
    monkeypatch.setattr(webhook_routes, "process_webhook", boom)
    raw = raw_json(shopify_order())
    caplog.set_level(logging.DEBUG)
    res = client.post("/webhooks/shopify/orders/updated", content=raw, headers=shopify_headers(raw))
    assert res.status_code == 200
    incident = rows(factory, Incident)[0]
    assert incident.kind == "webhook_processing_error" and incident.tenant_id == live_tenant.id
    assert "RuntimeError" in incident.detail and EMAIL not in incident.detail and SHOPIFY_SECRET not in incident.detail
    assert EMAIL not in caplog.text and SHOPIFY_SECRET not in caplog.text  # type name only, never the message


# --- Salla -------------------------------------------------------------------------------------------------

def salla_headers(raw: bytes, sig=None):
    return {"Content-Type": "application/json", "X-Salla-Signature": salla_sig(raw) if sig is None else sig}


def test_salla_valid_signature_processed(client, factory, salla_tenant, spy):
    raw = raw_json(salla_event())
    res = client.post("/webhooks/salla", content=raw, headers=salla_headers(raw))
    assert res.status_code == 200 and res.json() == {"status": "accepted"}
    order = rows(factory, Order)[0]
    assert order.platform_order_id == "77" and order.current_status == "delivered"
    delivery = rows(factory, WebhookDelivery)[0]
    assert delivery.platform == "salla" and delivery.topic == "order.status.updated"
    assert delivery.signature_ok is True and delivery.processed_ok is True and delivery.tenant_id == salla_tenant.id


def test_salla_invalid_signature_is_401(client, factory, salla_tenant, no_process):
    raw = raw_json(salla_event())
    res = client.post("/webhooks/salla", content=raw, headers=salla_headers(raw, sig=salla_sig(b"other")))
    assert res.status_code == 401
    delivery = rows(factory, WebhookDelivery)[0]
    assert delivery.signature_ok is False and delivery.tenant_id == salla_tenant.id
    assert delivery.topic == "order.status.updated"
    raw2 = raw_json(salla_event())
    assert client.post("/webhooks/salla", content=raw2, headers={"Content-Type": "application/json"}).status_code == 401
    assert no_process == []


def test_salla_unknown_merchant_404_and_unhandled_event_ignored(client, factory, salla_tenant, no_process):
    other = salla_event()
    other["merchant"] = 999
    raw = raw_json(other)
    assert client.post("/webhooks/salla", content=raw, headers=salla_headers(raw)).status_code == 404
    shipment = raw_json(salla_event(event="order.shipment.created"))
    res = client.post("/webhooks/salla", content=shipment, headers=salla_headers(shipment))
    assert res.status_code == 200 and res.json() == {"status": "ignored"}
    assert no_process == []


# --- Logging hygiene ---------------------------------------------------------------------------------------

def test_no_secret_email_or_phone_in_logs(client, factory, live_tenant, salla_tenant, spy, caplog):
    caplog.set_level(logging.DEBUG)
    good = raw_json(shopify_order())
    client.post("/webhooks/shopify/orders/updated", content=good, headers=shopify_headers(good))
    bad = raw_json(shopify_order())
    client.post("/webhooks/shopify/orders/updated", content=bad, headers=shopify_headers(bad, sig="forged=="))
    salla = raw_json(salla_event())
    client.post("/webhooks/salla", content=salla, headers=salla_headers(salla))
    client.post("/webhooks/salla", content=salla, headers=salla_headers(salla, sig="deadbeef"))
    text = caplog.text
    for forbidden in (SHOPIFY_SECRET, SALLA_SECRET, EMAIL, EMAIL.lower(), PHONE, "0501234567", "forged==",
                      shopify_sig(good), salla_sig(salla), "deadbeef"):
        assert forbidden not in text
