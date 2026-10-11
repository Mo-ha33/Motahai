"""
test_signal_integrity.py — Phase 1 signal integrity: E.164 phones for EG/KSA/UAE/KW (and the other GCC plans), the
pinned Graph API version and its expiry guard, and the per-tenant Meta test_event_code on pipeline sends.
"""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from scripts.onboard_store import main
from src.ameen_workforce.capi_service import (
    META_GRAPH_API_VERSION, META_GRAPH_API_VERSION_ENV, META_GRAPH_API_VERSION_EXPIRES, MetaCAPISender,
    graph_api_version, hash_phone, hash_sha256, normalize_phone, phone_default_country, quality_flags_for
)
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import (
    CapiEvent, Order, create_tenant, get_tenant_by_shop_domain, normalize_test_event_code, set_meta_test_event_code
)
from src.ameen_workforce.order_pipeline import process_webhook
from tests.test_order_pipeline import DELIVERED, FakeSender, shopify_order


# --- E.164 phone normalization ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,country,expected", [
    # Egypt (+20): mobile 10 digits after the code, Cairo landline 9
    ("01012345678", "EG", "201012345678"),
    ("1012345678", "EG", "201012345678"),           # mobile written without the trunk 0
    ("+20 10 1234 5678", "SA", "201012345678"),     # explicit international wins over the default country
    ("+20 010 1234 5678", "EG", "201012345678"),    # trunk 0 after the calling code is dropped
    ("0223456789", "EG", "20223456789"),            # Cairo landline
    # Saudi Arabia (+966)
    ("0551234567", "SA", "966551234567"),
    ("551234567", "SA", "966551234567"),
    ("00966 55 123 4567", "AE", "966551234567"),
    ("966551234567", "EG", "966551234567"),         # already international without '+'
    # UAE (+971): the audit's two failures
    ("0501234567", "AE", "971501234567"),
    ("501234567", "AE", "971501234567"),            # was mis-tagged as Saudi (966) before
    ("+971 50 123 4567", "EG", "971501234567"),
    ("971501234567", "SA", "971501234567"),
    ("00971 050 123 4567", "AE", "971501234567"),
    ("042345678", "AE", "97142345678"),             # Dubai landline
    # Kuwait (+965): 8 digits, no trunk prefix
    ("99123456", "KW", "96599123456"),
    ("9912 3456", "KW", "96599123456"),
    ("96599123456", "KW", "96599123456"),
    ("+965 9912 3456", "EG", "96599123456"),
    # Other GCC plans
    ("33123456", "QA", "97433123456"),
    ("36123456", "BH", "97336123456"),
    ("92123456", "OM", "96892123456"),
    # Never guessed into a country
    ("+447911123456", "EG", "447911123456"),
    ("12345", "EG", None),                          # fits no plan: no phone rather than a hash that never matches
    ("99123456", "EG", None),                       # a KW-looking number with an EG default is not guessed
    ("+20 12", "EG", None),                         # too short for E.164
    ("+1234567890123456", "EG", None),              # more than 15 digits
    ("4155550123", "US", "4155550123"),             # unknown default country: kept at a plausible length
    ("", "EG", None),
    (None, "EG", None),
    ("abc", "EG", None),
])
def test_e164_normalization(raw, country, expected):
    assert normalize_phone(raw, country) == expected


def test_uae_number_is_no_longer_hashed_as_saudi():
    assert hash_phone("501234567", "AED") == hash_sha256("971501234567")
    assert hash_phone("501234567", "AED") != hash_sha256("966501234567")


@pytest.mark.parametrize("currency,country,ship,expected", [
    ("AED", "EG", None, "AE"),
    ("KWD", "EG", None, "KW"),
    ("EGP", "EG", "AE", "AE"),     # shipping country beats currency
    ("SAR", "SA", "kw", "KW"),
    ("USD", "AE", None, "AE"),     # unmapped currency falls back to the tenant country
    ("USD", None, "US", "EG"),     # unknown shipping country and no tenant country: EG
    (None, None, None, "EG"),
])
def test_phone_default_country_precedence(currency, country, ship, expected):
    assert phone_default_country(currency, country, ship) == expected


def test_capi_payload_raw_phone_uses_gcc_currency():
    payload = MetaCAPISender().build_event_payload(
        event_name="DeliveredPurchase", event_id="delivered_1", order_id="1", value=10.0, currency="KWD",
        phone="99123456"
    )
    assert payload["data"][0]["user_data"]["ph"] == [hash_sha256("96599123456")]


