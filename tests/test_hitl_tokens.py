"""
test_hitl_tokens.py — Rule D-003 Ed25519-signed, bound, short-lived, single-use approval tokens.

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
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.ameen_workforce import hitl_tokens
from src.ameen_workforce.config import settings
from src.ameen_workforce.service import app

ROOT = Path(__file__).resolve().parents[1]
OPERATOR_KEY = "unit-test-operator-key"
CONTAINER = "GTM-5C5N552P"
WORKSPACE = "2"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pem_private(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def public_b64(key: Ed25519PrivateKey) -> str:
    return b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


ISSUER_KEY = {}


def forge(cid=CONTAINER, wid=WORKSPACE, exp=None, nonce="n-forged-1", key=None) -> str:
    """Builds a token the same way the issuer does, with arbitrary claims/signing key (for negative tests).
    Default key is the test issuer key (the one whose public half the verifier trusts)."""
    key = key or ISSUER_KEY["key"]
    claims = {"cid": cid, "wid": wid, "exp": int(time.time()) + 600 if exp is None else exp, "nonce": nonce}
    payload = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{b64(key.sign(payload.encode('ascii')))}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Issuer side: private key PEM (0600) via HITL_SIGNING_PRIVATE_KEY_PATH. Verifier side: public key only
    via HITL_VERIFY_PUBLIC_KEY. (One process plays both roles in tests; real hosts hold one half each.)"""
    key = Ed25519PrivateKey.generate()
    ISSUER_KEY["key"] = key
    key_file = tmp_path / "keys" / "hitl_private.pem"
    key_file.parent.mkdir()
    key_file.write_bytes(pem_private(key))
    if os.name == "posix":
        key_file.chmod(0o600)
    nonces = tmp_path / "state" / "hitl_used_nonces.json"
    monkeypatch.delenv("HITL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY_PATH", raising=False)
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY", public_b64(key))
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
    spec = importlib.util.spec_from_file_location("fakehermes.tools.consultation", ROOT / "ops" / "hermes" / "consultation.py")
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


def test_issue_fails_closed_without_private_key(env, monkeypatch, tmp_path):
    monkeypatch.delenv("HITL_SIGNING_PRIVATE_KEY_PATH")
    with pytest.raises(RuntimeError):
        hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
    with pytest.raises(RuntimeError):
        hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a pem", encoding="utf-8")
    if os.name == "posix":
        garbage.chmod(0o600)
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(garbage))
    with pytest.raises(RuntimeError):
        hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)


def test_issue_refuses_non_ed25519_private_key(env, monkeypatch, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path = tmp_path / "rsa.pem"
    path.write_bytes(rsa_pem)
    if os.name == "posix":
        path.chmod(0o600)
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(path))
    with pytest.raises(RuntimeError):
        hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_issue_refuses_group_or_world_readable_private_key(env):
    path = Path(os.environ["HITL_SIGNING_PRIVATE_KEY_PATH"])
    for mode in (0o640, 0o604, 0o644):
        path.chmod(mode)
        with pytest.raises(RuntimeError):
            hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    path.chmod(0o600)
    assert hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)


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
    for name in ("_verify_key", "_b64url_decode", "_consume_nonce", "verify_publish_token"):
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
    other_issuer = forge(wid=workspace_for(tool), key=Ed25519PrivateKey.generate(), nonce="n-other-key")
    assert assert_refused(call(tools, tool, token=other_issuer))["approval_check"] == "bad_signature"

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
def test_missing_public_key_refuses_even_a_well_formed_token(env, monkeypatch, tools, fake_google, fake_bridge, tool):
    token = hitl_tokens.issue_publish_token(CONTAINER, workspace_for(tool))
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY")
    assert assert_refused(call(tools, tool, token=token))["approval_check"] == "verify_key_not_configured"
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY", "not-a-key")
    assert assert_refused(call(tools, tool, token=token))["approval_check"] == "verify_key_not_configured"
    assert fake_google.calls == [] and fake_bridge == []


