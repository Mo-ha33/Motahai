"""test_tenant_api_keys.py — M3-3: per-tenant scoped API keys."""

import hmac as hmac_module

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import tenant_auth
from src.ameen_workforce.db import Tenant, TenantApiKey, create_tenant, init_db, make_engine
from src.ameen_workforce.tenant_auth import ensure_tenant_matches, require_tenant_key
from src.ameen_workforce.tenant_key_routes import router as key_router
from src.ameen_workforce.webhook_routes import get_session_factory_dep

OP = {"Authorization": "Bearer op-secret-key"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", "op-secret-key")
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        a = create_tenant(s, name="A", platform="shopify", shop_domain="a.myshopify.com")
        b = create_tenant(s, name="B", platform="shopify", shop_domain="b.myshopify.com")
        ids = (a.id, b.id)

    app = FastAPI()
    app.include_router(key_router)

    @app.get("/v1/tenants/{tenant_id}/stats")
    def stats(tenant_id: int, tenant: Tenant = Depends(require_tenant_key("stats:read"))):
        ensure_tenant_matches(tenant, tenant_id)
        return {"tenant_id": tenant.id}

    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    yield TestClient(app), factory, ids
    engine.dispose()


def _create(client, tid, **body):
    r = client.post(f"/v1/operator/tenants/{tid}/api-keys", json=body, headers=OP)
    assert r.status_code == 201, r.text
    return r.json()


def _get(client, tid, key):
    return client.get(f"/v1/tenants/{tid}/stats", headers={"Authorization": f"Bearer {key}"})


def test_create_returns_key_once_and_list_never_exposes_it(env):
    client, _, (a, _b) = env
    created = _create(client, a, label="portal")
    key = created["api_key"]
    assert key.startswith("mtk_") and key[4:12] == created["key_prefix"] and key[12] == "_"
    assert len(key) - 13 >= 43  # token_urlsafe(32)
    listing = client.get(f"/v1/operator/tenants/{a}/api-keys", headers=OP)
    assert listing.status_code == 200
    assert key not in listing.text and "key_hash" not in listing.text and "api_key" not in listing.text
    assert listing.json()[0]["key_prefix"] == created["key_prefix"]
    assert listing.json()[0]["label"] == "portal" and listing.json()[0]["scopes"] == "stats:read"


def test_operator_auth_required(env):
    client, _, (a, _b) = env
    assert client.post(f"/v1/operator/tenants/{a}/api-keys", json={}).status_code == 401
    assert client.get(f"/v1/operator/tenants/{a}/api-keys",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


def test_valid_key_resolves_tenant_and_updates_last_used(env):
    client, factory, (a, _b) = env
    key = _create(client, a)["api_key"]
    r = _get(client, a, key)
    assert r.status_code == 200 and r.json() == {"tenant_id": a}
    with factory() as s:
        assert s.scalar(select(TenantApiKey)).last_used_at is not None


def test_bad_keys_all_401_identical(env):
    client, factory, (a, _b) = env
    created = _create(client, a)
    key = created["api_key"]
    wrong = key[:-4] + ("AAAA" if not key.endswith("AAAA") else "BBBB")
    unknown = "mtk_deadbeef_" + "x" * 43
    revoked = _create(client, a)
    client.post(f"/v1/operator/tenants/{a}/api-keys/{revoked['id']}/revoke", headers=OP)
    responses = [
        client.get(f"/v1/tenants/{a}/stats"),
        client.get(f"/v1/tenants/{a}/stats", headers={"Authorization": "Basic abc"}),
        _get(client, a, "garbage"),
        _get(client, a, wrong),
        _get(client, a, unknown),
        _get(client, a, revoked["api_key"]),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1
    assert _get(client, a, key).status_code == 200


def test_rotate_revokes_old_key(env):
    client, _, (a, _b) = env
    old = _create(client, a, label="x", scopes="stats:read")
    r = client.post(f"/v1/operator/tenants/{a}/api-keys/{old['id']}/rotate", headers=OP)
    assert r.status_code == 201
    new = r.json()
    assert new["id"] != old["id"] and new["label"] == "x"
    assert _get(client, a, old["api_key"]).status_code == 401
    assert _get(client, a, new["api_key"]).status_code == 200
    listing = client.get(f"/v1/operator/tenants/{a}/api-keys", headers=OP).json()
    assert [bool(k["revoked_at"]) for k in listing] == [True, False]


def test_scope_enforced(env):
    client, _, (a, _b) = env
    key = _create(client, a, scopes="orders:read")["api_key"]
    assert _get(client, a, key).status_code == 401


def test_cross_tenant_url_is_404(env):
    client, _, (a, b) = env
    key = _create(client, a)["api_key"]
    assert _get(client, b, key).status_code == 404
    assert _get(client, a, key).status_code == 200


def test_operator_cannot_touch_key_under_other_tenant(env):
    client, _, (a, b) = env
    k = _create(client, a)
    assert client.post(f"/v1/operator/tenants/{b}/api-keys/{k['id']}/revoke", headers=OP).status_code == 404
    assert client.post(f"/v1/operator/tenants/9999/api-keys", json={}, headers=OP).status_code == 404


def test_no_plaintext_key_in_db(env):
    client, factory, (a, _b) = env
    key = _create(client, a)["api_key"]
    secret = key.split("_", 2)[2]
    with factory() as s:
        row = s.scalar(select(TenantApiKey))
        values = [str(getattr(row, c.name)) for c in TenantApiKey.__table__.columns]
        assert all(key not in v and secret not in v for v in values)
        assert row.key_hash == tenant_auth.hash_key(key) and len(row.key_hash) == 64
        assert key not in repr(row)


def test_timing_safe_compare_used(env, monkeypatch):
    client, _, (a, _b) = env
    key = _create(client, a)["api_key"]
    calls = []
    real = hmac_module.compare_digest
    monkeypatch.setattr(tenant_auth.hmac, "compare_digest", lambda x, y: calls.append(1) or real(x, y))
    _get(client, a, key)
    _get(client, a, "mtk_deadbeef_" + "x" * 43)  # unknown prefix still compares
    assert len(calls) == 2
