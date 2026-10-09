"""
test_order_pipeline.py — S1-1 persistence + idempotency: dedupe, shadow/live, retries, no raw PII at rest, credentials.
"""

import copy
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, select, text

from src.ameen_workforce.capi_service import MetaCAPISender, hash_email, hash_phone
from src.ameen_workforce.credentials import (
    CredentialConfigError, FERNET_KEY_ENV, get_credential, store_credential
)
from src.ameen_workforce.db import (
    CapiEvent, Credential, Order, OrderStatusEvent, WebhookDelivery, create_tenant, get_tenant_by_shop_domain
)
from src.ameen_workforce.order_pipeline import process_webhook, retry_failed_events

EMAIL = "Private.Customer@example.com"
PHONE = "01012345678"
# Placed shortly before the test runs: a fixed date would eventually fall past the placed+6.5d send cutoff.
PLACED_AT = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def shopify_order(**overrides):
    order = {
        "id": 987654321, "financial_status": "pending", "fulfillment_status": None,
        "payment_gateway_names": ["Cash on Delivery (COD)"], "total_price": "850.00", "currency": "EGP",
        "created_at": PLACED_AT,
        "customer": {"email": EMAIL, "phone": PHONE},
    }
    order.update(overrides)
    return order


DELIVERED = dict(financial_status="paid", fulfillment_status="fulfilled")


class FakeSender(MetaCAPISender):
    """Real D-005 logic, fake network."""
    def __init__(self, results=None):
        super().__init__()
        self.calls = []
        self.results = list(results or [])

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append({"pixel_id": pixel_id, "access_token": access_token, "payload": payload})
        if self.results:
            return self.results.pop(0)
        return {"status": "success", "fbtrace_id": "TRACE1"}


FAIL = {"status": "error", "http_code": 500, "error_message": "boom"}


def events(session):
    return session.scalars(select(CapiEvent)).all()


async def deliver(session, tenant, payload, sender, topic="orders/updated", delivery_id=None, platform="shopify", **kw):
    return await process_webhook(session, tenant, platform, topic, payload, delivery_id=delivery_id, sender=sender, **kw)


# --- idempotency ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_webhook_twice_sends_once(db_session, live_tenant):
    sender = FakeSender()
    first = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    second = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    assert first["action"] == "SENT" and first["capi_status"] == "sent"
    assert second["action"] == "DUPLICATE_DELIVERY"
    assert len(sender.calls) == 1
    assert len(events(db_session)) == 1
    deliveries = db_session.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all()
    assert [d.is_duplicate for d in deliveries] == [False, True]


@pytest.mark.asyncio
async def test_platform_delivery_id_dedupes_even_if_payload_differs(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender, delivery_id="wh-1")
    res = await deliver(db_session, live_tenant, shopify_order(note="changed", **DELIVERED), sender, delivery_id="wh-1")
    assert res["action"] == "DUPLICATE_DELIVERY"
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_orders_updated_repeated_after_delivered_is_already_emitted(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    again = await deliver(db_session, live_tenant, shopify_order(note="edited later", **DELIVERED), sender)
    assert again["action"] == "ALREADY_EMITTED"
    assert len(sender.calls) == 1 and len(events(db_session)) == 1


@pytest.mark.asyncio
async def test_late_pending_webhook_does_not_regress_delivered_order(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    res = await deliver(db_session, live_tenant, shopify_order(note="stale redelivery"), sender)
    assert res["action"] == "ALREADY_EMITTED"
    assert db_session.scalar(select(Order)).current_status == "delivered"


@pytest.mark.asyncio
async def test_failed_processing_delivery_is_reprocessed_not_deduped(db_session, live_tenant):
    class Boom(FakeSender):
        def process_cod_order_event(self, *a, **k):
            raise RuntimeError("db hiccup")
    with pytest.raises(RuntimeError):
        await deliver(db_session, live_tenant, shopify_order(**DELIVERED), Boom())
    res = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), FakeSender())
    assert res["action"] == "SENT"
    rows = db_session.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all()
    assert [r.processed_ok for r in rows] == [False, True]
    assert rows[0].error_type == "RuntimeError"