@pytest.mark.asyncio
async def test_pipeline_hashes_uae_phone_with_971(db_session, fernet_key):
    tenant = create_tenant(db_session, name="UAE Shop", platform="shopify", shop_domain="uae.myshopify.com",
                           meta_dataset_id="333", country="AE", currency="AED", mode="live", settlement_hours=0)
    store_credential(db_session, tenant.id, "meta_capi_token", "TOKEN")
    sender = FakeSender()
    order = shopify_order(currency="AED", customer={"email": "a@example.com", "phone": "0501234567"}, **DELIVERED)
    res = await process_webhook(db_session, tenant, "shopify", "orders/updated", order, sender=sender)
    assert res["action"] == "SENT"
    assert db_session.scalar(select(Order)).phone_hash == hash_sha256("971501234567")
    assert sender.calls[0]["payload"]["data"][0]["user_data"]["ph"] == [hash_sha256("971501234567")]


@pytest.mark.asyncio
async def test_pipeline_uses_shipping_country_over_tenant_currency(db_session, live_tenant):
    # An EGP store shipping to Kuwait: the local number must get +965, not +20.
    order = shopify_order(customer={"email": "k@example.com", "phone": "99123456"},
                          shipping_address={"first_name": "K", "city": "Kuwait City", "country_code": "KW"})
    await process_webhook(db_session, live_tenant, "shopify", "orders/create", order, sender=FakeSender())
    assert db_session.scalar(select(Order)).phone_hash == hash_sha256("96599123456")


# --- Graph API version ------------------------------------------------------------------------------------

def test_graph_api_version_is_not_the_expired_v20():
    assert META_GRAPH_API_VERSION != "v20.0"
    assert META_GRAPH_API_VERSION.startswith("v") and META_GRAPH_API_VERSION.endswith(".0")


def test_graph_api_version_not_within_90_days_of_expiry():
    # Fails 90 days before Meta expires the pinned version: bump META_GRAPH_API_VERSION and its expiry date then.
    assert META_GRAPH_API_VERSION_EXPIRES - date.today() > timedelta(days=90), (
        f"Graph API {META_GRAPH_API_VERSION} expires {META_GRAPH_API_VERSION_EXPIRES}; move to a newer version "
        "(https://developers.facebook.com/docs/graph-api/changelog/versions)")


def test_graph_api_version_env_override(monkeypatch):
    monkeypatch.delenv(META_GRAPH_API_VERSION_ENV, raising=False)
    assert graph_api_version() == META_GRAPH_API_VERSION
    monkeypatch.setenv(META_GRAPH_API_VERSION_ENV, " v25.0 ")
    assert graph_api_version() == "v25.0"


@pytest.mark.parametrize("bad", ["22.0", "v22", "v22.1", "v22.0/../me", "latest"])
def test_graph_api_version_env_override_rejects_garbage(monkeypatch, bad):
    monkeypatch.setenv(META_GRAPH_API_VERSION_ENV, bad)
    with pytest.raises(ValueError, match=META_GRAPH_API_VERSION_ENV):
        graph_api_version()


@pytest.mark.asyncio
async def test_send_event_posts_to_pinned_version(monkeypatch):
    monkeypatch.delenv(META_GRAPH_API_VERSION_ENV, raising=False)
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"events_received": 1, "fbtrace_id": "T"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            seen["url"], seen["body"] = url, json
            return FakeResponse()

    monkeypatch.setattr("src.ameen_workforce.capi_service.httpx.AsyncClient", FakeClient)
    res = await MetaCAPISender().send_event("123", "TOKEN", {"data": []})
    assert res["status"] == "success"
    assert seen["url"] == f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/123/events"
    assert "TOKEN" not in seen["url"] and seen["body"]["access_token"] == "TOKEN"


# --- test_event_code on pipeline sends ----------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("TEST12345", "TEST12345"), ("  TEST_9-a ", "TEST_9-a"),
                                          (None, None), ("", None), ("   ", None)])
def test_normalize_test_event_code(raw, expected):
    assert normalize_test_event_code(raw) == expected


@pytest.mark.parametrize("bad", ["TEST 1", "TEST;DROP", "x" * 65, "TÉST"])
def test_normalize_test_event_code_rejects_garbage(bad):
    with pytest.raises(ValueError, match="test_event_code"):
        normalize_test_event_code(bad)