@pytest.mark.parametrize("tool", BOTH)
def test_old_hmac_format_token_is_refused(env, tools, fake_google, fake_bridge, monkeypatch, tool):
    """HMAC support is gone: even a correctly HMAC-signed token (and a set HITL_SIGNING_KEY) is refused."""
    secret = "legacy-hmac-signing-key-0123456789abcdef"
    monkeypatch.setenv("HITL_SIGNING_KEY", secret)
    claims = {"cid": CONTAINER, "wid": workspace_for(tool), "exp": int(time.time()) + 600, "nonce": "n-hmac"}
    payload = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    legacy = f"{payload}.{b64(hmac.new(secret.encode(), payload.encode('ascii'), hashlib.sha256).digest())}"
    assert assert_refused(call(tools, tool, token=legacy))["approval_check"] == "bad_signature"
    assert hitl_tokens.verify_publish_token(legacy, CONTAINER, workspace_for(tool), os.environ["HITL_USED_NONCES_PATH"])[0] is False
    assert fake_google.calls == [] and fake_bridge == []


def test_verifier_works_with_only_the_public_key_in_the_environment(env, monkeypatch, consultation):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    for name in list(os.environ):
        if "PRIVATE" in name or name == "HITL_SIGNING_KEY":
            monkeypatch.delenv(name)
    assert not any("PRIVATE" in name for name in os.environ)
    assert consultation.verify_publish_token(token, CONTAINER, WORKSPACE, env) == (True, "ok")


def test_verifier_accepts_public_key_from_pem_path(env, monkeypatch, tmp_path):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    pub_pem = ISSUER_KEY["key"].public_key().public_bytes(serialization.Encoding.PEM,
                                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    pub_file = tmp_path / "hitl_public.pem"
    pub_file.write_bytes(pub_pem)
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY")
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY_PATH", str(pub_file))
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env) == (True, "ok")


def test_verifier_never_reads_the_private_key_file(env, monkeypatch):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    private_path = os.environ["HITL_SIGNING_PRIVATE_KEY_PATH"]
    real_open = open

    def guarded_open(file, *args, **kwargs):
        assert str(file) != private_path, "verifier touched the private key"
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, env) == (True, "ok")


def test_generate_keypair_cli_writes_0600_private_key_and_prints_only_public(env, monkeypatch, tmp_path):
    out = tmp_path / "cli_keys"
    root = Path(__file__).resolve().parents[1]
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("HITL_")}
    run_env["PYTHONPATH"] = str(root / "src")
    cmd = [sys.executable, "-m", "ameen_workforce.hitl_tokens", "generate-keypair", "--out", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=run_env, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    private_file = out / "hitl_private.pem"
    assert private_file.exists() and (out / "hitl_public.pem").exists()
    if os.name == "posix":
        assert (private_file.stat().st_mode & 0o777) == 0o600
    private_key = serialization.load_pem_private_key(private_file.read_bytes(), password=None)
    printed = proc.stdout.strip()
    assert printed == public_b64(private_key)
    assert len(base64.urlsafe_b64decode(printed + "=" * (-len(printed) % 4))) == 32
    # Nothing that looks like private material reaches stdout or stderr
    pem_body = "".join(private_file.read_text(encoding="ascii").splitlines()[1:-1])
    raw_private = b64(private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                serialization.NoEncryption()))
    for stream in (proc.stdout, proc.stderr):
        assert "PRIVATE KEY" not in stream and pem_body not in stream and raw_private not in stream
    # A token issued with the generated key verifies with ONLY the printed public key
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(private_file))
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    monkeypatch.delenv("HITL_SIGNING_PRIVATE_KEY_PATH")
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY", printed)
    assert hitl_tokens.verify_publish_token(token, CONTAINER, WORKSPACE, tmp_path / "n.json") == (True, "ok")
    # Refuses to overwrite an existing key
    again = subprocess.run(cmd, capture_output=True, text=True, env=run_env, cwd=str(root))
    assert again.returncode != 0 and "PRIVATE KEY" not in again.stdout


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
async def test_approvals_validation_and_missing_private_key(operator_env, monkeypatch):
    headers = {"Authorization": f"Bearer {OPERATOR_KEY}"}
    assert (await post_approval(body={"container_id": "bad id", "workspace_id": "2"}, headers=headers)).status_code == 422
    assert (await post_approval(body={"container_id": CONTAINER}, headers=headers)).status_code == 422
    monkeypatch.delenv("HITL_SIGNING_PRIVATE_KEY_PATH")
    res = await post_approval(headers=headers)
    assert res.status_code == 503
    assert "approval_token" not in res.text
