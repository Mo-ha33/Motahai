"""
test_hitl_tokens.py — Rule D-003 signed, bound, short-lived, single-use approval tokens.

Covers: the issuer/verifier (src/ameen_workforce/hitl_tokens.py), the gate inside the deployed
consultation.py (loaded from the real file under a stub package), parity of the two verifier copies,
and the operator-only issuance endpoint POST /approvals/gtm-publish.
"""

import base64
import hashlib
import hmac
import importlib.util
import inspect
import json
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

from src.ameen_workforce import hitl_tokens
from src.ameen_workforce.config import settings
from src.ameen_workforce.service import app

ROOT = Path(__file__).resolve().parents[1]
SIGNING_KEY = "unit-test-signing-key-0123456789abcdef"
OPERATOR_KEY = "unit-test-operator-key"
CONTAINER = "GTM-5C5N552P"
WORKSPACE = "2"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def forge(cid=CONTAINER, wid=WORKSPACE, exp=None, nonce="n-forged-1", key=SIGNING_KEY) -> str:
    """Builds a token the same way the issuer does, with arbitrary claims/key (for negative tests)."""
    claims = {"cid": cid, "wid": wid, "exp": int(time.time()) + 600 if exp is None else exp, "nonce": nonce}
    payload = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(key.encode(), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{b64(sig)}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    nonces = tmp_path / "state" / "hitl_used_nonces.json"
    monkeypatch.setenv("HITL_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("HITL_USED_NONCES_PATH", str(nonces))
    return nonces


class FakeServer:
    """Minimal stand-in for the MCP server: records the tool functions register_consultation_tools defines."""

    def __init__(self):
        self.executor = object()
        self.tools = {}

    def tool(self, name, description, input_schema):
        def decorator(fn):
            self.tools[name] = fn
            return fn
        return decorator


@pytest.fixture
def consultation(monkeypatch):
    """Loads the REAL consultation.py (relative imports and all) under a stub package."""
    pkg = types.ModuleType("fakehermes")
    pkg.__path__ = []
    sub = types.ModuleType("fakehermes.tools")
    sub.__path__ = []
    executor = types.ModuleType("fakehermes.executor")
    executor.VPSExecutor = object
    for name, mod in (("fakehermes", pkg), ("fakehermes.tools", sub), ("fakehermes.executor", executor)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("fakehermes.tools.consultation", ROOT / "consultation.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tools(consultation):
    server = FakeServer()
    consultation.register_consultation_tools(server)
    return server.tools


# --- Fakes for the Google API / local bridge: no network is ever touched ---------------------------------
class _Exec:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeTagManager:
    def __init__(self):
        self.calls = []

    def accounts(self):
        return self

    def containers(self):
        return self

    def workspaces(self):
        return self

    def versions(self):
        return self

    def create_version(self, path, body):
        self.calls.append(("create_version", path))
        return _Exec({"containerVersion": {"path": f"{path}/versions/9"}})

    def publish(self, path):
        self.calls.append(("publish", path))
        return _Exec({"containerVersion": {"containerVersionId": "9", "name": "v-test"}})


@pytest.fixture
def fake_google(monkeypatch, consultation, tmp_path):
    key_file = tmp_path / "gtm-service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(key_file))

    manager = FakeTagManager()
    google = types.ModuleType("google")
    google.__path__ = []
    oauth2 = types.ModuleType("google.oauth2")
    oauth2.__path__ = []
    service_account = types.ModuleType("google.oauth2.service_account")
    service_account.Credentials = types.SimpleNamespace(from_service_account_file=lambda path, scopes: object())
    oauth2.service_account = service_account
    google.oauth2 = oauth2
    gapi = types.ModuleType("googleapiclient")
    gapi.__path__ = []
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *args, **kwargs: manager
    gapi.discovery = discovery
    for name, mod in (("google", google), ("google.oauth2", oauth2), ("google.oauth2.service_account", service_account),
                      ("googleapiclient", gapi), ("googleapiclient.discovery", discovery)):
        monkeypatch.setitem(sys.modules, name, mod)
    return manager


@pytest.fixture
def fake_bridge(monkeypatch):
    requests = []

    class Resp:
        def read(self):
            return b'{"status": "bridge_ok"}'

    def fake_urlopen(req, timeout=None):
        requests.append(req.full_url)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return requests


def publish_args(token=None, **overrides):
    args = {"account_id": "6380333426", "container_id": CONTAINER, "workspace_id": WORKSPACE,
            "version_name": "v-test", "hitl_approval_token": token}
    args.update(overrides)
    return args


def assert_refused(raw: str):
    out = json.loads(raw)
    assert out["status"] == "AWAITING_HITL_APPROVAL"
    assert out["gate"] == "D-003"
    assert "POST /approvals/gtm-publish" in out["action_required"]
    return out


# =========================================================================================================
# Issuer / verifier
# =========================================================================================================
def test_issue_and_verify_roundtrip_is_single_use(env):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    assert token.count(".") == 1
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env) == (True, "ok")
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env) == (False, "nonce_reused_or_store_unavailable")
    stored = json.loads(env.read_text(encoding="utf-8"))
    assert len(stored) == 1


def test_issue_fails_closed_without_or_with_weak_key(monkeypatch):
    monkeypatch.delenv("HITL_SIGNING_KEY", raising=False)
    with pytest.raises(RuntimeError):
        hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    monkeypatch.setenv("HITL_SIGNING_KEY", "short")
    with pytest.raises(RuntimeError):
        hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)


@pytest.mark.parametrize("cid,wid,ttl", [("", "2", 60), ("GTM 1", "2", 60), (CONTAINER, "", 60),
                                         (CONTAINER, "../x", 60), (CONTAINER, "2", 0), (CONTAINER, "2", 10**9)])
def test_issue_rejects_bad_input(env, cid, wid, ttl):
    with pytest.raises(ValueError):
        hitl_tokens.issue_publish_token(cid, wid, ttl_seconds=ttl)


def test_verify_rejects_garbage_tokens(env):
    for token in (None, "", "   ", "abc", "a.b.c", "....", "é.é", forge().split(".")[0] + ".", ".sig"):
        ok, _ = hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env)
        assert ok is False


