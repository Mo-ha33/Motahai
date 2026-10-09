"""
test_attribution.py — S1-3 checkout attribution capture: note_attributes parsing/validation, persistence, CAPI
match-quality fields (fbp/fbc/IP/UA), privacy (IP/UA never stored) and the storefront JS test.
"""

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from src.ameen_workforce.capi_service import MetaCAPISender
from src.ameen_workforce.db import Order
from src.ameen_workforce.order_pipeline import process_webhook
from src.ameen_workforce.webhook_listener import OrderWebhookProcessor, order_webhook_processor

FBP = "fb.1.1700000000000.1234567890"
FBC = "fb.1.1700000000000.AbC_dEf-123"
IP = "203.0.113.77"
UA = "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36"
ATTRS = {
    "utm_source": "meta", "utm_medium": "paid", "utm_campaign": "120001", "utm_content": "120002",
    "ad_id": "120002", "fbp": FBP, "fbc": FBC, "ttclid": "TT_click-1", "sccid": "SC.click~1",
}
ROOT = Path(__file__).resolve().parent.parent
# Placed shortly before the test runs: a fixed date would eventually fall past the placed+6.5d send cutoff.
PLACED_AT = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def notes(**fields):
    """note_attributes list as Shopify delivers it: [{name, value}] with the _mt_ prefix, plus unrelated notes."""
    out = [{"name": "gift_wrap", "value": "no"}]
    out += [{"name": f"_mt_{k}", "value": v} for k, v in fields.items()]
    return out


def shopify_order(**overrides):
    order = {
        "id": 555001, "financial_status": "paid", "fulfillment_status": "fulfilled",
        "payment_gateway_names": ["Cash on Delivery (COD)"], "total_price": "850.00", "currency": "EGP",
        "created_at": PLACED_AT,
        "customer": {"email": "buyer@example.com", "phone": "01012345678"},
        "note_attributes": notes(**ATTRS),
        "client_details": {"browser_ip": IP, "user_agent": UA, "accept_language": "ar"},
    }
    order.update(overrides)
    return order


class FakeSender(MetaCAPISender):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append(payload)
        return {"status": "success", "fbtrace_id": "T1"}


async def deliver(session, tenant, payload, sender, topic="orders/updated", **kw):
    return await process_webhook(session, tenant, "shopify", topic, payload, sender=sender, **kw)


def user_data(sender, index=0):
    return sender.calls[index]["data"][0]["user_data"]


# --- parsing ------------------------------------------------------------------------------------------------

def test_parse_shopify_reads_note_attributes_and_client_details():
    parsed = OrderWebhookProcessor.parse_shopify_order(shopify_order())
    assert parsed["attribution"] == ATTRS
    assert parsed["client_ip"] == IP
    assert parsed["user_agent"] == UA


def test_parse_shopify_without_attribution_has_all_keys_none():
    parsed = OrderWebhookProcessor.parse_shopify_order(shopify_order(note_attributes=[], client_details=None))
    assert set(parsed["attribution"]) == set(ATTRS) and not any(parsed["attribution"].values())
    assert parsed["client_ip"] is None and parsed["user_agent"] is None


def test_parse_salla_and_fulfillment_have_no_attribution_yet():
    salla = OrderWebhookProcessor.parse_salla_order({"data": {"id": 1, "status": {"slug": "delivered"}}})
    assert salla["attribution"] is None and salla["client_ip"] is None and salla["user_agent"] is None
    ful = OrderWebhookProcessor.parse_shopify_fulfillment({"id": 1, "order_id": 2, "shipment_status": "delivered"})
    assert ful["attribution"] is None and ful["client_ip"] is None and ful["user_agent"] is None


