"""
tests/test_onboarding_cli.py — Unit and integration tests for Operator Onboarding CLI & Salla capture.
====================================================================================================
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from scripts.onboard_store import (
    main, onboard_store, verify_capi_ping
)
from src.ameen_workforce.capi_service import MetaCAPISender
from src.ameen_workforce.checkout_context import load_checkout_context
from src.ameen_workforce.credentials import FERNET_KEY_ENV, generate_key, get_credential
from src.ameen_workforce.db import Credential, Order, Tenant, get_tenant_by_shop_domain
from src.ameen_workforce.service import app
from src.ameen_workforce.webhook_listener import OrderWebhookProcessor, extract_salla_attribution
from src.ameen_workforce.webhook_routes import get_session_factory_dep


@pytest.fixture
def test_fernet_key(monkeypatch):
    key = generate_key()
    monkeypatch.setenv(FERNET_KEY_ENV, key)
    return key


@pytest.fixture
def mock_capi_sender():
    sender = MetaCAPISender()
    sender.send_event = AsyncMock(return_value={
        "status": "success",
        "events_received": 1,
        "fbtrace_id": "TEST_TRACE_999",
        "raw": {"events_received": 1, "fbtrace_id": "TEST_TRACE_999"}
    })
    return sender


# =============================================================================
# 1. Store Onboarding Engine Tests
# =============================================================================

def test_onboard_shopify_store(db_session, test_fernet_key):
    res = onboard_store(
        session=db_session,
        name="Pilot Shopify Store",
        platform="shopify",
        shop_domain="pilot-1.myshopify.com",
        meta_dataset_id="111222333444",
        meta_capi_token="META_SECRET_ACCESS_TOKEN",
        webhook_secret="shpss_webhook_secret_key",
        mode="shadow",
        settlement_hours=12.0,
        storefront_url="https://pilot-1.com"
    )

    assert res["status"] == "success"
    assert res["action"] == "created"
    assert res["name"] == "Pilot Shopify Store"
    assert res["platform"] == "shopify"
    assert res["country"] == "EG"
    assert res["currency"] == "EGP"
    assert res["timezone"] == "Africa/Cairo"
    assert res["mode"] == "shadow"
    assert res["meta_dataset_id"] == "111222333444"

    # Verify tenant in database
    tenant = get_tenant_by_shop_domain(db_session, "pilot-1.myshopify.com")
    assert tenant is not None
    assert tenant.id == res["tenant_id"]

    # Verify credentials stored encrypted
    decrypted_token = get_credential(db_session, tenant.id, "meta_capi_token")
    assert decrypted_token == "META_SECRET_ACCESS_TOKEN"

    decrypted_wh_secret = get_credential(db_session, tenant.id, "webhook_secret")
    assert decrypted_wh_secret == "shpss_webhook_secret_key"

    # Verify secret is NOT plaintext in ciphertext column
    cred_row = db_session.scalar(select(Credential).where(Credential.tenant_id == tenant.id, Credential.kind == "meta_capi_token"))
    assert "META_SECRET_ACCESS_TOKEN" not in cred_row.ciphertext


def test_onboard_salla_store(db_session, test_fernet_key):
    res = onboard_store(
        session=db_session,
        name="Pilot Salla Store",
        platform="salla",
        shop_domain="987654",  # Salla merchant ID
        meta_dataset_id="555666777888",
        meta_capi_token="SALLA_META_TOKEN",
        mode="live",
        settlement_hours=6.0,
    )

    assert res["status"] == "success"
    assert res["action"] == "created"
    assert res["country"] == "SA"
    assert res["currency"] == "SAR"
    assert res["timezone"] == "Asia/Riyadh"
    assert res["mode"] == "live"
    assert res["settlement_hours"] == 6.0

    tenant = get_tenant_by_shop_domain(db_session, "987654")
    assert tenant is not None
    assert tenant.currency == "SAR"

    decrypted_token = get_credential(db_session, tenant.id, "meta_capi_token")
    assert decrypted_token == "SALLA_META_TOKEN"


def test_onboard_store_update_existing(db_session, test_fernet_key):
    # Initial onboarding
    res1 = onboard_store(
        session=db_session,
        name="Original Name",
        platform="shopify",
        shop_domain="existing.myshopify.com",
        meta_dataset_id="111111",
        meta_capi_token="OLD_TOKEN",
    )
    assert res1["action"] == "created"

    # Re-run onboarding with updated attributes and rotated token
    res2 = onboard_store(
        session=db_session,
        name="Updated Name",
        platform="shopify",
        shop_domain="existing.myshopify.com",
        meta_dataset_id="222222",
        meta_capi_token="NEW_ROTATED_TOKEN",
        mode="live",
        settlement_hours=24.0
    )
    assert res2["action"] == "updated"
    assert res2["tenant_id"] == res1["tenant_id"]

    tenant = get_tenant_by_shop_domain(db_session, "existing.myshopify.com")
    assert tenant.name == "Updated Name"
    assert tenant.meta_dataset_id == "222222"
    assert tenant.mode == "live"
    assert tenant.settlement_hours == 24.0

    # Credential rotated
    assert get_credential(db_session, tenant.id, "meta_capi_token") == "NEW_ROTATED_TOKEN"


def test_onboard_store_dry_run(db_session, test_fernet_key):
    res = onboard_store(
        session=db_session,
        name="Dry Run Store",
        platform="shopify",
        shop_domain="dryrun.myshopify.com",
        meta_dataset_id="333333",
        meta_capi_token="TOKEN",
        dry_run=True
    )
    assert res["status"] == "dry_run"
    assert res["action"] == "created"

    # Ensure no row written to database
    tenant = get_tenant_by_shop_domain(db_session, "dryrun.myshopify.com")
    assert tenant is None


def test_onboard_store_validations(db_session, test_fernet_key):
    # Invalid platform
    with pytest.raises(ValueError, match="Invalid platform"):
        onboard_store(db_session, "Store", "magento", "domain.com", "123", "tok")

    # Invalid mode
    with pytest.raises(ValueError, match="Invalid mode"):
        onboard_store(db_session, "Store", "shopify", "domain.com", "123", "tok", mode="invalid_mode")

    # Empty shop domain
    with pytest.raises(ValueError, match="shop_domain cannot be empty"):
        onboard_store(db_session, "Store", "shopify", "", "123", "tok")

    # Empty meta dataset id
    with pytest.raises(ValueError, match="meta_dataset_id cannot be empty"):
        onboard_store(db_session, "Store", "shopify", "domain.com", "", "tok")

    # Empty meta capi token
    with pytest.raises(ValueError, match="meta_capi_token cannot be empty"):
        onboard_store(db_session, "Store", "shopify", "domain.com", "123", "")

    # Negative settlement hours
    with pytest.raises(ValueError, match="settlement_hours must be >= 0"):
        onboard_store(db_session, "Store", "shopify", "domain.com", "123", "tok", settlement_hours=-1.0)

    # Invalid storefront url
    with pytest.raises(ValueError, match="storefront_url must start with http:// or https://"):
        onboard_store(db_session, "Store", "shopify", "domain.com", "123", "tok", storefront_url="ftp://store.com")


# =============================================================================
# 2. Meta CAPI Test Ping Verification Tests
# =============================================================================

@pytest.mark.asyncio
async def test_verify_capi_ping_success(mock_capi_sender):
    res = await verify_capi_ping(
        meta_dataset_id="123456789",
        meta_capi_token="VALID_TOKEN",
        currency="EGP",
        country="EG",
        test_event_code="TEST1234",
        storefront_url="https://test.com",
        sender=mock_capi_sender
    )
    assert res["status"] == "success"
    assert res["events_received"] == 1
    assert res["fbtrace_id"] == "TEST_TRACE_999"

    mock_capi_sender.send_event.assert_called_once()
    call_kwargs = mock_capi_sender.send_event.call_args.kwargs
    assert call_kwargs["pixel_id"] == "123456789"
    assert call_kwargs["access_token"] == "VALID_TOKEN"
    payload = call_kwargs["payload"]
    assert payload["test_event_code"] == "TEST1234"
    assert payload["data"][0]["event_source_url"] == "https://test.com"


@pytest.mark.asyncio
async def test_verify_capi_ping_error():
    failing_sender = MetaCAPISender()
    failing_sender.send_event = AsyncMock(return_value={
        "status": "error",
        "http_code": 400,
        "error_message": "Invalid OAuth access token",
        "raw": {"error": {"message": "Invalid OAuth access token"}}
    })

    res = await verify_capi_ping(
        meta_dataset_id="123456789",
        meta_capi_token="INVALID_TOKEN",
        sender=failing_sender
    )
    assert res["status"] == "error"
    assert res["http_code"] == 400
    assert "Invalid OAuth access token" in res["message"]


# =============================================================================
# 3. CLI Integration & Execution Tests
# =============================================================================

def test_cli_main_success(db_session, test_fernet_key, monkeypatch, capsys):
    # Mock verify_capi_ping so it doesn't do real HTTP
    async def mock_ping(*args, **kwargs):
        return {"status": "success", "events_received": 1, "fbtrace_id": "CLI_TRACE_1"}

    monkeypatch.setattr("scripts.onboard_store.verify_capi_ping", mock_ping)
    monkeypatch.setattr("scripts.onboard_store.session_scope", lambda: db_session)

    # Use a dummy context manager for session_scope
    from contextlib import contextmanager
    @contextmanager
    def fake_scope():
        yield db_session

    monkeypatch.setattr("scripts.onboard_store.session_scope", fake_scope)

    argv = [
        "--name", "CLI Store",
        "--platform", "shopify",
        "--shop-domain", "cli-store.myshopify.com",
        "--meta-dataset-id", "999888777",
        "--meta-capi-token", "CLI_SECRET_TOKEN",
        "--webhook-secret", "CLI_WH_SECRET",
        "--json"
    ]

    ret = main(argv)
    assert ret == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "success"
    assert data["name"] == "CLI Store"
    assert data["verification_ping"]["status"] == "success"


def test_cli_main_invalid_args_returns_error(capsys):
    ret = main(["--name", "Bad Store", "--platform", "invalid_plat", "--shop-domain", "x.com", "--meta-dataset-id", "1", "--meta-capi-token", "t", "--json"])
    assert ret != 0
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err.lower() or "error" in captured.out.lower()


# =============================================================================
# 4. S2-8 Storefront Salla Capture Endpoint & Webhook Parsing Tests
# =============================================================================

def test_salla_storefront_capture_endpoint(db_session, test_fernet_key):
    # 1. Onboard a Salla store
    tenant = Tenant(
        name="Salla Pilot Store",
        platform="salla",
        shop_domain="778899",  # merchant id
        meta_dataset_id="444555666",
        country="SA",
        currency="SAR",
        timezone="Asia/Riyadh",
        mode="live"
    )
    db_session.add(tenant)
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    client = TestClient(app)

    capture_payload = {
        "platform": "salla",
        "merchant": "778899",
        "order_id": "ORD-12345",
        "attribution": {
            "utm_source": "meta",
            "utm_medium": "paid",
            "utm_campaign": "1001",
            "utm_content": "2002",
            "ad_id": "2002",
            "fbp": "fb.1.1700000000000.1234567890",
            "fbc": "fb.1.1700000000000.ABC_DEF",
            "ttclid": "TT-123",
            "sccid": "SC-456"
        },
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36",
        "client_ip": "197.35.120.40"
    }

    # Test POST /storefront/capture
    res = client.post("/storefront/capture", json=capture_payload)
    assert res.status_code == 200
    assert res.json()["status"] == "captured"
    assert res.json()["order_id"] == "ORD-12345"

    # Verify Order created and attribution persisted
    order = db_session.scalar(select(Order).where(Order.tenant_id == tenant.id, Order.platform_order_id == "ORD-12345"))
    assert order is not None
    assert order.utm_source == "meta"
    assert order.ad_id == "2002"
    assert order.fbp == "fb.1.1700000000000.1234567890"
    assert order.fbc == "fb.1.1700000000000.ABC_DEF"
    assert order.ttclid == "TT-123"
    assert order.sccid == "SC-456"

    # Verify Checkout Context encrypted (client_ip and user_agent)
    ip, ua = load_checkout_context(db_session, order.id)
    assert ip == "197.35.120.40"
    assert ua == "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36"

    # Test alias POST /webhooks/salla/capture
    capture_payload["order_id"] = "ORD-67890"
    res2 = client.post("/webhooks/salla/capture", json=capture_payload)
    assert res2.status_code == 200
    assert res2.json()["status"] == "captured"

    app.dependency_overrides.pop(get_session_factory_dep, None)


def test_parse_salla_order_with_source_details_and_notes():
    payload_with_source_details = {
        "event": "order.status.updated",
        "merchant": "778899",
        "data": {
            "id": "ORD-SOURCE-1",
            "status": {"slug": "delivered"},
            "payment_method": "cod",
            "amounts": {"total": {"amount": 350, "currency": "SAR"}},
            "customer": {"email": "cust@example.com", "mobile": "0501112233"},
            "source_details": {
                "utm_source": "meta",
                "utm_medium": "paid",
                "utm_campaign": "888",
                "utm_content": "999",
                "ad_id": "999",
                "ip": "197.35.120.50",
                "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4)"
            }
        }
    }

    parsed = OrderWebhookProcessor.parse_salla_order(payload_with_source_details)
    assert parsed["platform"] == "salla"
    assert parsed["order_id"] == "ORD-SOURCE-1"
    assert parsed["status"] == "delivered"
    assert parsed["client_ip"] == "197.35.120.50"
    assert parsed["user_agent"] == "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4)"
    assert parsed["attribution"]["utm_source"] == "meta"
    assert parsed["attribution"]["ad_id"] == "999"


def test_parse_salla_order_with_embedded_notes():
    payload_with_notes = {
        "event": "order.status.updated",
        "merchant": "778899",
        "data": {
            "id": "ORD-NOTES-1",
            "status": {"slug": "delivered"},
            "payment_method": "cod",
            "amounts": {"total": {"amount": 200, "currency": "SAR"}},
            "note": "_mt_utm_source=tiktok\n_mt_ad_id=12345\n_mt_fbp=fb.1.1700000000000.1234567890\n_mt_ttclid=TT_XYZ"
        }
    }

    parsed = OrderWebhookProcessor.parse_salla_order(payload_with_notes)
    assert parsed["attribution"]["utm_source"] == "tiktok"
    assert parsed["attribution"]["ad_id"] == "12345"
    assert parsed["attribution"]["fbp"] == "fb.1.1700000000000.1234567890"
    assert parsed["attribution"]["ttclid"] == "TT_XYZ"
