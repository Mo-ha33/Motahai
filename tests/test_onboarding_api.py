"""test_onboarding_api.py — M3-6: operator onboarding API."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce.credentials import FERNET_KEY_ENV, generate_key, get_credential, store_credential
from src.ameen_workforce.db import Credential, Tenant, init_db, make_engine
from src.ameen_workforce.onboarding_routes import compute_checklist, router
from src.ameen_workforce.webhook_routes import get_session_factory_dep

OP = {"Authorization": "Bearer op-secret-key"}
B = "/v1/operator/onboarding"
TOKEN = "EAAB-super-secret-capi-token"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", "op-secret-key")
    monkeypatch.setenv(FERNET_KEY_ENV, generate_key())
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    yield TestClient(app), factory
    engine.dispose()


def _create(client, domain="Shop.MyShopify.com", platform="shopify"):
    return client.post(f"{B}/tenants", headers=OP, json=dict(
        shop_domain=domain, platform=platform, country="EG", currency="EGP"))


def _steps(client, tid):
    r = client.get(f"{B}/tenants/{tid}/checklist", headers=OP)
    assert r.status_code == 200, r.text
    return r.json(), {s["key"]: s["status"] for s in r.json()["steps"]}


def test_auth_required_on_every_endpoint(env):
    client, _ = env
    calls = [("post", f"{B}/tenants", {"shop_domain": "x.com", "platform": "shopify", "country": "EG", "currency": "EGP"}),
             ("post", f"{B}/tenants/1/meta", {"meta_dataset_id": "12345", "meta_capi_token": TOKEN}),
             ("post", f"{B}/tenants/1/courier-secrets", {}),
             ("post", f"{B}/tenants/1/api-key", None),
             ("get", f"{B}/tenants/1/checklist", None)]
    for method, url, body in calls:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            kwargs = {"json": body} if body is not None else {}
            r = getattr(client, method)(url, headers=headers, **kwargs)
            assert r.status_code in (401, 403), (url, r.status_code)


def test_create_starts_in_shadow_and_duplicate_is_409(env):
    client, factory = env
    r = _create(client)
    assert r.status_code == 201
    assert r.json()["mode"] == "shadow" and r.json()["live_sending"] is False
    with factory() as s:
        assert s.get(Tenant, r.json()["tenant_id"]).mode == "shadow"
    assert _create(client, domain="shop.myshopify.com").status_code == 409


def test_unknown_tenant_404_and_bad_platform_422(env):
    client, _ = env
    assert client.get(f"{B}/tenants/99/checklist", headers=OP).status_code == 404
    r = client.post(f"{B}/tenants", headers=OP,
                    json=dict(shop_domain="a.com", platform="zid", country="EG", currency="EGP"))
    assert r.status_code == 422


def test_meta_token_never_echoed_and_encrypted(env):
    client, factory = env
    tid = _create(client).json()["tenant_id"]
    r = client.post(f"{B}/tenants/{tid}/meta", headers=OP, json=dict(
        meta_dataset_id="123456789", meta_capi_token=TOKEN, test_event_code="TEST123"))
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
    assert TOKEN not in client.get(f"{B}/tenants/{tid}/checklist", headers=OP).text
    with factory() as s:
        assert get_credential(s, tid, "meta_capi_token") == TOKEN
        assert TOKEN not in s.scalar(select(Credential.ciphertext).where(Credential.kind == "meta_capi_token"))
        assert s.get(Tenant, tid).meta_test_event_code == "TEST123"


def test_courier_secrets_returned_once_and_long_enough(env):
    client, factory = env
    tid = _create(client).json()["tenant_id"]
    r = client.post(f"{B}/tenants/{tid}/courier-secrets", headers=OP, json={})
    assert r.status_code == 201, r.text
    sec = r.json()["secrets"]
    assert sec["bosta"]["url"].endswith("/webhooks/bosta/shop.myshopify.com")
    assert sec["oto"]["url"].endswith("/webhooks/oto/shop.myshopify.com")
    assert len(sec["bosta"]["secret"]) >= 24
    with factory() as s:
        assert get_credential(s, tid, "bosta_webhook_secret") == sec["bosta"]["secret"]
    assert client.post(f"{B}/tenants/{tid}/courier-secrets", headers=OP, json={}).status_code == 409
    r2 = client.post(f"{B}/tenants/{tid}/courier-secrets", headers=OP, json={"couriers": ["bosta"], "rotate": True})
    assert r2.status_code == 201 and set(r2.json()["secrets"]) == {"bosta"}
    assert r2.json()["secrets"]["bosta"]["secret"] != sec["bosta"]["secret"]
    with factory() as s:  # OTO untouched by a bosta-only rotation
        assert get_credential(s, tid, "oto_webhook_secret") == sec["oto"]["secret"]
    body, _ = _steps(client, tid)
    assert sec["bosta"]["secret"] not in str(body) and r2.json()["secrets"]["bosta"]["secret"] not in str(body)


def test_api_key_returned_once(env):
    client, _ = env
    tid = _create(client).json()["tenant_id"]
    r = client.post(f"{B}/tenants/{tid}/api-key", headers=OP)
    assert r.status_code == 201 and r.json()["api_key"].startswith("mtk_")
    assert r.json()["api_key"] not in client.get(f"{B}/tenants/{tid}/checklist", headers=OP).text
    assert client.post(f"{B}/tenants/{tid}/api-key", headers=OP).status_code == 409


def test_checklist_transitions_to_ready_but_never_goes_live(env):
    client, factory = env
    tid = _create(client, platform="salla").json()["tenant_id"]
    body, st = _steps(client, tid)
    assert st == {"tenant_created": "done", "meta_dataset": "missing", "meta_capi_token": "missing",
                  "courier_secret": "missing", "api_key": "missing", "platform_webhooks": "manual",
                  "test_event_code": "missing"}
    assert body["ready_for_live"] is False
    assert body["platform_webhook_urls"][0].endswith("/webhooks/salla")
    courier = next(s for s in body["steps"] if s["key"] == "courier_secret")
    assert courier["required"] is False and "promote-live" in courier["detail"]  # Salla exemption

    client.post(f"{B}/tenants/{tid}/meta", headers=OP, json=dict(meta_dataset_id="123456789", meta_capi_token=TOKEN))
    body, st = _steps(client, tid)
    assert st["meta_dataset"] == st["meta_capi_token"] == "done"
    assert body["ready_for_live"] is False

    client.post(f"{B}/tenants/{tid}/api-key", headers=OP)
    body, st = _steps(client, tid)
    assert st["courier_secret"] == "missing" and st["api_key"] == "done"
    assert body["ready_for_live"] is True  # Salla needs no courier secret

    client.post(f"{B}/tenants/{tid}/courier-secrets", headers=OP, json={"couriers": ["oto"]})
    body, st = _steps(client, tid)
    assert st["courier_secret"] == "done" and st["api_key"] == "done"
    assert st["platform_webhooks"] == "manual"  # never auto-done, never blocks readiness
    assert body["ready_for_live"] is True
    with factory() as s:
        assert s.get(Tenant, tid).mode == "shadow"


def test_short_bosta_secret_does_not_satisfy_checklist(env):
    client, factory = env
    tid = _create(client).json()["tenant_id"]
    with factory() as s:
        store_credential(s, tid, "bosta_webhook_secret", "short")
    _, st = _steps(client, tid)
    assert st["courier_secret"] == "missing"


def _courier_required(client, tid):
    body, st = _steps(client, tid)
    step = next(s for s in body["steps"] if s["key"] == "courier_secret")
    return step["required"], body


def _complete_everything_but_courier(client, tid):
    client.post(f"{B}/tenants/{tid}/meta", headers=OP, json=dict(meta_dataset_id="123456789", meta_capi_token=TOKEN))
    client.post(f"{B}/tenants/{tid}/api-key", headers=OP)


@pytest.mark.parametrize("platform", ["shopify", "salla"])
def test_courier_secret_is_optional_for_shopify_and_salla(env, platform):
    client, _ = env
    tid = _create(client, domain="x.myshopify.com" if platform == "shopify" else "99887", platform=platform).json()[
        "tenant_id"]
    required, _ = _courier_required(client, tid)
    assert required is False
    _complete_everything_but_courier(client, tid)
    body, st = _steps(client, tid)
    assert st["courier_secret"] == "missing" and body["ready_for_live"] is True
    detail = next(s for s in body["steps"] if s["key"] == "courier_secret")["detail"]
    assert "no_delivered_evidence" in detail and ("fulfillments/update" in detail or "delivered" in detail)


def test_courier_secret_required_for_a_platform_the_db_cannot_store_yet(env):
    """woocommerce is not a valid tenants.platform yet; the exemption must be an allowlist, not 'anything but zid'."""
    _, factory = env
    with factory() as s:
        ghost = Tenant(id=4242, name="Woo", platform="woocommerce", shop_domain="woo.example", country="EG",
                       currency="EGP", mode="shadow")
        step = next(x for x in compute_checklist(s, ghost)["steps"] if x["key"] == "courier_secret")
        assert step["required"] is True


def test_courier_secret_still_required_for_zid(env):
    client, factory = env
    tid = _create(client).json()["tenant_id"]
    platform = "zid"
    with factory() as s:
        s.get(Tenant, tid).platform = platform
        s.commit()
    required, _ = _courier_required(client, tid)
    assert required is True
    _complete_everything_but_courier(client, tid)
    body, _ = _steps(client, tid)
    assert body["ready_for_live"] is False
    client.post(f"{B}/tenants/{tid}/courier-secrets", headers=OP, json={"couriers": ["oto"]})
    assert _steps(client, tid)[0]["ready_for_live"] is True


def test_shopify_webhook_urls_include_fulfillments_update(env):
    client, _ = env
    tid = _create(client).json()["tenant_id"]
    urls = _steps(client, tid)[0]["platform_webhook_urls"]
    assert any(u.endswith("/webhooks/shopify/fulfillments/update") for u in urls)
    assert any(u.endswith("/webhooks/shopify/orders/paid") for u in urls)