def test_verify_fails_closed_on_corrupt_nonce_store(env):
    env.parent.mkdir(parents=True)
    env.write_text("{not json", encoding="utf-8")
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env)[0] is False


def test_expired_nonces_are_pruned(env):
    env.parent.mkdir(parents=True)
    env.write_text(json.dumps({"old": 1, "fresh": int(time.time()) + 999}), encoding="utf-8")
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env)[0] is True
    stored = json.loads(env.read_text(encoding="utf-8"))
    assert "old" not in stored and "fresh" in stored and len(stored) == 2


def test_consultation_verifier_is_byte_for_byte_identical_to_hitl_tokens(consultation):
    for name in ("_signing_key", "_b64url_decode", "_consume_nonce", "verify_publish_token"):
        assert inspect.getsource(getattr(consultation, name)) == inspect.getsource(getattr(hitl_tokens, name)), name


def test_token_from_hitl_tokens_verifies_with_consultation_verifier(env, consultation):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    assert consultation.verify_publish_token(token, CONTAINER, WORKSPACE, env) == (True, "ok")
    assert consultation.verify_publish_token(token, CONTAINER, WORKSPACE, env)[0] is False   # single use


# =========================================================================================================
# The D-003 gate in consultation.py (both tools)
# =========================================================================================================
def call(tools, tool, token=None, **overrides):
    if tool == "hermes_gtm_cloud_publish":
        return tools[tool](**publish_args(token, **overrides))
    kwargs = {"container_id": CONTAINER, "version_name": "v-test", "hitl_approval_token": token}
    kwargs.update(overrides)
    return tools[tool](**kwargs)


def workspace_for(tool):
    return WORKSPACE if tool == "hermes_gtm_cloud_publish" else "browser"


BOTH = ["hermes_gtm_cloud_publish", "hermes_gtm_deploy"]


@pytest.mark.parametrize("tool", BOTH)
def test_no_token_is_refused_and_refusal_leaks_nothing(env, tools, fake_google, fake_bridge, tool):
    raw = call(tools, tool, token=None)
    out = assert_refused(raw)
    assert out["proposed_container_id"] == CONTAINER
    assert out["proposed_version"] == "v-test"
    assert out["approval_check"] == "token_missing"
    for forbidden in ("SUPERVISOR_APPROVED", "fallback_available", "local_bridge_tool"):
        assert forbidden not in raw
    assert "hitl_approval_token=" not in raw
    assert fake_google.calls == [] and fake_bridge == []