def test_junk_values_are_dropped():
    junk = notes(
        utm_source="x" * 256,                 # overlong
        utm_medium="paid\x00\x1f",            # control chars removed, rest kept
        utm_campaign=12345,                   # not a string
        utm_content="<script>1</script>",     # free text is kept as an inert string (not HTML-escaped at rest)
        ad_id="12ab34",                       # non-digit
        fbp="not-a-cookie",
        fbc="fb.1.123.abc",                   # timestamp too short
        ttclid="bad value with spaces",
        sccid="y" * 300,
    )
    parsed = OrderWebhookProcessor.parse_shopify_order(shopify_order(
        note_attributes=junk, client_details={"browser_ip": "999.1.1.1", "user_agent": "\x00\x01"}))
    attr = parsed["attribution"]
    assert attr["utm_source"] is None
    assert attr["utm_medium"] == "paid"
    assert attr["utm_campaign"] is None and attr["utm_content"] == "<script>1</script>"
    assert attr["ad_id"] is None and attr["fbp"] is None and attr["fbc"] is None
    assert attr["ttclid"] is None and attr["sccid"] is None
    assert parsed["client_ip"] is None and parsed["user_agent"] is None


def test_overlong_user_agent_is_truncated_and_ipv6_accepted():
    parsed = OrderWebhookProcessor.parse_shopify_order(shopify_order(
        client_details={"browser_ip": "2001:db8::1", "user_agent": "U" * 2000}))
    assert parsed["client_ip"] == "2001:db8::1"
    assert len(parsed["user_agent"]) == 512


def test_malformed_note_attributes_do_not_raise():
    for bad in (None, "x", {"name": "_mt_ad_id"}, [None, "x", {"name": 5, "value": "1"}, {"value": "1"}]):
        parsed = OrderWebhookProcessor.parse_shopify_order(shopify_order(note_attributes=bad))
        assert not any(parsed["attribution"].values())


# --- persistence + CAPI payload -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attribution_persisted_and_capi_payload_has_match_fields(db_session, live_tenant):
    sender = FakeSender()
    res = await deliver(db_session, live_tenant, shopify_order(), sender)
    assert res["action"] == "SENT"
    order = db_session.scalar(select(Order))
    for column, value in ATTRS.items():
        assert getattr(order, column) == value, column
    ud = user_data(sender)
    assert ud["fbp"] == FBP and ud["fbc"] == FBC
    assert ud["client_ip_address"] == IP and ud["client_user_agent"] == UA
    # D-005 payload stays minimal: attribution is not added to custom_data
    assert sender.calls[0]["data"][0]["custom_data"] == {"currency": "EGP", "value": 850.0, "order_id": "555001"}


@pytest.mark.asyncio
async def test_later_webhook_without_attributes_does_not_erase_stored_attribution(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(financial_status="pending", fulfillment_status=None), sender)
    await deliver(db_session, live_tenant, shopify_order(
        note_attributes=[], client_details=None, financial_status="pending", fulfillment_status="fulfilled",
        total_price="850.01"), sender)
    order = db_session.scalar(select(Order))
    for column, value in ATTRS.items():
        assert getattr(order, column) == value, column
    # the delivering webhook carries no attributes either: the send still uses the stored fbp/fbc, and the checkout
    # IP/UA captured by the FIRST webhook (kept encrypted, D-006)
    res = await deliver(db_session, live_tenant, shopify_order(note_attributes=[], client_details=None,
                                                               total_price="850.02"), sender)
    assert res["action"] == "SENT"
    ud = user_data(sender)
    assert ud["fbp"] == FBP and ud["fbc"] == FBC
    assert ud["client_ip_address"] == IP and ud["client_user_agent"] == UA


@pytest.mark.asyncio
async def test_first_non_empty_value_per_field_wins(db_session, live_tenant):
    sender = FakeSender()
    pending = dict(financial_status="pending", fulfillment_status=None)
    await deliver(db_session, live_tenant, shopify_order(
        note_attributes=notes(utm_source="meta", ad_id="111"), **pending), sender)
    await deliver(db_session, live_tenant, shopify_order(
        note_attributes=notes(utm_source="tiktok", ad_id="222", fbp=FBP), total_price="851.00", **pending), sender)
    order = db_session.scalar(select(Order))
    assert (order.utm_source, order.ad_id) == ("meta", "111")  # not overwritten
    assert order.fbp == FBP                                    # empty field filled by the later webhook


