"""
tests/test_onboarding_cli.py — Unit and integration tests for Operator Onboarding CLI & Salla capture.
====================================================================================================
"""

import json
import logging
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
        test_event_code="TEST1234",
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
        "--verify-capi-ping",
        "--test-event-code", "TEST_CLI_1",
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

def test_old_storefront_capture_routes_are_gone(db_session):
    """FX-2: the unauthenticated /storefront/capture and /webhooks/salla/capture routes no longer exist."""
    client = TestClient(app)
    for path in ("/storefront/capture", "/webhooks/salla/capture"):
        res = client.post(path, json={"merchant": "778899", "order_id": "ORD-1"})
        assert res.status_code in (404, 405)


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


# =============================================================================
# 5. Connection-test guard (R5) and courier webhook secrets
# =============================================================================

BASE_CLI_ARGS = [
    "--name", "CLI Store",
    "--platform", "shopify",
    "--shop-domain", "cli-store.myshopify.com",
    "--meta-dataset-id", "999888777",
    "--meta-capi-token", "CLI_SECRET_TOKEN",
]


@pytest.fixture
def cli_db(db_session, test_fernet_key, monkeypatch):
    """Routes the CLI to the in-memory session and skips touching the real database file."""
    from contextlib import contextmanager

    @contextmanager
    def fake_scope(*_args, **_kwargs):
        yield db_session

    monkeypatch.setattr("scripts.onboard_store.session_scope", fake_scope)
    monkeypatch.setattr("scripts.onboard_store.init_db", lambda *a, **k: None)
    return db_session


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, "", "   "])
async def test_verify_capi_ping_refuses_without_test_event_code(mock_capi_sender, code):
    with pytest.raises(ValueError, match="test_event_code"):
        await verify_capi_ping(
            meta_dataset_id="123456789", meta_capi_token="VALID_TOKEN", test_event_code=code, sender=mock_capi_sender
        )
    mock_capi_sender.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_verify_capi_ping_sends_connection_test_event_not_delivered_purchase(mock_capi_sender):
    res = await verify_capi_ping(
        meta_dataset_id="123456789", meta_capi_token="VALID_TOKEN", test_event_code="TEST1234", sender=mock_capi_sender
    )
    payload = mock_capi_sender.send_event.call_args.kwargs["payload"]
    assert payload["data"][0]["event_name"] == "MotahaiConnectionTest"
    assert payload["test_event_code"] == "TEST1234"
    assert res["event_name"] == "MotahaiConnectionTest"


def test_cli_verify_ping_without_test_event_code_exits_nonzero_and_sends_nothing(cli_db, monkeypatch, capsys):
    send = AsyncMock(return_value={"status": "success"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)

    ret = main(BASE_CLI_ARGS + ["--verify-capi-ping", "--json"])

    assert ret != 0
    send.assert_not_called()
    assert get_tenant_by_shop_domain(cli_db, "cli-store.myshopify.com") is None  # refused before any write
    assert "--test-event-code" in json.loads(capsys.readouterr().out)["message"]


def test_cli_default_run_sends_no_ping(cli_db, monkeypatch):
    send = AsyncMock(return_value={"status": "success"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)

    assert main(BASE_CLI_ARGS + ["--json"]) == 0
    send.assert_not_called()


def test_cli_verify_ping_with_test_event_code_sends_connection_test(cli_db, monkeypatch):
    send = AsyncMock(return_value={"status": "success", "events_received": 1, "fbtrace_id": "T1"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)

    assert main(BASE_CLI_ARGS + ["--verify-capi-ping", "--test-event-code", "TEST9", "--json"]) == 0

    send.assert_called_once()
    payload = send.call_args.kwargs["payload"]
    assert payload["data"][0]["event_name"] == "MotahaiConnectionTest"
    assert payload["test_event_code"] == "TEST9"


def test_cli_courier_secret_flags_store_credentials_of_the_right_kinds(cli_db, capsys):
    assert main(BASE_CLI_ARGS + ["--bosta-webhook-secret", "BOSTA_SECRET_VALUE",
                                 "--oto-webhook-secret", "OTO_SECRET_VALUE", "--json"]) == 0

    tenant = get_tenant_by_shop_domain(cli_db, "cli-store.myshopify.com")
    assert get_credential(cli_db, tenant.id, "bosta_webhook_secret") == "BOSTA_SECRET_VALUE"
    assert get_credential(cli_db, tenant.id, "oto_webhook_secret") == "OTO_SECRET_VALUE"
    out = capsys.readouterr().out
    assert "BOSTA_SECRET_VALUE" not in out and "OTO_SECRET_VALUE" not in out


def test_cli_generate_courier_secrets_prints_once_and_never_logs(cli_db, caplog, capsys):
    caplog.set_level(logging.DEBUG)

    assert main(BASE_CLI_ARGS + ["--generate-courier-secrets"]) == 0

    captured = capsys.readouterr()
    tenant = get_tenant_by_shop_domain(cli_db, "cli-store.myshopify.com")
    bosta = get_credential(cli_db, tenant.id, "bosta_webhook_secret")
    oto = get_credential(cli_db, tenant.id, "oto_webhook_secret")
    assert len(bosta) >= 43 and len(oto) >= 43 and bosta != oto  # token_urlsafe(32)
    # printed exactly once, on stdout only
    assert captured.out.count(bosta) == 1
    assert captured.out.count(oto) == 1
    assert bosta not in captured.err and oto not in captured.err
    # per-tenant webhook paths are shown
    assert "/webhooks/bosta/cli-store.myshopify.com" in captured.out
    assert "/webhooks/oto/cli-store.myshopify.com" in captured.out
    # never logged
    assert bosta not in caplog.text and oto not in caplog.text


def test_cli_generate_conflicts_with_explicit_courier_secret(cli_db):
    assert main(BASE_CLI_ARGS + ["--generate-courier-secrets", "--bosta-webhook-secret", "X", "--json"]) != 0
    assert get_tenant_by_shop_domain(cli_db, "cli-store.myshopify.com") is None


def test_cli_refuses_real_run_without_fernet_key(monkeypatch, capsys):
    monkeypatch.delenv(FERNET_KEY_ENV, raising=False)

    assert main(BASE_CLI_ARGS + ["--json"]) != 0
    assert FERNET_KEY_ENV in json.loads(capsys.readouterr().out)["message"]