@pytest.mark.parametrize("tool", BOTH)
def test_old_magic_string_and_arbitrary_strings_are_refused(env, tools, fake_google, fake_bridge, tool):
    for token in ("SUPERVISOR_APPROVED", "yes", "approved.approved", "x" * 200):
        assert_refused(call(tools, tool, token=token))
    assert fake_google.calls == [] and fake_bridge == []


@pytest.mark.parametrize("tool", BOTH)
def test_forged_signature_is_refused(env, tools, fake_google, fake_bridge, tool):
    wrong_key = forge(wid=workspace_for(tool), key="a-different-signing-key-0123456789abcdef")
    assert assert_refused(call(tools, tool, token=wrong_key))["approval_check"] == "bad_signature"

    good = forge(wid=workspace_for(tool), nonce="n-tamper")
    payload, sig = good.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["exp"] += 100000
    tampered = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()) + "." + sig
    assert assert_refused(call(tools, tool, token=tampered))["approval_check"] == "bad_signature"
    assert fake_google.calls == [] and fake_bridge == []


@pytest.mark.parametrize("tool", BOTH)
def test_expired_token_is_refused(env, tools, fake_google, fake_bridge, tool):
    token = forge(wid=workspace_for(tool), exp=int(time.time()) - 5)
    assert assert_refused(call(tools, tool, token=token))["approval_check"] == "token_expired"
    assert fake_google.calls == [] and fake_bridge == []


@pytest.mark.parametrize("tool", BOTH)
def test_token_for_other_container_or_workspace_is_refused(env, tools, fake_google, fake_bridge, tool):
    other_container = hitl_tokens.issue_publish_token("GTM-OTHER123", workspace_for(tool))
    assert assert_refused(call(tools, tool, token=other_container))["approval_check"] == "container_mismatch"
    other_workspace = hitl_tokens.issue_publish_token(CONTAINER, "99")
    assert assert_refused(call(tools, tool, token=other_workspace))["approval_check"] == "workspace_mismatch"
    assert fake_google.calls == [] and fake_bridge == []


def test_cloud_token_cannot_be_replayed_on_the_bridge_tool(env, tools, fake_google, fake_bridge):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)          # workspace "2"
    assert_refused(call(tools, "hermes_gtm_deploy", token=token))          # bridge needs workspace "browser"
    assert fake_bridge == []


@pytest.mark.parametrize("tool", BOTH)
def test_missing_signing_key_refuses_even_a_well_formed_token(env, monkeypatch, tools, fake_google, fake_bridge, tool):
    token = hitl_tokens.issue_publish_token(CONTAINER, workspace_for(tool))
    monkeypatch.delenv("HITL_SIGNING_KEY")
    assert assert_refused(call(tools, tool, token=token))["approval_check"] == "signing_key_not_configured"
    assert fake_google.calls == [] and fake_bridge == []


def test_valid_token_proceeds_and_cannot_be_reused_cloud(env, tools, fake_google):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    out = json.loads(call(tools, "hermes_gtm_cloud_publish", token=token))
    assert out["status"] == "success"
    assert out["live_version_id"] == "9"
    assert fake_google.calls == [
        ("create_version", f"accounts/6380333426/containers/{CONTAINER}/workspaces/{WORKSPACE}"),
        ("publish", f"accounts/6380333426/containers/{CONTAINER}/workspaces/{WORKSPACE}/versions/9"),
    ]
    # Same token again: the nonce is spent, nothing more is published
    assert assert_refused(call(tools, "hermes_gtm_cloud_publish", token=token))["approval_check"] == "nonce_reused_or_store_unavailable"
    assert len(fake_google.calls) == 2


def test_valid_token_proceeds_and_cannot_be_reused_bridge(env, tools, fake_bridge):
    token = hitl_tokens.issue_publish_token(CONTAINER, "browser")
    assert json.loads(call(tools, "hermes_gtm_deploy", token=token)) == {"status": "bridge_ok"}
    assert fake_bridge == ["http://127.0.0.1:8765/gtm-deploy"]
    assert_refused(call(tools, "hermes_gtm_deploy", token=token))
    assert len(fake_bridge) == 1