# --- shadow / live ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_tenant_records_shadow_and_never_calls_meta(db_session, shadow_tenant):
    sender = FakeSender()
    res = await deliver(db_session, shadow_tenant, shopify_order(**DELIVERED), sender)
    assert res["action"] == "SHADOW"
    assert [e.status for e in events(db_session)] == ["shadow"]
    assert sender.calls == []


@pytest.mark.asyncio
async def test_live_tenant_uses_encrypted_token_and_records_fbtrace(db_session, live_tenant):
    sender = FakeSender([{"status": "success", "fbtrace_id": "ABC123"}])
    res = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    assert res["action"] == "SENT"
    call = sender.calls[0]
    assert call["pixel_id"] == "222" and call["access_token"] == "SECRET-META-TOKEN-123"
    event_obj = call["payload"]["data"][0]
    assert event_obj["event_name"] == "DeliveredPurchase" and event_obj["event_id"] == "delivered_987654321"
    assert event_obj["user_data"]["em"] == [hash_email(EMAIL)]
    event = events(db_session)[0]
    assert event.status == "sent" and event.fbtrace_id == "ABC123" and event.attempts == 1 and event.sent_at
    # the token is stored encrypted, never in plaintext
    assert "SECRET-META-TOKEN-123" not in db_session.scalar(select(Credential.ciphertext))


@pytest.mark.asyncio
async def test_live_tenant_without_token_fails_without_calling_meta(db_session, fernet_key):
    tenant = create_tenant(db_session, name="NoTok", platform="shopify", shop_domain="notok.myshopify.com",
                           meta_dataset_id="333", mode="live", settlement_hours=0)
    sender = FakeSender()
    res = await deliver(db_session, tenant, shopify_order(**DELIVERED), sender)
    assert res["action"] == "FAILED"
    event = events(db_session)[0]
    assert event.status == "failed" and event.error_type == "missing_credential" and event.attempts == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_inactive_tenant_is_not_processed(db_session, live_tenant):
    live_tenant.active = False
    db_session.commit()
    sender = FakeSender()
    res = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    assert res["action"] == "TENANT_INACTIVE"
    assert sender.calls == [] and db_session.scalars(select(Order)).all() == []


# --- failures and retry -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_failure_then_retry_marks_sent(db_session, live_tenant):
    sender = FakeSender([FAIL])
    res = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    assert res["action"] == "FAILED"
    event = events(db_session)[0]
    assert event.status == "failed" and event.http_code == 500 and event.error_type == "meta_api_error"
    assert event.attempts == 1

    counts = await retry_failed_events(db_session, sender=sender)
    assert counts["retried"] == 1 and counts["sent"] == 1
    db_session.refresh(event)
    assert event.status == "sent" and event.attempts == 2 and event.fbtrace_id == "TRACE1"
    assert len(sender.calls) == 2
    # resent payload keeps the same idempotent event_id and the stored (not re-hashed) customer hashes
    resent = sender.calls[1]["payload"]["data"][0]
    assert resent["event_id"] == "delivered_987654321"
    assert resent["user_data"]["em"] == [hash_email(EMAIL)]
    assert resent["user_data"]["ph"] == [hash_phone(PHONE, "EGP")]
    assert (await retry_failed_events(db_session, sender=sender))["retried"] == 0


@pytest.mark.asyncio
async def test_retry_stops_after_max_attempts(db_session, live_tenant):
    sender = FakeSender([FAIL] * 10)
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)  # attempt 1
    for _ in range(5):
        await retry_failed_events(db_session, max_attempts=3, sender=sender)
    event = events(db_session)[0]
    assert event.status == "failed" and event.attempts == 3
    assert len(sender.calls) == 3


