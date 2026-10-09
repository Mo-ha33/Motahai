"""
test_s2_engine.py — Sprint S2 wave 1 engine: S2-1 (event_time = order placed, settlement window, late_delivery),
S2-3 (checkout_context D-006 + external_id/address match keys + event_source_url), S2-4 (partial refunds = net value).

These tests use DEFAULT-settlement tenants (12h) unless a test says otherwise; tests/conftest.py's shared tenants use
settlement_hours=0 so the older S1 tests keep their "send at once" semantics.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, select, text

from src.ameen_workforce.capi_service import (
    DELIVERED_EVENT, DELIVERED_EVENT_NAME, EVENT_TYPES, LATE_DELIVERY_CUTOFF, MetaCAPISender, hash_match_value,
    normalize_match_value
)
from src.ameen_workforce.checkout_context import purge_expired_checkout_context
from src.ameen_workforce.credentials import FERNET_KEY_ENV, store_credential
from src.ameen_workforce.db import CapiEvent, CheckoutContext, Order, create_tenant
from src.ameen_workforce.order_pipeline import (
    DEFAULT_SETTLEMENT_HOURS, DUE_SAFETY_MARGIN, process_webhook, retry_failed_events, send_due_events
)
from src.ameen_workforce.webhook_listener import OrderWebhookProcessor

IP = "203.0.113.77"
UA = "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36"
EMAIL = "buyer@example.com"
PHONE = "01012345678"
CUSTOMER_ID = 7700123
NOW = datetime.now(timezone.utc)

ADDRESS = {"first_name": "Ahmed", "last_name": "El-Sayed", "city": "Nasr City", "province": "Giza Governorate",
           "province_code": "C", "zip": "11835", "country_code": "EG"}


class FakeSender(MetaCAPISender):
    """Real D-005 logic, fake network."""
    def __init__(self, results=None):
        super().__init__()
        self.calls = []
        self.results = list(results or [])

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append(payload)
        if self.results:
            return self.results.pop(0)
        return {"status": "success", "fbtrace_id": "TRACE1"}

    def event(self, index=0):
        return self.calls[index]["data"][0]


FAIL = {"status": "error", "http_code": 500, "error_message": "boom"}


def iso(dt):
    return dt.isoformat()


def order_payload(placed=None, **overrides):
    """A delivered COD Shopify order placed `placed` (default 2h ago)."""
    placed = placed or NOW - timedelta(hours=2)
    order = {
        "id": 555001, "financial_status": "paid", "fulfillment_status": "fulfilled",
        "payment_gateway_names": ["Cash on Delivery (COD)"], "total_price": "850.00", "currency": "EGP",
        "created_at": iso(placed),
        "customer": {"id": CUSTOMER_ID, "email": EMAIL, "phone": PHONE},
        "shipping_address": dict(ADDRESS),
        "client_details": {"browser_ip": IP, "user_agent": UA},
    }
    order.update(overrides)
    return order


async def deliver(session, tenant, payload, sender, topic="orders/updated", platform="shopify", **kw):
    return await process_webhook(session, tenant, platform, topic, payload, sender=sender, **kw)


def capi_rows(session):
    return session.scalars(select(CapiEvent).order_by(CapiEvent.id)).all()


@pytest.fixture
def live(db_session, fernet_key):
    """Live tenant with DEFAULT settlement (12h)."""
    tenant = create_tenant(db_session, name="Live12", platform="shopify", shop_domain="live12.myshopify.com",
                           meta_dataset_id="901", mode="live")
    store_credential(db_session, tenant.id, "meta_capi_token", "TOKEN-S2")
    return tenant


@pytest.fixture
def shadow(db_session, fernet_key):
    return create_tenant(db_session, name="Shadow12", platform="shopify", shop_domain="shadow12.myshopify.com",
                         meta_dataset_id="902")


# --- S2-1: event_time, settlement window, late delivery ---------------------------------------------------------

@pytest.mark.asyncio
async def test_event_time_is_order_placed_time_not_send_time(db_session, live):
    placed = NOW - timedelta(hours=30)
    sender = FakeSender()
    res = await deliver(db_session, live, order_payload(placed=placed), sender)
    assert res["action"] == "SCHEDULED"
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert sender.event()["event_time"] == int(placed.timestamp())
    assert sender.event()["event_time"] < int((NOW - timedelta(hours=29)).timestamp())  # not "now"
    assert capi_rows(db_session)[0].event_time == int(placed.timestamp())


@pytest.mark.asyncio
async def test_event_time_falls_back_to_first_seen_when_platform_time_missing(db_session, live):
    payload = order_payload()
    payload.pop("created_at")
    sender = FakeSender()
    await deliver(db_session, live, payload, sender)
    order = db_session.scalar(select(Order))
    assert order.created_at_platform is None
    row = capi_rows(db_session)[0]
    assert row.event_time == int(order.created_at.timestamp())


@pytest.mark.asyncio
async def test_eligible_order_is_scheduled_then_sent_once_when_due(db_session, live):
    sender = FakeSender()
    res = await deliver(db_session, live, order_payload(), sender)
    assert res["action"] == "SCHEDULED" and res["capi_status"] == "scheduled"
    assert sender.calls == []
    order, row = db_session.scalar(select(Order)), capi_rows(db_session)[0]
    assert row.status == "scheduled" and order.delivered_at is not None
    assert row.due_at == order.delivered_at + timedelta(hours=DEFAULT_SETTLEMENT_HOURS)

    before = await send_due_events(db_session, now=row.due_at - timedelta(minutes=1), sender=sender)
    assert before["due"] == 0 and sender.calls == []

    after = await send_due_events(db_session, now=row.due_at + timedelta(minutes=1), sender=sender)
    assert after["due"] == 1 and after["sent"] == 1 and len(sender.calls) == 1
    again = await send_due_events(db_session, now=row.due_at + timedelta(hours=1), sender=sender)
    assert again["due"] == 0 and len(sender.calls) == 1
    db_session.refresh(row)
    assert row.status == "sent" and row.attempts == 1 and row.fbtrace_id == "TRACE1"


@pytest.mark.asyncio
async def test_repeated_webhook_while_scheduled_is_already_emitted(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    again = await deliver(db_session, live, order_payload(note="edited"), sender)
    assert again["action"] == "ALREADY_EMITTED" and len(capi_rows(db_session)) == 1


@pytest.mark.asyncio
async def test_order_cancelled_during_settlement_is_stale_and_meta_not_called(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    cancelled = await deliver(db_session, live, order_payload(cancelled_at=iso(NOW)), sender)
    assert cancelled["action"] == "SUPPRESSED"
    counts = await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert counts["stale"] == 1 and counts["sent"] == 0 and sender.calls == []
    row = capi_rows(db_session)[0]
    assert row.status == "stale" and row.error_type == "no_longer_eligible"


@pytest.mark.asyncio
async def test_partial_refund_during_settlement_sends_net_value(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    refund = order_payload(financial_status="partially_refunded", current_total_price="600.00")
    assert (await deliver(db_session, live, refund, sender))["action"] == "ALREADY_EMITTED"
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert sender.event()["custom_data"]["value"] == 600.0


@pytest.mark.asyncio
async def test_full_refund_during_settlement_is_stale(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    await deliver(db_session, live, order_payload(financial_status="refunded", current_total_price="0.00"), sender)
    counts = await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert counts["stale"] == 1 and sender.calls == []


@pytest.mark.asyncio
async def test_order_placed_seven_days_ago_is_late_delivery_and_never_sent(db_session, live):
    sender = FakeSender()
    res = await deliver(db_session, live, order_payload(placed=NOW - timedelta(days=7)), sender)
    assert res["action"] == "LATE_DELIVERY" and res["capi_status"] == "late_delivery"
    row = capi_rows(db_session)[0]
    assert row.status == "late_delivery" and row.error_type == "past_cutoff" and row.due_at is None
    assert sender.calls == []
    # terminal: it holds the idempotency key and later runs/retries do nothing
    assert (await deliver(db_session, live, order_payload(placed=NOW - timedelta(days=7), note="x"),
                          sender))["action"] == "ALREADY_EMITTED"
    assert (await send_due_events(db_session, now=NOW + timedelta(days=1), sender=sender))["due"] == 0
    assert (await retry_failed_events(db_session, sender=sender))["retried"] == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_due_at_never_exceeds_placed_plus_cutoff_minus_safety(db_session, live):
    placed = NOW - timedelta(days=6.2)  # latest safe send = placed + 6.5d - 1h, about 6h from now
    sender = FakeSender()
    res = await deliver(db_session, live, order_payload(placed=placed), sender)
    assert res["action"] == "SCHEDULED"
    row = capi_rows(db_session)[0]
    latest = placed + LATE_DELIVERY_CUTOFF - DUE_SAFETY_MARGIN
    assert row.due_at == latest and row.due_at < NOW + timedelta(hours=DEFAULT_SETTLEMENT_HOURS)
    # sent at its due time (before the cutoff) ...
    assert (await send_due_events(db_session, now=row.due_at, sender=sender))["sent"] == 1


@pytest.mark.asyncio
async def test_scheduled_row_that_is_overdue_past_cutoff_becomes_late_delivery(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(placed=NOW - timedelta(days=6.2)), sender)
    counts = await send_due_events(db_session, now=NOW + timedelta(days=0.4), sender=sender)  # scheduler was down
    assert counts["late_delivery"] == 1 and sender.calls == []
    assert capi_rows(db_session)[0].status == "late_delivery"


@pytest.mark.asyncio
async def test_already_due_order_is_dispatched_in_line(db_session, live):
    placed = NOW - timedelta(days=6.47)  # latest safe send was a few minutes ago
    sender = FakeSender()
    res = await deliver(db_session, live, order_payload(placed=placed), sender)
    assert res["action"] == "SENT" and len(sender.calls) == 1


@pytest.mark.asyncio
async def test_tenant_settlement_hours_override_is_respected(db_session, fernet_key):
    fast = create_tenant(db_session, name="Fast", platform="shopify", shop_domain="fast.myshopify.com",
                         meta_dataset_id="903", settlement_hours=2)
    sender = FakeSender()
    res = await deliver(db_session, fast, order_payload(), sender)
    assert res["action"] == "SCHEDULED"
    order, row = db_session.scalar(select(Order)), capi_rows(db_session)[0]
    assert row.due_at == order.delivered_at + timedelta(hours=2)
    counts = await send_due_events(db_session, now=order.delivered_at + timedelta(hours=2, minutes=1), sender=sender)
    assert counts["shadow"] == 1  # shadow tenant: same timing, no Meta call
    assert sender.calls == []


@pytest.mark.asyncio
async def test_shadow_tenant_scheduled_then_recorded_shadow_without_meta(db_session, shadow):
    sender = FakeSender()
    assert (await deliver(db_session, shadow, order_payload(), sender))["action"] == "SCHEDULED"
    counts = await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert counts["shadow"] == 1 and sender.calls == []
    assert capi_rows(db_session)[0].status == "shadow"


@pytest.mark.asyncio
async def test_inactive_tenant_rows_stay_scheduled(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    live.active = False
    db_session.commit()
    counts = await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert counts["due"] == 1 and counts["skipped"] == 1 and sender.calls == []
    assert capi_rows(db_session)[0].status == "scheduled"


@pytest.mark.asyncio
async def test_failed_send_retry_uses_placed_time_and_respects_cutoff(db_session, live):
    placed = NOW - timedelta(days=3)
    sender = FakeSender([FAIL])
    await deliver(db_session, live, order_payload(placed=placed), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert capi_rows(db_session)[0].status == "failed"
    counts = await retry_failed_events(db_session, sender=sender, now=NOW + timedelta(hours=14))
    assert counts["retried"] == 1 and counts["sent"] == 1
    assert sender.event(1)["event_time"] == int(placed.timestamp())

    # a failed row that is only retried after the cutoff ends as late_delivery, Meta not called again
    late = FakeSender([FAIL])
    await deliver(db_session, live, order_payload(id=555002, placed=NOW - timedelta(days=6)), late)
    await send_due_events(db_session, now=NOW + timedelta(hours=11, minutes=5), sender=late)
    assert capi_rows(db_session)[1].status == "failed" and len(late.calls) == 1
    counts = await retry_failed_events(db_session, sender=late, now=NOW + timedelta(days=1))
    assert counts["late_delivery"] == 1 and counts["retried"] == 0 and len(late.calls) == 1
    assert capi_rows(db_session)[1].status == "late_delivery"


def test_event_types_are_configuration_not_scattered_literals():
    assert EVENT_TYPES[DELIVERED_EVENT_NAME] is DELIVERED_EVENT
    assert DELIVERED_EVENT.event_id("42") == "delivered_42"
    assert DELIVERED_EVENT.converting_statuses == frozenset({"delivered", "paid"})


# --- S2-4: partial refunds -> net value ------------------------------------------------------------------------

def test_partially_refunded_delivered_cod_is_eligible_with_net_value():
    parsed = OrderWebhookProcessor.parse_shopify_order(
        order_payload(financial_status="partially_refunded", current_total_price="600.00"))
    assert parsed["status"] == "delivered" and parsed["total_price"] == 600.0
    decision = MetaCAPISender().process_cod_order_event(
        parsed["order_id"], parsed["status"], parsed["total_price"], parsed["currency"], parsed["is_cod"])
    assert decision["action"] == "READY_TO_EMIT"
    assert decision["payload"]["data"][0]["custom_data"]["value"] == 600.0


def test_partially_refunded_prepaid_is_paid_and_eligible():
    parsed = OrderWebhookProcessor.parse_shopify_order(order_payload(
        financial_status="partially_refunded", payment_gateway_names=["shopify_payments"], fulfillment_status=None,
        current_total_price="700.00"))
    assert parsed["status"] == "paid" and parsed["total_price"] == 700.0


def test_net_value_is_total_minus_successful_refund_transactions_current_total_price_only_as_fallback():
    refunds = [
        {"transactions": [{"kind": "REFUND", "status": "SUCCESS", "amount": "100.00"},  # GraphQL casing
                          {"kind": "refund", "status": "failure", "amount": "500.00"},
                          {"kind": "sale", "status": "success", "amount": "999.00"}]},
        {"transactions": [{"kind": "refund", "status": "success", "amount": "50.50"}]},
    ]
    # refunds[] present: the refund math wins even if current_total_price disagrees (its refund handling is undocumented)
    with_both = OrderWebhookProcessor.parse_shopify_order(
        order_payload(financial_status="partially_refunded", current_total_price="700.00", refunds=refunds))
    assert with_both["total_price"] == pytest.approx(699.5)
    # no refunds array at all: current_total_price is the fallback, then total_price
    fallback = OrderWebhookProcessor.parse_shopify_order(
        order_payload(financial_status="partially_refunded", current_total_price="600.00"))
    assert fallback["total_price"] == 600.0
    # an empty refunds array is still an array: total_price
    assert OrderWebhookProcessor.parse_shopify_order(
        order_payload(refunds=[], current_total_price="1.00"))["total_price"] == 850.0
    untouched = OrderWebhookProcessor.parse_shopify_order(order_payload())
    assert untouched["total_price"] == 850.0


def test_full_refund_void_and_zero_net_stay_suppressed():
    sender = MetaCAPISender()
    for overrides in ({"financial_status": "refunded"}, {"financial_status": "voided"},
                      {"cancelled_at": iso(NOW)}):
        parsed = OrderWebhookProcessor.parse_shopify_order(order_payload(**overrides))
        decision = sender.process_cod_order_event(parsed["order_id"], parsed["status"], parsed["total_price"],
                                                  parsed["currency"], parsed["is_cod"])
        assert decision["action"] == "SUPPRESSED"
    zero = OrderWebhookProcessor.parse_shopify_order(
        order_payload(financial_status="partially_refunded", current_total_price="0.00"))
    assert zero["status"] == "delivered" and zero["total_price"] == 0.0
    assert sender.process_cod_order_event("1", "delivered", 0.0, "EGP")["action"] == "SUPPRESSED"
    refunded_all = OrderWebhookProcessor.parse_shopify_order(order_payload(
        financial_status="partially_refunded",
        refunds=[{"transactions": [{"kind": "refund", "status": "success", "amount": "900.00"}]}]))
    assert refunded_all["total_price"] == 0.0


@pytest.mark.asyncio
async def test_partially_refunded_webhook_schedules_net_value_and_zero_net_is_suppressed(db_session, live):
    sender = FakeSender()
    res = await deliver(db_session, live, order_payload(financial_status="partially_refunded",
                                                        current_total_price="600.00"), sender)
    assert res["action"] == "SCHEDULED"
    zero = await deliver(db_session, live, order_payload(id=555009, financial_status="partially_refunded",
                                                         current_total_price="0.00"), sender)
    assert zero["action"] == "SUPPRESSED"
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert [c["data"][0]["custom_data"]["value"] for c in sender.calls] == [600.0]


# --- S2-3: checkout context (D-006) -----------------------------------------------------------------------------

def all_tables_text(session):
    blob = []
    for table in inspect(session.get_bind()).get_table_names():
        for row in session.execute(text(f'SELECT * FROM "{table}"')).all():  # noqa: S608
            blob.append(" ".join(str(v) for v in row))
    return "\n".join(blob).lower()


@pytest.mark.asyncio
async def test_checkout_context_is_stored_encrypted_only(db_session, live):
    await deliver(db_session, live, order_payload(), FakeSender())
    ctx = db_session.scalar(select(CheckoutContext))
    assert ctx.ip_ciphertext and ctx.ua_ciphertext
    assert IP not in ctx.ip_ciphertext and "Mozilla" not in ctx.ua_ciphertext
    assert ctx.expires_at - ctx.captured_at == timedelta(days=14)
    blob = all_tables_text(db_session)
    assert IP not in blob and "mozilla" not in blob and "chrome/126" not in blob
    assert IP not in repr(ctx) and ctx.ip_ciphertext not in repr(ctx)


@pytest.mark.asyncio
async def test_delayed_send_includes_decrypted_ip_and_ua_and_live_success_purges_it(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    assert db_session.scalar(select(CheckoutContext)) is not None
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    ud = sender.event()["user_data"]
    assert ud["client_ip_address"] == IP and ud["client_user_agent"] == UA
    assert db_session.scalar(select(CheckoutContext)) is None  # purged after a successful live send


@pytest.mark.asyncio
async def test_failed_send_keeps_context_and_retry_success_purges(db_session, live):
    sender = FakeSender([FAIL])
    await deliver(db_session, live, order_payload(), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert capi_rows(db_session)[0].status == "failed"
    assert db_session.scalar(select(CheckoutContext)) is not None
    await retry_failed_events(db_session, sender=sender, now=NOW + timedelta(hours=14))
    assert sender.event(1)["user_data"]["client_ip_address"] == IP
    assert db_session.scalar(select(CheckoutContext)) is None


@pytest.mark.asyncio
async def test_shadow_send_keeps_context(db_session, shadow):
    sender = FakeSender()
    await deliver(db_session, shadow, order_payload(), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert capi_rows(db_session)[0].status == "shadow"
    assert db_session.scalar(select(CheckoutContext)) is not None  # shadow never dispatched


@pytest.mark.asyncio
async def test_expired_context_is_purged_and_never_used(db_session, shadow):
    await deliver(db_session, shadow, order_payload(), FakeSender())
    ctx = db_session.scalar(select(CheckoutContext))
    assert purge_expired_checkout_context(db_session, now=ctx.expires_at - timedelta(seconds=1)) == 0
    assert db_session.scalar(select(CheckoutContext)) is not None
    assert purge_expired_checkout_context(db_session, now=ctx.expires_at + timedelta(seconds=1)) == 1
    assert db_session.scalar(select(CheckoutContext)) is None


@pytest.mark.asyncio
async def test_expired_context_is_ignored_at_send_time(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    ctx = db_session.scalar(select(CheckoutContext))
    ctx.expires_at = NOW + timedelta(hours=1)
    db_session.commit()
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert "client_ip_address" not in sender.event()["user_data"]


@pytest.mark.asyncio
async def test_first_capture_wins(db_session, shadow):
    sender = FakeSender()
    await deliver(db_session, shadow, order_payload(financial_status="pending", fulfillment_status=None), sender)
    await deliver(db_session, shadow, order_payload(
        client_details={"browser_ip": "198.51.100.9", "user_agent": "Other/1.0"}), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert len(db_session.scalars(select(CheckoutContext)).all()) == 1
    from src.ameen_workforce.checkout_context import load_checkout_context
    order = db_session.scalar(select(Order))
    assert load_checkout_context(db_session, order.id) == (IP, UA)


@pytest.mark.asyncio
async def test_missing_fernet_key_does_not_break_order_processing(db_session, monkeypatch):
    monkeypatch.delenv(FERNET_KEY_ENV, raising=False)
    tenant = create_tenant(db_session, name="NoKey", platform="shopify", shop_domain="nokey.myshopify.com",
                           meta_dataset_id="904")
    sender = FakeSender()
    res = await deliver(db_session, tenant, order_payload(), sender)
    assert res["action"] == "SCHEDULED"
    assert db_session.scalars(select(CheckoutContext)).all() == []
    counts = await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert counts["shadow"] == 1  # still processed; just no IP/UA


@pytest.mark.asyncio
async def test_no_client_details_means_no_ip_or_ua_is_ever_sent(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(client_details=None), sender)
    assert db_session.scalars(select(CheckoutContext)).all() == []
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    ud = sender.event()["user_data"]
    assert "client_ip_address" not in ud and "client_user_agent" not in ud


@pytest.mark.asyncio
async def test_context_not_recaptured_after_the_event_is_sent(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    await deliver(db_session, live, order_payload(note="later edit"), sender)
    assert db_session.scalars(select(CheckoutContext)).all() == []


# --- S2-3: external_id + address hashes ---------------------------------------------------------------------------

def test_match_key_normalization():
    assert normalize_match_value("fn", "  Ahmed   Ali ") == "ahmed ali"
    assert normalize_match_value("ln", "El-Sayed") == "el-sayed"
    assert normalize_match_value("ct", " New  York! ") == "newyork"
    assert normalize_match_value("ct", "مدينة نصر") == "مدينةنصر"
    assert normalize_match_value("zp", "SW1A 1AA") == "sw1a1aa"
    assert normalize_match_value("zp", "12345-6789") == "123456789"
    assert normalize_match_value("st", "  Giza  Governorate") == "giza governorate"
    assert normalize_match_value("country", "EG") == "eg" and normalize_match_value("country", "Egypt") is None
    assert normalize_match_value("fn", "   ") is None and normalize_match_value("fn", None) is None
    assert normalize_match_value("external_id", " 7700123 ") == "7700123"
    assert hash_match_value("ct", "Nasr City") == hash_match_value("ct", "nasrcity") and len(hash_match_value("fn", "a")) == 64
    # Arabic text: trim/collapse only
    assert normalize_match_value("fn", "  محمد   علي ") == "محمد علي"


@pytest.mark.asyncio
async def test_external_id_and_address_hashes_in_payload_and_no_raw_values_in_db(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    order = db_session.scalar(select(Order))
    expected = {
        "external_id": hash_match_value("external_id", str(CUSTOMER_ID)),
        "fn": hash_match_value("fn", "ahmed"), "ln": hash_match_value("ln", "el-sayed"),
        "ct": hash_match_value("ct", "nasrcity"), "st": hash_match_value("st", "giza governorate"),
        "zp": hash_match_value("zp", "11835"), "country": hash_match_value("country", "eg"),
    }
    for key, value in expected.items():
        assert getattr(order, f"{key}_hash") == value, key
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    ud = sender.event()["user_data"]
    for key, value in expected.items():
        assert ud[key] == [value], key
    assert ud["em"] and ud["ph"]
    blob = all_tables_text(db_session)
    for raw in ("ahmed", "sayed", "nasr", "giza", "11835", str(CUSTOMER_ID)):
        assert raw not in blob, raw
    assert "ahmed" not in repr(sender.calls).lower() and "nasr" not in repr(sender.calls).lower()


@pytest.mark.asyncio
async def test_billing_address_is_the_fallback_and_hashes_are_never_overwritten_with_empty(db_session, shadow):
    sender = FakeSender()
    payload = order_payload(financial_status="pending", fulfillment_status=None)
    payload.pop("shipping_address")
    payload["billing_address"] = {**ADDRESS, "first_name": "Sara"}
    await deliver(db_session, shadow, payload, sender)
    order = db_session.scalar(select(Order))
    assert order.fn_hash == hash_match_value("fn", "sara") and order.country_hash == hash_match_value("country", "eg")
    # a later (redacted) update with no address and no customer id keeps what is stored
    redacted = order_payload(financial_status="pending", fulfillment_status=None, customer={}, note="r")
    redacted.pop("shipping_address")
    await deliver(db_session, shadow, redacted, sender)
    db_session.refresh(order)
    assert order.fn_hash == hash_match_value("fn", "sara")
    assert order.external_id_hash == hash_match_value("external_id", str(CUSTOMER_ID))


@pytest.mark.asyncio
async def test_salla_customer_id_becomes_external_id(db_session, fernet_key):
    tenant = create_tenant(db_session, name="Salla", platform="salla", shop_domain="1234567", meta_dataset_id="905",
                           country="SA", currency="SAR")
    sender = FakeSender()
    salla = {"data": {"id": 77, "status": {"slug": "delivered"}, "payment_method": "cod",
                      "customer": {"id": 99887, "email": EMAIL, "mobile": "0501234567", "first_name": "Sara"},
                      "amounts": {"total": {"amount": 120, "currency": "SAR"}}}}
    res = await deliver(db_session, tenant, salla, sender, topic="order.status.updated", platform="salla")
    assert res["action"] == "SCHEDULED"
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert db_session.scalar(select(Order)).external_id_hash == hash_match_value("external_id", "99887")
    assert capi_rows(db_session)[0].status == "shadow"


# --- S2-3: event_source_url -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_source_url_shopify_default_and_storefront_override(db_session, fernet_key):
    default = create_tenant(db_session, name="D", platform="shopify", shop_domain="dflt.myshopify.com",
                            meta_dataset_id="906", mode="live")
    store_credential(db_session, default.id, "meta_capi_token", "T")
    custom = create_tenant(db_session, name="C", platform="shopify", shop_domain="cust.myshopify.com",
                           meta_dataset_id="907", mode="live", storefront_url="https://shop.example.com/")
    store_credential(db_session, custom.id, "meta_capi_token", "T")
    sender = FakeSender()
    await deliver(db_session, default, order_payload(id=1), sender)
    await deliver(db_session, custom, order_payload(id=2), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    urls = {c["data"][0]["custom_data"]["order_id"]: c["data"][0].get("event_source_url") for c in sender.calls}
    assert urls == {"1": "https://dflt.myshopify.com/", "2": "https://shop.example.com/"}


@pytest.mark.asyncio
async def test_event_source_url_salla_only_with_storefront_url(db_session, fernet_key):
    plain = create_tenant(db_session, name="S1", platform="salla", shop_domain="111", meta_dataset_id="908",
                          mode="live")
    store_credential(db_session, plain.id, "meta_capi_token", "T")
    with_url = create_tenant(db_session, name="S2", platform="salla", shop_domain="222", meta_dataset_id="909",
                             mode="live", storefront_url="https://salla.example.sa")
    store_credential(db_session, with_url.id, "meta_capi_token", "T")
    sender = FakeSender()
    for tenant, order_id in ((plain, 1), (with_url, 2)):
        salla = {"data": {"id": order_id, "status": {"slug": "delivered"}, "payment_method": "cod",
                          "amounts": {"total": {"amount": 120, "currency": "SAR"}}}}
        await deliver(db_session, tenant, salla, sender, topic="order.status.updated", platform="salla")
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    urls = {c["data"][0]["custom_data"]["order_id"]: c["data"][0].get("event_source_url") for c in sender.calls}
    assert urls == {"1": None, "2": "https://salla.example.sa"}


# --- quality flags (required-for-website fields) ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quality_flags_none_when_user_agent_and_source_url_present(db_session, live):
    sender = FakeSender()
    await deliver(db_session, live, order_payload(), sender)
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert capi_rows(db_session)[0].quality_flags is None
    assert sender.event()["user_data"]["client_user_agent"] == UA


@pytest.mark.asyncio
async def test_quality_flags_record_missing_user_agent_and_source_url_without_fabricating(db_session, fernet_key):
    tenant = create_tenant(db_session, name="S", platform="salla", shop_domain="333", meta_dataset_id="910",
                           mode="live")
    store_credential(db_session, tenant.id, "meta_capi_token", "T")
    sender = FakeSender()
    salla = {"data": {"id": 5, "status": {"slug": "delivered"}, "payment_method": "cod",
                      "amounts": {"total": {"amount": 120, "currency": "SAR"}}}}
    await deliver(db_session, tenant, salla, sender, topic="order.status.updated", platform="salla")
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    assert len(sender.calls) == 1  # still sent
    assert capi_rows(db_session)[0].quality_flags == "missing_user_agent,missing_event_source_url"
    assert "client_user_agent" not in sender.event()["user_data"] and "event_source_url" not in sender.event()


@pytest.mark.asyncio
async def test_quality_flag_missing_user_agent_when_context_expired_and_on_shadow(db_session, shadow):
    sender = FakeSender()
    await deliver(db_session, shadow, order_payload(), sender)
    ctx = db_session.scalar(select(CheckoutContext))
    ctx.expires_at = NOW + timedelta(hours=1)
    db_session.commit()
    await send_due_events(db_session, now=NOW + timedelta(hours=13), sender=sender)
    row = capi_rows(db_session)[0]
    assert row.status == "shadow" and row.quality_flags == "missing_user_agent"


def test_tenant_validation_for_new_columns(db_session):
    with pytest.raises(ValueError):
        create_tenant(db_session, name="X", platform="shopify", shop_domain="x.myshopify.com", settlement_hours=-1)
    with pytest.raises(ValueError):
        create_tenant(db_session, name="X", platform="shopify", shop_domain="x.myshopify.com",
                      storefront_url="javascript:alert(1)")
