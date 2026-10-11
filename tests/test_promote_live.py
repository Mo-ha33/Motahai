"""
test_promote_live.py — S3-4: POST /v1/tenants/{tenant_key}/promote-live (Hermes bearer + human-minted action token)
and the operator mint endpoint POST /approvals/promote-live.

The clock for the endpoint is pinned to NOW (2026-10-11 12:00 UTC). Tokens are real Ed25519 tokens whose expiry is
relative to the wall clock, so "expired" tokens are forged with a past `exp`.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import hitl_tokens, tenant_api
from src.ameen_workforce.credentials import FERNET_KEY_ENV, generate_key, store_credential
from src.ameen_workforce.db import (
    CapiEvent, Order, OrderStatusEvent, Tenant, TenantApiKey, create_tenant, init_db, make_engine
)
from src.ameen_workforce.service import app
from src.ameen_workforce.tenant_api import get_tenant_api_session_factory
from src.ameen_workforce.tenant_auth import create_tenant_key
from src.ameen_workforce.webhook_routes import get_session_factory_dep

HERMES_KEY = "hermes-tenant-key-for-promote-tests"
OPERATOR_KEY = "operator-key-for-promote-tests"
HERMES = {"Authorization": f"Bearer {HERMES_KEY}"}
OPERATOR = {"Authorization": f"Bearer {OPERATOR_KEY}"}
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
A = "a.myshopify.com"
B = "b.myshopify.com"
ALL_IDS = ["inactive", "checklist_not_ready", "test_event_not_verified", "test_event_code_still_set",
           "no_delivered_evidence", "no_shadow_events"]
_n = {"i": 0}


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def seed_ready(session, domain, platform="shopify"):
    """A tenant that satisfies every promote-live precondition (shadow mode)."""
    _n["i"] += 1
    tenant = create_tenant(session, name=domain, platform=platform, shop_domain=domain, meta_dataset_id="11111")
    store_credential(session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-123")
    create_tenant_key(session, tenant.id, "initial")
    tenant.test_event_verified_at = NOW - timedelta(hours=1)
    order = Order(tenant_id=tenant.id, platform_order_id=f"po{_n['i']}", is_cod=True, value=100.0, currency="EGP",
                  current_status="delivered", delivered_at=NOW - timedelta(days=2), created_at=NOW - timedelta(days=3))
    session.add(order)
    session.flush()
    session.add(OrderStatusEvent(order_id=order.id, status="delivered", source="webhook", topic="bosta/delivered",
                                 received_at=NOW - timedelta(days=2)))
    session.add(CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name="DeliveredPurchase",
                          event_id=f"delivered_{order.id}", status="shadow", created_at=NOW - timedelta(days=1)))
    session.commit()
    return tenant


@pytest.fixture
def world(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "keys" / "hitl_private.pem"
    key_file.parent.mkdir()
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    if os.name == "posix":
        key_file.chmod(0o600)
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY", b64(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)))
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY_PATH", raising=False)
    monkeypatch.setenv("HITL_USED_NONCES_PATH", str(tmp_path / "state" / "nonces.json"))
    monkeypatch.setenv("HERMES_TENANT_API_KEY", HERMES_KEY)
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv(FERNET_KEY_ENV, generate_key())
    monkeypatch.setattr(tenant_api, "_now", lambda: NOW)
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.dependency_overrides[get_tenant_api_session_factory] = lambda: factory
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    with factory() as session:
        seed_ready(session, A)
        seed_ready(session, B)
    yield {"client": TestClient(app), "factory": factory, "key": key, "nonces": tmp_path / "state" / "nonces.json"}
    app.dependency_overrides.pop(get_tenant_api_session_factory, None)
    app.dependency_overrides.pop(get_session_factory_dep, None)
    engine.dispose()


def mint(tenant_key=A, action="promote_live"):
    return hitl_tokens.issue_action_token(action, tenant_key)


def forge(*, act="promote_live", sub=A, exp=None, nonce="n-forged", key):
    claims = {"act": act, "sub": sub, "exp": int(time.time()) + 600 if exp is None else exp, "nonce": nonce}
    payload = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{b64(key.sign(payload.encode('ascii')))}"


def promote(world, token, tenant_key=A, headers=HERMES):
    return world["client"].post(f"/v1/tenants/{tenant_key}/promote-live", headers=headers,
                                json={"approval_token": token})


def tenant_row(world, domain=A):
    with world["factory"]() as s:
        t = s.scalar(select(Tenant).where(Tenant.shop_domain == domain))
        return t.mode, t.promoted_live_at


def mutate(world, fn, domain=A):
    with world["factory"]() as s:
        tenant = s.scalar(select(Tenant).where(Tenant.shop_domain == domain))
        fn(s, tenant)
        s.commit()


def token_still_valid(world, token, tenant_key=A):
    return hitl_tokens.verify_action_token(token, "promote_live", tenant_key, str(world["nonces"]), consume=False)[0]


# --- break exactly one precondition ----------------------------------------------------------------------

def _inactive(s, t):
    t.active = False


def _checklist(s, t):
    t.meta_dataset_id = None


def _unverified(s, t):
    t.test_event_verified_at = None


def _code_set(s, t):
    t.meta_test_event_code = "TEST12345"


def _no_delivered(s, t):
    for ev in s.scalars(select(OrderStatusEvent)).all():
        s.delete(ev)
    for o in s.scalars(select(Order).where(Order.tenant_id == t.id)).all():
        o.current_status, o.delivered_at = "paid", None


def _no_shadow(s, t):
    for ev in s.scalars(select(CapiEvent).where(CapiEvent.tenant_id == t.id)).all():
        ev.status = "sent"


BREAKERS = {"inactive": _inactive, "checklist_not_ready": _checklist, "test_event_not_verified": _unverified,
            "test_event_code_still_set": _code_set, "no_delivered_evidence": _no_delivered,
            "no_shadow_events": _no_shadow}


# =========================================================================================================
# Auth / lookup / ordering
# =========================================================================================================
def test_401_without_or_with_wrong_bearer(world):
    token = mint()
    c = world["client"]
    assert c.post(f"/v1/tenants/{A}/promote-live", json={"approval_token": token}).status_code == 401
    assert c.post(f"/v1/tenants/{A}/promote-live").status_code == 401  # auth answers before body validation
    for headers in ({"Authorization": "Bearer nope"}, {"Authorization": f"Bearer {OPERATOR_KEY}"}):
        assert promote(world, token, headers=headers).status_code == 401
    assert tenant_row(world)[0] == "shadow" and token_still_valid(world, token)


def test_401_when_hermes_key_unset_or_equal_to_operator_key(world, monkeypatch):
    token = mint()
    monkeypatch.setenv("OPERATOR_API_KEY", HERMES_KEY)
    assert promote(world, token).status_code == 401
    monkeypatch.delenv("HERMES_TENANT_API_KEY")
    assert promote(world, token).status_code == 401
    assert tenant_row(world)[0] == "shadow"


def test_404_unknown_tenant_is_generic(world):
    resp = promote(world, mint("ghost.myshopify.com"), tenant_key="ghost.myshopify.com")
    assert resp.status_code == 404 and resp.json() == {"detail": "Not found"}


def test_422_when_token_field_missing(world):
    resp = world["client"].post(f"/v1/tenants/{A}/promote-live", headers=HERMES, json={})
    assert resp.status_code == 422


def test_already_live_is_409_and_does_not_consume(world):
    token = mint()
    mutate(world, lambda s, t: setattr(t, "mode", "live"))
    resp = promote(world, token)
    assert resp.status_code == 409 and resp.json() == {"error": "already_live"}
    assert token_still_valid(world, token)
    # already_live is answered before token validation: even garbage gets the same answer
    assert promote(world, "garbage").json() == {"error": "already_live"}


# =========================================================================================================
# Token validity -> 403 token_rejected (no detail)
# =========================================================================================================
def test_token_rejections_are_403_with_no_detail(world):
    key = world["key"]
    cases = {
        "garbage": "not-a-token",
        "empty": "",
        "bad_signature": forge(key=Ed25519PrivateKey.generate()),
        "expired": forge(exp=int(time.time()) - 5, key=key),
        "wrong_act": forge(act="something_else", key=key),
        "wrong_sub": mint(B),
        "publish_token": hitl_tokens.issue_publish_token("GTM-ABC", "2"),
    }
    for name, token in cases.items():
        resp = promote(world, token)
        assert resp.status_code == 403, name
        assert resp.json() == {"error": "token_rejected"}, name
    assert tenant_row(world)[0] == "shadow"


def test_reused_token_is_403(world):
    token = mint()
    hitl_tokens.verify_action_token(token, "promote_live", A, str(world["nonces"]))  # someone spent it
    assert promote(world, token).json() == {"error": "token_rejected"}
    assert tenant_row(world)[0] == "shadow"


def test_cross_tenant_token_is_403_and_does_not_flip_either_tenant(world):
    token_for_a = mint(A)
    resp = promote(world, token_for_a, tenant_key=B)
    assert resp.status_code == 403 and resp.json() == {"error": "token_rejected"}
    assert tenant_row(world, A)[0] == tenant_row(world, B)[0] == "shadow"
    assert token_still_valid(world, token_for_a)  # a wrong-tenant attempt did not burn it
    assert promote(world, token_for_a).status_code == 200


def test_token_is_checked_before_preconditions(world):
    mutate(world, _inactive)
    resp = promote(world, "garbage")
    assert resp.status_code == 403 and "failed" not in resp.json()  # no precondition oracle for a bad token


# =========================================================================================================
# Preconditions -> 409 precondition_failed (token NOT consumed)
# =========================================================================================================
@pytest.mark.parametrize("precondition", ALL_IDS)
def test_each_precondition_individually_blocks_without_consuming_the_token(world, precondition):
    token = mint()
    mutate(world, BREAKERS[precondition])
    resp = promote(world, token)
    assert resp.status_code == 409
    assert resp.json() == {"error": "precondition_failed", "failed": [precondition]}
    assert tenant_row(world) == ("shadow", None)
    assert token_still_valid(world, token)


def test_all_preconditions_listed_in_order(world):
    token = mint()
    for fn in BREAKERS.values():
        mutate(world, fn)
    resp = promote(world, token)
    assert resp.status_code == 409
    assert resp.json() == {"error": "precondition_failed", "failed": ALL_IDS}


@pytest.mark.parametrize("precondition", ALL_IDS)
def test_same_token_succeeds_once_the_precondition_is_fixed(world, precondition):
    token = mint()
    mutate(world, BREAKERS[precondition])
    assert promote(world, token).status_code == 409
    # repair
    if precondition == "inactive":
        mutate(world, lambda s, t: setattr(t, "active", True))
    elif precondition == "checklist_not_ready":
        mutate(world, lambda s, t: setattr(t, "meta_dataset_id", "11111"))
    elif precondition == "test_event_not_verified":
        mutate(world, lambda s, t: setattr(t, "test_event_verified_at", NOW - timedelta(minutes=5)))
    elif precondition == "test_event_code_still_set":
        mutate(world, lambda s, t: setattr(t, "meta_test_event_code", None))
    elif precondition == "no_delivered_evidence":
        def fix(s, t):
            o = s.scalar(select(Order).where(Order.tenant_id == t.id))
            o.current_status, o.delivered_at = "delivered", NOW - timedelta(days=1)
        mutate(world, fix)
    else:
        def fix(s, t):
            for ev in s.scalars(select(CapiEvent).where(CapiEvent.tenant_id == t.id)).all():
                ev.status = "shadow"
        mutate(world, fix)
    resp = promote(world, token)
    assert resp.status_code == 200, resp.text
    assert tenant_row(world)[0] == "live"


def test_checklist_not_ready_when_api_key_revoked(world):
    def revoke(s, t):
        for k in s.scalars(select(TenantApiKey).where(TenantApiKey.tenant_id == t.id)).all():
            k.revoked_at = NOW
    mutate(world, revoke)
    assert promote(world, mint()).json() == {"error": "precondition_failed", "failed": ["checklist_not_ready"]}


def test_shopify_and_salla_do_not_need_a_courier_secret_to_promote(world):
    with world["factory"]() as s:
        seed_ready(s, "777", platform="salla")  # no courier secret stored anywhere
    resp = promote(world, mint("777"), tenant_key="777")
    assert resp.status_code == 200, resp.text


def test_zid_without_courier_secret_fails_the_checklist(world):
    with world["factory"]() as s:
        seed_ready(s, "zid-store.example", platform="zid")
    resp = promote(world, mint("zid-store.example"), tenant_key="zid-store.example")
    assert resp.json() == {"error": "precondition_failed", "failed": ["checklist_not_ready"]}


# --- delivered / shadow evidence windows --------------------------------------------------------------

def test_delivered_evidence_window_is_14_days(world):
    def age(days):
        def fn(s, t):
            for ev in s.scalars(select(OrderStatusEvent)).all():
                ev.received_at = NOW - timedelta(days=days)
            for o in s.scalars(select(Order).where(Order.tenant_id == t.id)).all():
                o.delivered_at = NOW - timedelta(days=days)
        return fn
    token = mint()
    mutate(world, age(15))
    assert promote(world, token).json()["failed"] == ["no_delivered_evidence"]
    mutate(world, age(13))
    assert promote(world, token).status_code == 200


def test_delivered_evidence_from_status_event_alone_counts(world):
    """e.g. a courier webhook that moved the order to delivered; the order row may later have changed."""
    def fn(s, t):
        o = s.scalar(select(Order).where(Order.tenant_id == t.id))
        o.current_status, o.delivered_at = "refunded", None
    mutate(world, fn)
    assert promote(world, mint()).status_code == 200


def test_delivered_evidence_from_order_row_alone_counts(world):
    def fn(s, t):
        for ev in s.scalars(select(OrderStatusEvent)).all():
            s.delete(ev)
    mutate(world, fn)
    assert promote(world, mint()).status_code == 200


def test_paid_or_shipped_is_not_delivered_evidence(world):
    def fn(s, t):
        for ev in s.scalars(select(OrderStatusEvent)).all():
            ev.status = "shipped"
        for o in s.scalars(select(Order).where(Order.tenant_id == t.id)).all():
            o.current_status, o.delivered_at = "paid", NOW - timedelta(days=1)
    mutate(world, fn)
    assert promote(world, mint()).json()["failed"] == ["no_delivered_evidence"]


def test_delivered_evidence_is_per_tenant(world):
    """Tenant B's delivered order must not satisfy tenant A."""
    mutate(world, _no_delivered, domain=A)
    # _no_delivered deleted *all* status events; give B a fresh one
    def fn(s, t):
        o = s.scalar(select(Order).where(Order.tenant_id == t.id))
        o.current_status, o.delivered_at = "delivered", NOW - timedelta(days=1)
        s.add(OrderStatusEvent(order_id=o.id, status="delivered", received_at=NOW - timedelta(days=1)))
    mutate(world, fn, domain=B)
    assert promote(world, mint(A)).json()["failed"] == ["no_delivered_evidence"]
    assert promote(world, mint(B), tenant_key=B).status_code == 200