@pytest.mark.asyncio
async def test_network_exception_type_is_recorded(db_session, live_tenant):
    sender = FakeSender([{"status": "failed", "error": "ConnectTimeout"}])
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    assert events(db_session)[0].error_type == "ConnectTimeout"


@pytest.mark.asyncio
async def test_retry_skips_order_cancelled_since(db_session, live_tenant):
    sender = FakeSender([FAIL])
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    await deliver(db_session, live_tenant, shopify_order(cancelled_at="2026-10-10T10:00:00Z", **DELIVERED), sender)
    counts = await retry_failed_events(db_session, sender=sender)
    assert counts["stale"] == 1 and len(sender.calls) == 1
    assert events(db_session)[0].error_type == "no_longer_eligible"


@pytest.mark.asyncio
async def test_retry_picks_up_orphaned_pending_claim(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender)
    event = events(db_session)[0]
    event.status, event.sent_at, event.attempts = "pending", None, 0
    event.created_at = event.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()
    counts = await retry_failed_events(db_session, sender=sender)
    assert counts["sent"] == 1
    assert events(db_session)[0].status == "sent"


@pytest.mark.asyncio
async def test_retry_only_loads_failed_or_orphaned_rows(db_session, live_tenant):
    sender = FakeSender()
    for i in range(1, 6):  # sent rows
        await deliver(db_session, live_tenant, shopify_order(id=i, **DELIVERED), sender)
    stale_sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(id=10, **DELIVERED), stale_sender,
                  event_time=int(time.time()) - 8 * 24 * 3600)  # stale row
    failing = FakeSender([FAIL])
    await deliver(db_session, live_tenant, shopify_order(id=11, **DELIVERED), failing)
    assert sorted(e.status for e in events(db_session)) == ["failed", "sent", "sent", "sent", "sent", "sent", "stale"]
    sender.calls.clear()
    counts = await retry_failed_events(db_session, sender=sender)
    assert counts == {"retried": 1, "sent": 1, "failed": 0, "stale": 0, "late_delivery": 0, "skipped": 0}
    assert len(sender.calls) == 1 and sender.calls[0]["payload"]["data"][0]["event_id"] == "delivered_11"


# --- fulfillments ---------------------------------------------------------------------------------------

FULFILLMENT = {"id": 555, "order_id": 987654321, "status": "success", "shipment_status": "delivered"}


@pytest.mark.asyncio
async def test_fulfillment_delivered_for_known_order_emits_with_stored_hashes(db_session, live_tenant):
    sender = FakeSender()
    held = await deliver(db_session, live_tenant, shopify_order(fulfillment_status="fulfilled"), sender)
    assert held["action"] == "DEFERRED"  # COD, shipped but not delivered
    res = await deliver(db_session, live_tenant, FULFILLMENT, sender, topic="fulfillments/update")
    assert res["action"] == "SENT"
    event_obj = sender.calls[0]["payload"]["data"][0]
    assert event_obj["custom_data"] == {"currency": "EGP", "value": 850.0, "order_id": "987654321"}
    assert event_obj["user_data"]["em"] == [hash_email(EMAIL)]
    assert event_obj["user_data"]["ph"] == [hash_phone(PHONE, "EGP")]
    assert db_session.scalar(select(Order)).current_status == "delivered"
    statuses = [e.status for e in db_session.scalars(select(OrderStatusEvent).order_by(OrderStatusEvent.id))]
    assert statuses == ["shipped", "delivered"]
    # a repeated delivered fulfillment (different id) does not resend
    again = await deliver(db_session, live_tenant, {**FULFILLMENT, "id": 556}, sender, topic="fulfillments/update")
    assert again["action"] == "ALREADY_EMITTED" and len(sender.calls) == 1