def test_pending_credentials_no_longer_advertises_the_ungated_bridge(env, tools, consultation, monkeypatch, tmp_path):
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(tmp_path / "missing.json"))
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    raw = call(tools, "hermes_gtm_cloud_publish", token=token)
    assert json.loads(raw)["status"] == "pending_credentials"
    assert "fallback_available" not in raw and "local_bridge_tool" not in raw and "hermes_gtm_deploy" not in raw
    # Missing credentials must NOT burn the human-issued token
    assert not env.exists()
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env, consume=False) == (True, "ok")


def test_missing_credentials_not_revealed_to_unapproved_caller(env, tools, consultation, monkeypatch, tmp_path):
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(tmp_path / "missing.json"))
    assert_refused(call(tools, "hermes_gtm_cloud_publish", token=None))


def test_credentials_restored_then_same_token_publishes(env, tools, consultation, fake_google, monkeypatch, tmp_path):
    good_path = consultation.GTM_SERVICE_ACCOUNT_PATH
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(tmp_path / "missing.json"))
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    assert json.loads(call(tools, "hermes_gtm_cloud_publish", token=token))["status"] == "pending_credentials"
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", good_path)
    assert json.loads(call(tools, "hermes_gtm_cloud_publish", token=token))["status"] == "success"


# =========================================================================================================
# Issuance endpoint: POST /approvals/gtm-publish (human operator only)
# =========================================================================================================
@pytest.fixture
def operator_env(env, monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    monkeypatch.setattr(settings, "HERMES_API_KEY", "the-hermes-agent-key")
    return env


async def post_approval(body=None, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/approvals/gtm-publish",
                                 json=body or {"container_id": CONTAINER, "workspace_id": WORKSPACE},
                                 headers=headers or {})


@pytest.mark.asyncio
async def test_approvals_requires_operator_key(operator_env):
    assert (await post_approval()).status_code == 401
    assert (await post_approval(headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await post_approval(headers={"Authorization": OPERATOR_KEY})).status_code == 401       # not a Bearer header
    # The Hermes agent/webhook key must NOT work as operator key
    assert (await post_approval(headers={"Authorization": "Bearer the-hermes-agent-key"})).status_code == 401
    # Auth runs before body validation: an unauthenticated caller learns nothing about the schema
    assert (await post_approval(body={"container_id": "bad id"})).status_code == 401


@pytest.mark.asyncio
async def test_approvals_ok_with_operator_key_and_token_verifies(operator_env):
    res = await post_approval(headers={"Authorization": f"Bearer {OPERATOR_KEY}"})
    assert res.status_code == 200
    data = res.json()
    assert data["gate"] == "D-003" and data["single_use"] is True
    assert data["container_id"] == CONTAINER and data["workspace_id"] == WORKSPACE
    assert hitl_tokens.verify_publish_token(data["approval_token"], CONTAINER, WORKSPACE, operator_env) == (True, "ok")


@pytest.mark.asyncio
async def test_approvals_fail_closed_when_operator_key_unset_or_shared(operator_env, monkeypatch):
    monkeypatch.delenv("OPERATOR_API_KEY")
    assert (await post_approval(headers={"Authorization": "Bearer "})).status_code == 401
    assert (await post_approval(headers={"Authorization": f"Bearer {OPERATOR_KEY}"})).status_code == 401
    monkeypatch.setenv("OPERATOR_API_KEY", "the-hermes-agent-key")                                # same as agent key
    assert (await post_approval(headers={"Authorization": "Bearer the-hermes-agent-key"})).status_code == 401


@pytest.mark.asyncio
async def test_approvals_validation_and_missing_signing_key(operator_env, monkeypatch):
    headers = {"Authorization": f"Bearer {OPERATOR_KEY}"}
    assert (await post_approval(body={"container_id": "bad id", "workspace_id": "2"}, headers=headers)).status_code == 422
    assert (await post_approval(body={"container_id": CONTAINER}, headers=headers)).status_code == 422
    monkeypatch.delenv("HITL_SIGNING_KEY")
    res = await post_approval(headers=headers)
    assert res.status_code == 503
    assert "approval_token" not in res.text