def test_shadow_window_is_7_days_and_only_shadow_counts(world):
    def at(days, status="shadow"):
        def fn(s, t):
            for ev in s.scalars(select(CapiEvent).where(CapiEvent.tenant_id == t.id)).all():
                ev.status, ev.created_at, ev.sent_at = status, NOW - timedelta(days=days), None
        return fn
    token = mint()
    mutate(world, at(8))
    assert promote(world, token).json()["failed"] == ["no_shadow_events"]
    mutate(world, at(1, "sent"))
    assert promote(world, token).json()["failed"] == ["no_shadow_events"]
    mutate(world, at(6))
    assert promote(world, token).status_code == 200


# =========================================================================================================
# Success path
# =========================================================================================================
def test_success_flips_mode_sets_timestamp_and_is_single_use(world, caplog):
    caplog.set_level(logging.DEBUG)
    token = mint()
    resp = promote(world, token)
    assert resp.status_code == 200
    assert resp.json() == {"tenant_key": A, "mode": "live", "promoted_live_at": NOW.isoformat()}
    mode, promoted = tenant_row(world)
    assert mode == "live" and promoted == NOW
    assert tenant_row(world, B) == ("shadow", None)
    assert not token_still_valid(world, token)  # spent
    # second use: already live
    assert promote(world, token).json() == {"error": "already_live"}
    # even if the mode were reset, the spent token cannot flip it again
    mutate(world, lambda s, t: setattr(t, "mode", "shadow"))
    assert promote(world, token).json() == {"error": "token_rejected"}
    assert tenant_row(world)[0] == "shadow"
    assert token not in caplog.text