@pytest.mark.asyncio
async def test_no_raw_pii_anywhere_in_database(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(fulfillment_status="fulfilled"), sender)
    await deliver(db_session, live_tenant, FULFILLMENT, sender, topic="fulfillments/update")
    needles = [EMAIL.lower(), EMAIL, "private.customer", PHONE, PHONE[1:], "201012345678"]
    for table in inspect(db_session.get_bind()).get_table_names():
        for row in db_session.execute(text(f"SELECT * FROM {table}")).all():
            blob = " ".join(str(v) for v in row)
            for needle in needles:
                assert needle not in blob, f"raw PII {needle!r} found in {table}"
    order = db_session.scalar(select(Order))
    assert order.email_hash == hash_email(EMAIL) and order.phone_hash == hash_phone(PHONE, "EGP")


@pytest.mark.asyncio
async def test_fulfillment_for_unknown_order_needs_context_and_is_reprocessable(db_session, live_tenant):
    sender = FakeSender()
    res = await deliver(db_session, live_tenant, FULFILLMENT, sender, topic="fulfillments/update")
    assert res["action"] == "NEEDS_ORDER_CONTEXT"
    assert sender.calls == [] and events(db_session) == []
    # once the order is known, the SAME fulfillment redelivery is processed (not treated as a duplicate)
    await deliver(db_session, live_tenant, shopify_order(fulfillment_status="fulfilled"), sender)
    res = await deliver(db_session, live_tenant, FULFILLMENT, sender, topic="fulfillments/update")
    assert res["action"] == "SENT"


# --- D-005 outcomes ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cod_pending_deferred_and_cancelled_suppressed_write_no_capi_row(db_session, live_tenant):
    sender = FakeSender()
    assert (await deliver(db_session, live_tenant, shopify_order(), sender))["action"] == "DEFERRED"
    cancelled = await deliver(db_session, live_tenant, shopify_order(cancelled_at="2026-10-10T10:00:00Z"), sender)
    assert cancelled["action"] == "SUPPRESSED"
    assert events(db_session) == [] and sender.calls == []
    assert db_session.scalar(select(Order)).current_status == "cancelled"
    statuses = [e.status for e in db_session.scalars(select(OrderStatusEvent).order_by(OrderStatusEvent.id))]
    assert statuses == ["pending", "cancelled"]


@pytest.mark.asyncio
async def test_status_event_only_when_status_changes(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(), sender)
    await deliver(db_session, live_tenant, shopify_order(note="x"), sender)
    assert len(db_session.scalars(select(OrderStatusEvent)).all()) == 1


@pytest.mark.asyncio
async def test_stale_event_time_writes_stale_row_and_never_calls_meta(db_session, live_tenant):
    sender = FakeSender()
    old = int(time.time()) - 8 * 24 * 3600
    res = await deliver(db_session, live_tenant, shopify_order(**DELIVERED), sender, event_time=old)
    assert res["action"] == "STALE"
    event = events(db_session)[0]
    assert event.status == "stale" and event.event_id == "delivered_987654321"
    assert sender.calls == []


@pytest.mark.asyncio
async def test_prepaid_paid_and_salla_flows_persist(db_session, live_tenant):
    sender = FakeSender()
    prepaid = shopify_order(id=1, financial_status="paid", payment_gateway_names=["shopify_payments"])
    assert (await deliver(db_session, live_tenant, prepaid, sender))["action"] == "SENT"
    salla = {"data": {"id": 77, "status": {"slug": "delivered"}, "payment_method": "cod",
                      "customer": {"email": EMAIL, "mobile": "0501234567"},
                      "amounts": {"total": {"amount": 120, "currency": "SAR"}}}}
    res = await deliver(db_session, live_tenant, salla, sender, topic="order.status.updated", platform="salla")
    assert res["action"] == "SENT"
    assert {e.event_id for e in events(db_session)} == {"delivered_1", "delivered_77"}


@pytest.mark.asyncio
async def test_unsupported_platform_is_recorded_not_processed(db_session, live_tenant):
    res = await deliver(db_session, live_tenant, {"id": 1}, FakeSender(), platform="zid", topic="orders/updated")
    assert res["action"] == "UNSUPPORTED"
    delivery = db_session.scalar(select(WebhookDelivery))
    assert delivery.processed_ok is False and delivery.error_type == "unsupported_platform"