@pytest.mark.asyncio
async def test_live_tenant_with_test_code_sends_it_and_flags_the_event(db_session, live_tenant):
    set_meta_test_event_code(db_session, live_tenant, "TEST777")
    sender = FakeSender()
    res = await process_webhook(db_session, live_tenant, "shopify", "orders/updated", shopify_order(**DELIVERED),
                                sender=sender)
    assert res["action"] == "SENT"
    assert sender.all_calls and all(c["payload"]["test_event_code"] == "TEST777" for c in sender.all_calls)
    rows = db_session.scalars(select(CapiEvent)).all()
    assert {r.event_name for r in rows} == {"ConfirmedOrder", "DeliveredPurchase"}
    assert all("test_event" in (r.quality_flags or "") for r in rows)


@pytest.mark.asyncio
async def test_live_tenant_without_test_code_sends_none(db_session, live_tenant):
    sender = FakeSender()
    await process_webhook(db_session, live_tenant, "shopify", "orders/updated", shopify_order(**DELIVERED),
                          sender=sender)
    assert sender.all_calls and all("test_event_code" not in c["payload"] for c in sender.all_calls)
    assert all("test_event" not in (r.quality_flags or "") for r in db_session.scalars(select(CapiEvent)))


@pytest.mark.asyncio
async def test_clearing_the_test_code_restores_normal_sends(db_session, live_tenant):
    set_meta_test_event_code(db_session, live_tenant, "TEST1")
    assert set_meta_test_event_code(db_session, live_tenant, "") is None
    sender = FakeSender()
    await process_webhook(db_session, live_tenant, "shopify", "orders/updated", shopify_order(**DELIVERED),
                          sender=sender)
    assert all("test_event_code" not in c["payload"] for c in sender.all_calls)


@pytest.mark.asyncio
async def test_shadow_tenant_with_test_code_still_calls_nothing(db_session, shadow_tenant):
    set_meta_test_event_code(db_session, shadow_tenant, "TEST2")
    sender = FakeSender()
    res = await process_webhook(db_session, shadow_tenant, "shopify", "orders/updated", shopify_order(**DELIVERED),
                                sender=sender)
    assert res["action"] == "SHADOW" and sender.all_calls == []


def test_quality_flags_mark_test_events():
    payload = {"data": [{"user_data": {"client_user_agent": "UA"}, "event_source_url": "https://s/"}],
               "test_event_code": "TEST1"}
    assert quality_flags_for(payload) == "test_event"


# --- onboarding CLI --------------------------------------------------------------------------------------------

CLI = ["--name", "Test Store", "--platform", "shopify", "--shop-domain", "tec-store.myshopify.com",
       "--meta-dataset-id", "123", "--meta-capi-token", "TOKEN", "--json"]


@pytest.fixture
def cli_db(db_session, fernet_key, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def fake_scope(*_args, **_kwargs):
        yield db_session

    monkeypatch.setattr("scripts.onboard_store.session_scope", fake_scope)
    monkeypatch.setattr("scripts.onboard_store.init_db", lambda *a, **k: None)
    return db_session


def test_cli_sets_and_clears_pipeline_test_event_code(cli_db, capsys, monkeypatch):
    monkeypatch.setattr(MetaCAPISender, "send_event", AsyncMock())
    assert main(CLI + ["--pipeline-test-event-code", "TEST55"]) == 0
    assert json.loads(capsys.readouterr().out)["pipeline_test_event_code"] == "TEST55"
    assert get_tenant_by_shop_domain(cli_db, "tec-store.myshopify.com").meta_test_event_code == "TEST55"

    assert main(CLI) == 0  # flag omitted: unchanged
    capsys.readouterr()
    assert get_tenant_by_shop_domain(cli_db, "tec-store.myshopify.com").meta_test_event_code == "TEST55"

    assert main(CLI + ["--pipeline-test-event-code", ""]) == 0
    capsys.readouterr()
    assert get_tenant_by_shop_domain(cli_db, "tec-store.myshopify.com").meta_test_event_code is None
    MetaCAPISender.send_event.assert_not_called()


def test_cli_rejects_invalid_pipeline_test_event_code_before_writing(cli_db, capsys):
    assert main(CLI + ["--pipeline-test-event-code", "BAD CODE"]) != 0
    capsys.readouterr()
    assert get_tenant_by_shop_domain(cli_db, "tec-store.myshopify.com") is None