def test_tenant_key_in_path_is_normalized(world):
    resp = promote(world, mint(A), tenant_key=A.upper())
    assert resp.status_code == 200 and resp.json()["tenant_key"] == A


def test_losing_the_consume_race_is_403_and_nothing_flips(world, monkeypatch):
    real = hitl_tokens.verify_action_token

    def racing(token, action, subject, store, now=None, consume=True):
        if consume:
            return False, "nonce_reused_or_store_unavailable"  # someone else spent it between check and consume
        return real(token, action, subject, store, now=now, consume=consume)

    monkeypatch.setattr(tenant_api, "verify_action_token", racing)
    resp = promote(world, mint())
    assert resp.status_code == 403 and resp.json() == {"error": "token_rejected"}
    assert tenant_row(world) == ("shadow", None)


def test_token_never_appears_in_any_response_or_log(world, caplog):
    caplog.set_level(logging.DEBUG)
    good = mint()
    bad = forge(act="x", key=world["key"])
    mutate(world, _code_set)
    responses = [promote(world, bad), promote(world, good)]  # token_rejected, precondition_failed
    mutate(world, lambda s, t: setattr(t, "meta_test_event_code", None))
    responses += [promote(world, good), promote(world, good)]  # 200, already_live
    assert [r.status_code for r in responses] == [403, 409, 200, 409]
    for resp in responses:
        assert good not in resp.text and bad not in resp.text
    for token in (good, bad):
        assert token not in caplog.text
        assert token.split(".")[0] not in caplog.text and token.split(".")[1] not in caplog.text
    # logs carry the numeric tenant id and outcome
    assert "promote-live tenant 1: promoted" in caplog.text