@pytest.mark.asyncio
async def test_input_payload_not_mutated_and_only_hash_stored(db_session, live_tenant):
    payload = shopify_order(**DELIVERED)
    before = copy.deepcopy(payload)
    await deliver(db_session, live_tenant, payload, FakeSender())
    assert payload == before
    assert len(db_session.scalar(select(WebhookDelivery)).payload_sha256) == 64


# --- pre-hashed payload path --------------------------------------------------------------------------------

def test_payload_builder_accepts_prehashed_values_without_double_hashing():
    em, ph = hash_email(EMAIL), hash_phone(PHONE, "EGP")
    payload = MetaCAPISender().build_event_payload(
        "DeliveredPurchase", "delivered_1", "1", 10.0, "EGP", email_hash=em, phone_hash=ph)
    assert payload["data"][0]["user_data"] == {"em": [em], "ph": [ph]}
    with pytest.raises(ValueError):
        MetaCAPISender().build_event_payload("DeliveredPurchase", "d", "1", 1.0, "EGP", email_hash="a@b.com")


# --- credentials ----------------------------------------------------------------------------------------

def test_credential_round_trip_rotation_and_repr(db_session, shadow_tenant, fernet_key):
    cid = store_credential(db_session, shadow_tenant.id, "webhook_secret", "plain-secret-xyz")
    assert get_credential(db_session, shadow_tenant.id, "webhook_secret") == "plain-secret-xyz"
    row = db_session.get(Credential, cid)
    assert "plain-secret-xyz" not in row.ciphertext
    assert "plain-secret-xyz" not in repr(row) and "plain-secret-xyz" not in str(row)
    assert row.ciphertext not in repr(row)
    assert get_credential(db_session, shadow_tenant.id, "missing_kind") is None
    store_credential(db_session, shadow_tenant.id, "webhook_secret", "rotated-secret")
    db_session.refresh(row)
    assert row.rotated_at is not None
    assert get_credential(db_session, shadow_tenant.id, "webhook_secret") == "rotated-secret"
    assert len(db_session.scalars(select(Credential)).all()) == 1


def test_expired_credential_returns_none(db_session, shadow_tenant, fernet_key):
    store_credential(db_session, shadow_tenant.id, "meta_capi_token", "tok",
                     expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert get_credential(db_session, shadow_tenant.id, "meta_capi_token") is None


def test_missing_or_invalid_fernet_key_fails_closed(db_session, shadow_tenant, monkeypatch):
    monkeypatch.delenv(FERNET_KEY_ENV, raising=False)
    with pytest.raises(CredentialConfigError):
        store_credential(db_session, shadow_tenant.id, "meta_capi_token", "tok")
    with pytest.raises(CredentialConfigError):
        get_credential(db_session, shadow_tenant.id, "meta_capi_token")
    monkeypatch.setenv(FERNET_KEY_ENV, "not-a-fernet-key")
    with pytest.raises(CredentialConfigError) as exc:
        store_credential(db_session, shadow_tenant.id, "meta_capi_token", "tok-secret")
    assert "tok-secret" not in str(exc.value)


# --- tenants ---------------------------------------------------------------------------------------------

def test_tenant_helpers(db_session):
    tenant = create_tenant(db_session, name="X", platform="salla", shop_domain=" Shop.Example.COM ", country="sa",
                           currency="sar")
    assert tenant.mode == "shadow" and tenant.active and tenant.timezone == "Africa/Cairo"
    assert get_tenant_by_shop_domain(db_session, "shop.example.com").id == tenant.id
    assert get_tenant_by_shop_domain(db_session, "nope.example.com") is None
    with pytest.raises(ValueError):
        create_tenant(db_session, name="Bad", platform="woo", shop_domain="a.com")