@pytest.mark.asyncio
async def test_junk_attribution_is_not_persisted(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(
        note_attributes=notes(ad_id="12ab", fbp="junk", fbc="fb.1.1.x", utm_source="s" * 300)), sender)
    order = db_session.scalar(select(Order))
    assert order.ad_id is None and order.fbp is None and order.fbc is None and order.utm_source is None
    ud = user_data(sender)
    assert "fbp" not in ud and "fbc" not in ud


@pytest.mark.asyncio
async def test_ip_and_user_agent_are_not_stored_in_any_column(db_session, live_tenant):
    sender = FakeSender()
    await deliver(db_session, live_tenant, shopify_order(), sender)
    tables = [r[0] for r in db_session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))]
    assert "orders" in tables
    for table in tables:
        rows = db_session.execute(text(f'SELECT * FROM "{table}"')).all()  # noqa: S608 (table names from sqlite_master)
        blob = repr(rows)
        assert IP not in blob, table
        assert "Mozilla" not in blob, table
        assert "Chrome/126" not in blob, table
    assert not {"client_ip", "user_agent", "ip", "ip_address"} & {c.name for c in Order.__table__.columns}


@pytest.mark.asyncio
async def test_fulfillment_triggered_send_uses_stored_fbp_fbc_and_stored_ip_ua(db_session, live_tenant):
    sender = FakeSender()
    held = await deliver(db_session, live_tenant, shopify_order(financial_status="pending"), sender)
    assert held["action"] == "DEFERRED"  # COD shipped, not yet delivered
    res = await deliver(db_session, live_tenant,
                        {"id": 9, "order_id": 555001, "status": "success", "shipment_status": "delivered"},
                        sender, topic="fulfillments/update")
    assert res["action"] == "SENT"
    ud = user_data(sender)
    assert ud["fbp"] == FBP and ud["fbc"] == FBC
    # the fulfillments/update payload has no client_details, but the order webhook's IP/UA were stored encrypted
    assert ud["client_ip_address"] == IP and ud["client_user_agent"] == UA


@pytest.mark.asyncio
async def test_result_does_not_leak_ip_or_user_agent(db_session, live_tenant):
    res = await deliver(db_session, live_tenant, shopify_order(), FakeSender())
    assert IP not in repr(res) and "Mozilla" not in repr(res)


@pytest.mark.asyncio
async def test_stateless_handler_passes_attribution_to_payload():
    parsed_decision = await order_webhook_processor.handle_order_update("shopify", shopify_order())
    ud = parsed_decision["d005_decision"]["payload"]["data"][0]["user_data"]
    assert ud["fbp"] == FBP and ud["fbc"] == FBC
    assert ud["client_ip_address"] == IP and ud["client_user_agent"] == UA


# --- storefront JS ------------------------------------------------------------------------------------------

def test_storefront_capture_script_js_tests():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; storefront/shopify/test_capture.mjs not run")
    result = subprocess.run([node, "storefront/shopify/test_capture.mjs"], cwd=ROOT, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_salla_storefront_capture_script_js_tests():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; storefront/salla/test_capture.mjs not run")
    result = subprocess.run([node, "storefront/salla/test_capture.mjs"], cwd=ROOT, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_capture_script_stays_small():
    script = (ROOT / "storefront/shopify/assets/motahai-capture.js").read_text(encoding="utf-8")
    assert len(script.encode("utf-8")) < 6000  # source with comments; ~3.6 KB without them


def test_salla_capture_script_stays_small():
    script = (ROOT / "storefront/salla_capture.js").read_text(encoding="utf-8")
    assert len(script.encode("utf-8")) < 10000

