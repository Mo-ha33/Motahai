"""
test_verify_test_event.py — S3-3: per-store Meta test_event_code verification of shadow events.

verify_test_event re-sends a tenant's latest SHADOW event with its meta_test_event_code so it shows in Events Manager ->
Test Events. It must never touch the original capi_events row, never create a `sent` row, never purge checkout IP/UA,
and never call Meta without a test code.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from scripts.onboard_store import main, parse_cli_args, run_verification
from src.ameen_workforce.capi_service import MetaCAPISender
from src.ameen_workforce.checkout_context import capture_checkout_context
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import (
    CapiEvent, CheckoutContext, Order, Tenant, create_tenant, get_tenant_by_shop_domain,
    set_meta_test_event_code
)
from src.ameen_workforce.order_pipeline import process_webhook, verify_test_event
from tests.test_order_pipeline import DELIVERED, FAIL, FakeSender, shopify_order

CODE = "TEST4242"
BASE_CLI_ARGS = [
    "--name", "Verify Store", "--platform", "shopify", "--shop-domain", "verify-cli.myshopify.com",
    "--meta-dataset-id", "555", "--meta-capi-token", "CLI_TOKEN",
]


async def shadow_with_events(session, tenant):
    """Runs a delivered order through a shadow tenant (0h settlement): ConfirmedOrder + DeliveredPurchase, both `shadow`."""
    res = await process_webhook(session, tenant, "shopify", "orders/updated", shopify_order(**DELIVERED),
                                sender=FakeSender())
    assert res["action"] == "SHADOW"
    return session.scalar(select(Order))


def snapshot(session):
    return [(e.id, e.event_name, e.status, e.attempts, e.http_code, e.error_type, e.sent_at, e.fbtrace_id)
            for e in session.scalars(select(CapiEvent).order_by(CapiEvent.id))]


@pytest.fixture
def ready(db_session, shadow_tenant, fernet_key):
    """Shadow tenant with a Meta token, a test code, and shadow events (checkout IP/UA captured for the order)."""
    store_credential(db_session, shadow_tenant.id, "meta_capi_token", "SECRET-META-TOKEN-123")
    set_meta_test_event_code(db_session, shadow_tenant, CODE)
    return shadow_tenant


# --- refusals: no network ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_test_code_means_no_http_call(db_session, shadow_tenant, fernet_key):
    store_credential(db_session, shadow_tenant.id, "meta_capi_token", "SECRET-META-TOKEN-123")
    await shadow_with_events(db_session, shadow_tenant)
    sender = FakeSender()
    sender.send_event = AsyncMock()

    res = await verify_test_event(db_session, shadow_tenant, sender=sender)

    assert res == {"status": "error", "error": "no_test_event_code"}
    sender.send_event.assert_not_called()
    assert shadow_tenant.test_event_verified_at is None


@pytest.mark.asyncio
async def test_no_shadow_event_means_no_http_call(db_session, ready):
    sender = FakeSender()
    sender.send_event = AsyncMock()
    res = await verify_test_event(db_session, ready, sender=sender)
    assert res["error"] == "no_shadow_event"
    sender.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_missing_token_means_no_http_call(db_session, shadow_tenant, fernet_key):
    set_meta_test_event_code(db_session, shadow_tenant, CODE)  # no meta_capi_token stored
    await shadow_with_events(db_session, shadow_tenant)
    sender = FakeSender()
    sender.send_event = AsyncMock()
    res = await verify_test_event(db_session, shadow_tenant, sender=sender)
    assert res["error"] == "missing_credential"
    sender.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_event_past_metas_window_is_not_sent(db_session, ready):
    await shadow_with_events(db_session, ready)
    sender = FakeSender()
    sender.send_event = AsyncMock()
    later = datetime.now(timezone.utc) + timedelta(days=8)  # placed ~2h ago: now beyond placed+6.5d and 7 days

    res = await verify_test_event(db_session, ready, sender=sender, now=later)

    assert res["error"] == "event_too_old"
    sender.send_event.assert_not_called()
    assert ready.test_event_verified_at is None


# --- the happy path ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_sends_the_live_payload_plus_the_test_code(db_session, ready, fernet_key):
    await shadow_with_events(db_session, ready)
    sender = FakeSender()

    res = await verify_test_event(db_session, ready, sender=sender)

    assert res["status"] == "success" and res["event_name"] == "DeliveredPurchase"
    assert len(sender.all_calls) == 1
    call = sender.all_calls[0]
    assert call["pixel_id"] == "111" and call["access_token"] == "SECRET-META-TOKEN-123"
    assert call["payload"]["test_event_code"] == CODE

    # Same event the live path produces for the same order: a live tenant fed the same webhook.
    live = create_tenant(db_session, name="Live Twin", platform="shopify", shop_domain="twin.myshopify.com",
                         meta_dataset_id="222", mode="live", settlement_hours=0)
    store_credential(db_session, live.id, "meta_capi_token", "LIVE-TOKEN")
    live_sender = FakeSender()
    await process_webhook(db_session, live, "shopify", "orders/updated", shopify_order(**DELIVERED), sender=live_sender)
    live_event = live_sender.calls[0]["payload"]["data"][0]
    event = call["payload"]["data"][0]
    # Everything but event_id is identical to the live event; the replay's event_id is the live one with a prefix.
    assert event.keys() == live_event.keys()
    # (event_source_url is excluded only because the twin is a different tenant with its own shop domain.)
    for key in event.keys() - {"event_id", "event_source_url"}:
        assert event[key] == live_event[key], key
    assert event["event_id"] == "verify_" + live_event["event_id"] != live_event["event_id"]
    assert res["event_id"] == event["event_id"] and res["source_event_id"] == live_event["event_id"]
    assert "test_event_code" not in live_sender.calls[0]["payload"]
    stored = db_session.scalar(select(CapiEvent).where(CapiEvent.tenant_id == ready.id,
                                                       CapiEvent.event_name == "DeliveredPurchase"))
    assert (event["event_id"], event["event_time"]) == ("verify_" + stored.event_id, stored.event_time)
    assert stored.event_id == res["source_event_id"]  # the stored (live) event keeps its real id


@pytest.mark.asyncio
async def test_original_event_and_checkout_context_are_untouched(db_session, ready, fernet_key):
    order = await shadow_with_events(db_session, ready)
    capture_checkout_context(db_session, order.id, "203.0.113.9", "Mozilla/5.0 test")
    db_session.commit()
    before = snapshot(db_session)
    sender = FakeSender()

    res = await verify_test_event(db_session, ready, sender=sender)

    assert res["status"] == "success"
    assert snapshot(db_session) == before                                    # same rows, same statuses
    assert not db_session.scalars(select(CapiEvent).where(CapiEvent.status == "sent")).all()
    assert db_session.scalar(select(CheckoutContext).where(CheckoutContext.order_id == order.id)) is not None
    # ...and the stored IP/UA did reach the payload, exactly like a live send.
    user_data = sender.all_calls[0]["payload"]["data"][0]["user_data"]
    assert user_data["client_user_agent"] == "Mozilla/5.0 test" and user_data["client_ip_address"] == "203.0.113.9"


@pytest.mark.asyncio
async def test_verified_at_set_on_success(db_session, ready):
    await shadow_with_events(db_session, ready)
    now = datetime.now(timezone.utc)  # the order was placed ~2h before the real clock: inside Meta's window
    res = await verify_test_event(db_session, ready, sender=FakeSender(), now=now)
    assert res["verified_at"] == now
    db_session.expire_all()
    assert db_session.get(Tenant, ready.id).test_event_verified_at == now


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [FAIL, {"status": "failed", "error": "ConnectTimeout"}])
async def test_verified_at_not_set_on_failure(db_session, ready, failure):
    await shadow_with_events(db_session, ready)
    sender = FakeSender(results=[failure])

    res = await verify_test_event(db_session, ready, sender=sender)

    assert res["status"] == "error"
    assert res["error"] == ("meta_api_error" if failure is FAIL else "ConnectTimeout")
    assert "SECRET-META-TOKEN-123" not in repr(res) and "boom" not in repr(res)
    assert ready.test_event_verified_at is None
    assert {e.status for e in db_session.scalars(select(CapiEvent))} == {"shadow"}


@pytest.mark.asyncio
async def test_works_for_live_tenants_too(db_session, ready):
    await shadow_with_events(db_session, ready)
    ready.mode = "live"
    db_session.commit()
    res = await verify_test_event(db_session, ready, sender=FakeSender())
    assert res["status"] == "success"
    assert {e.status for e in db_session.scalars(select(CapiEvent))} == {"shadow"}


@pytest.mark.asyncio
async def test_changing_the_code_resets_verification(db_session, ready):
    await shadow_with_events(db_session, ready)
    await verify_test_event(db_session, ready, sender=FakeSender())
    assert ready.test_event_verified_at is not None
    set_meta_test_event_code(db_session, ready, CODE)          # same code: stays verified
    assert ready.test_event_verified_at is not None
    set_meta_test_event_code(db_session, ready, "TEST9999")    # different code: must be verified again
    assert ready.test_event_verified_at is None


# --- one-shot replay ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_shot_code_works_without_a_stored_code_and_stores_nothing(db_session, shadow_tenant, fernet_key):
    store_credential(db_session, shadow_tenant.id, "meta_capi_token", "SECRET-META-TOKEN-123")
    await shadow_with_events(db_session, shadow_tenant)
    sender = FakeSender()

    res = await verify_test_event(db_session, shadow_tenant, sender=sender, test_event_code="ONESHOT1")

    assert res["status"] == "success"
    assert sender.all_calls[0]["payload"]["test_event_code"] == "ONESHOT1"
    db_session.expire_all()
    tenant = db_session.get(Tenant, shadow_tenant.id)
    assert tenant.meta_test_event_code is None and tenant.test_event_verified_at is not None


@pytest.mark.asyncio
async def test_one_shot_code_overrides_the_stored_code_and_bad_codes_send_nothing(db_session, ready):
    await shadow_with_events(db_session, ready)
    sender = FakeSender()
    res = await verify_test_event(db_session, ready, sender=sender, test_event_code="OTHER2")
    assert res["status"] == "success" and sender.all_calls[0]["payload"]["test_event_code"] == "OTHER2"
    assert ready.meta_test_event_code == CODE

    sender.send_event = AsyncMock()
    assert (await verify_test_event(db_session, ready, sender=sender, test_event_code="bad code!"))["error"] ==         "invalid_test_event_code"
    sender.send_event.assert_not_called()


# --- onboard_store ----------------------------------------------------------------------------------------

@pytest.fixture
def cli_db(db_session, fernet_key, monkeypatch):
    @contextmanager
    def fake_scope(*_a, **_k):
        yield db_session

    monkeypatch.setattr("scripts.onboard_store.session_scope", fake_scope)
    monkeypatch.setattr("scripts.onboard_store.init_db", lambda *a, **k: None)
    return db_session


@pytest.mark.asyncio
async def test_cli_verification_without_any_code_sends_nothing(cli_db):
    assert main(BASE_CLI_ARGS) == 0  # tenant exists, no code anywhere
    args = parse_cli_args(BASE_CLI_ARGS + ["--verify-capi-ping"])
    args.test_event_code = args.pipeline_test_event_code = None
    sender = MetaCAPISender()
    sender.send_event = AsyncMock()

    res = await run_verification(args, {"shop_domain": "verify-cli.myshopify.com", "currency": "EGP", "country": "EG"},
                                 sender=sender)

    assert res["status"] == "skipped" and "nothing was sent" in res["message"]
    sender.send_event.assert_not_called()


def test_cli_flag_requires_a_code_and_sends_nothing_without_one(cli_db, monkeypatch):
    send = AsyncMock(return_value={"status": "success"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)
    assert main(BASE_CLI_ARGS + ["--verify-capi-ping"]) == 2
    send.assert_not_called()


def test_cli_stores_the_code_and_falls_back_to_a_connection_test_before_any_shadow_event(cli_db, monkeypatch):
    send = AsyncMock(return_value={"status": "success", "events_received": 1, "fbtrace_id": "T"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)

    assert main(BASE_CLI_ARGS + ["--meta-test-event-code", CODE, "--verify-capi-ping", "--json"]) == 0

    tenant = get_tenant_by_shop_domain(cli_db, "verify-cli.myshopify.com")
    assert tenant.meta_test_event_code == CODE
    send.assert_called_once()
    payload = send.call_args.kwargs["payload"]
    assert payload["test_event_code"] == CODE
    assert payload["data"][0]["event_name"] == "MotahaiConnectionTest"  # never a real DeliveredPurchase
    assert tenant.test_event_verified_at is None


@pytest.mark.asyncio
async def test_cli_verification_uses_the_shadow_event_and_records_verified_at(cli_db, monkeypatch):
    assert main(BASE_CLI_ARGS + ["--meta-test-event-code", CODE, "--json"]) == 0
    tenant = get_tenant_by_shop_domain(cli_db, "verify-cli.myshopify.com")
    tenant.settlement_hours = 0
    cli_db.commit()
    await shadow_with_events(cli_db, tenant)
    send = AsyncMock(return_value={"status": "success", "events_received": 1, "fbtrace_id": "T"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)
    before = snapshot(cli_db)

    args = parse_cli_args(BASE_CLI_ARGS + ["--verify-capi-ping", "--meta-test-event-code", CODE])
    res = await run_verification(args, {"shop_domain": "verify-cli.myshopify.com"})

    assert res["status"] == "success" and res["event_name"] == "DeliveredPurchase"
    send.assert_called_once()
    assert send.call_args.kwargs["payload"]["test_event_code"] == CODE
    assert get_tenant_by_shop_domain(cli_db, "verify-cli.myshopify.com").test_event_verified_at is not None
    assert snapshot(cli_db) == before


@pytest.mark.asyncio
async def test_cli_test_event_code_flag_is_a_one_shot_replay_that_is_not_stored(cli_db, monkeypatch):
    assert main(BASE_CLI_ARGS + ["--json"]) == 0
    tenant = get_tenant_by_shop_domain(cli_db, "verify-cli.myshopify.com")
    tenant.settlement_hours = 0
    cli_db.commit()
    await shadow_with_events(cli_db, tenant)
    send = AsyncMock(return_value={"status": "success", "events_received": 1, "fbtrace_id": "T"})
    monkeypatch.setattr(MetaCAPISender, "send_event", send)

    args = parse_cli_args(BASE_CLI_ARGS + ["--verify-capi-ping", "--test-event-code", "ONESHOT3"])
    res = await run_verification(args, {"shop_domain": "verify-cli.myshopify.com"})

    assert res["event_name"] == "DeliveredPurchase"
    assert send.call_args.kwargs["payload"]["test_event_code"] == "ONESHOT3"
    tenant = get_tenant_by_shop_domain(cli_db, "verify-cli.myshopify.com")
    assert tenant.meta_test_event_code is None and tenant.test_event_verified_at is not None