# =========================================================================================================
# Operator mint endpoint: POST /approvals/promote-live
# =========================================================================================================
def mint_via_api(world, tenant_key=A, headers=OPERATOR):
    return world["client"].post("/approvals/promote-live", headers=headers, json={"tenant_key": tenant_key})


def test_mint_requires_operator_key_not_the_hermes_key(world):
    assert world["client"].post("/approvals/promote-live", json={"tenant_key": A}).status_code == 401
    assert mint_via_api(world, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert mint_via_api(world, headers={"Authorization": OPERATOR_KEY}).status_code == 401  # not a Bearer header
    assert mint_via_api(world, headers=HERMES).status_code == 401  # HERMES_TENANT_API_KEY must NOT work here


def test_mint_unknown_tenant_is_404(world):
    resp = mint_via_api(world, "ghost.myshopify.com")
    assert resp.status_code == 404 and resp.json() == {"detail": "Not found"}


def test_mint_returns_token_that_works_on_promote_live(world):
    resp = mint_via_api(world, " A.MyShopify.com ")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_key"] == A and body["single_use"] is True and body["gate"] == "promote_live"
    assert set(body) >= {"approval_token", "expires_at"}
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=25) < expires - datetime.now(timezone.utc) <= timedelta(minutes=31)
    promoted = promote(world, body["approval_token"])
    assert promoted.status_code == 200 and promoted.json()["mode"] == "live"
    assert promote(world, body["approval_token"], tenant_key=B).status_code in (403, 409)


def test_minted_token_is_bound_to_its_tenant(world):
    token = mint_via_api(world, A).json()["approval_token"]
    assert promote(world, token, tenant_key=B).json() == {"error": "token_rejected"}
    assert tenant_row(world, B)[0] == "shadow"


def test_mint_without_signing_key_is_503(world, monkeypatch):
    monkeypatch.delenv("HITL_SIGNING_PRIVATE_KEY_PATH")
    assert mint_via_api(world).status_code == 503


def test_mint_does_not_log_the_token(world, caplog):
    caplog.set_level(logging.DEBUG)
    token = mint_via_api(world).json()["approval_token"]
    assert token not in caplog.text
