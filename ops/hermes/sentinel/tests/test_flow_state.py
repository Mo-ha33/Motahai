"""Tests for the sentinel flow-state writer (C1) and its hook in ops/hermes/consultation.py.

Run: python -m pytest -q ops/hermes/sentinel
"""

import importlib.util
import json
import logging
import os
import re
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


SENTINEL_DIR = Path(__file__).resolve().parents[1]
HERMES_DIR = SENTINEL_DIR.parent
ROOT = HERMES_DIR.parents[1]
sys.path.insert(0, str(SENTINEL_DIR))
import flow_state as F  # noqa: E402

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
DEADLINE = "2026-10-11T12:05:00Z"
GTM = "GTM-ABC1234"


@pytest.fixture(autouse=True)
def _isolate_sentinel_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SENTINEL_DIR", str(tmp_path / "sentinel"))
    monkeypatch.delenv("SENTINEL_FLOW_DEADLINE_S", raising=False)
    monkeypatch.setenv("GTM_CACHE_DIR", str(tmp_path / "gtm-cache"))


def _flow_file(tmp_path, container=GTM):
    return tmp_path / "sentinel" / "flow" / f"{container}.json"


def _read(tmp_path, container=GTM):
    return json.loads(_flow_file(tmp_path, container).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------
# Mapping table (step, outcome) -> owed_by
# ---------------------------------------------------------------------------------------------------------
FULL_TABLE = [
    # verify_access
    ("verify_access", "CONNECTED", "agent"),
    ("verify_access", "RETRYABLE", "agent"),
    ("verify_access", "RETRYABLE_QUOTA", "operator"),
    ("verify_access", "NOT_INVITED", "client"),
    ("verify_access", "PERMISSION_MISSING", "client"),
    ("verify_access", "CONTAINER_NOT_FOUND", "client"),
    ("verify_access", "INVALID_ID", "client"),
    ("verify_access", "WORKSPACE_LIMIT", "client"),
    ("verify_access", "CREDENTIALS_PENDING", "operator"),
    ("verify_access", "API_DISABLED", "operator"),
    ("verify_access", "SCOPE_INSUFFICIENT", "operator"),
    ("verify_access", "UNKNOWN", "operator"),
    # workspace_apply
    ("workspace_apply", "applied", "agent"),
    ("workspace_apply", "LINT_FAILED", "agent"),
    ("workspace_apply", "RETRYABLE", "agent"),
    ("workspace_apply", "RETRYABLE_QUOTA", "operator"),
    ("workspace_apply", "NOT_INVITED", "client"),
    ("workspace_apply", "PERMISSION_MISSING", "client"),
    ("workspace_apply", "CONTAINER_NOT_FOUND", "client"),
    ("workspace_apply", "INVALID_ID", "client"),
    ("workspace_apply", "WORKSPACE_LIMIT", "client"),
    ("workspace_apply", "INVALID_SPEC", "operator"),
    ("workspace_apply", "LINTER_UNAVAILABLE", "operator"),
    ("workspace_apply", "CREDENTIALS_PENDING", "operator"),
    ("workspace_apply", "API_DISABLED", "operator"),
    ("workspace_apply", "SCOPE_INSUFFICIENT", "operator"),
    ("workspace_apply", "UNKNOWN", "operator"),
    # cloud_publish
    ("cloud_publish", "published", "none"),
    ("cloud_publish", "AWAITING_HITL_APPROVAL", "human"),
    ("cloud_publish", "HITL_KEY_NOT_CONFIGURED", "operator"),
    ("cloud_publish", "RETRYABLE", "operator"),
    ("cloud_publish", "RETRYABLE_QUOTA", "operator"),
    ("cloud_publish", "PERMISSION_MISSING", "client"),
    ("cloud_publish", "CONTAINER_NOT_FOUND", "client"),
    ("cloud_publish", "CREDENTIALS_PENDING", "operator"),
    ("cloud_publish", "API_DISABLED", "operator"),
    ("cloud_publish", "SCOPE_INSUFFICIENT", "operator"),
    ("cloud_publish", "MERGE_CONFLICT", "operator"),
    ("cloud_publish", "COMPILER_ERROR", "operator"),
    ("cloud_publish", "UNKNOWN", "operator"),
]


@pytest.mark.parametrize("step,outcome,expected", FULL_TABLE)
def test_owed_by_table(step, outcome, expected):
    assert F.owed_by(step, outcome) == expected


@pytest.mark.parametrize("step,outcome,expected", FULL_TABLE)
def test_record_writes_owed_by_and_deadline_only_for_agent(tmp_path, step, outcome, expected):
    F.record(GTM, step, outcome, now=NOW)
    data = _read(tmp_path)
    assert data["owed_by"] == expected
    assert data["deadline"] == (DEADLINE if expected == "agent" else None)


@pytest.mark.parametrize("step,outcome", [
    ("verify_access", "SOMETHING_NEW"),
    ("workspace_apply", "SOMETHING_NEW"),
    ("cloud_publish", ""),
    ("unknown_step", "CONNECTED"),
    ("cloud_publish", None),
])
def test_unknown_outcome_or_step_is_operator_never_agent(step, outcome):
    assert F.owed_by(step, outcome) == "operator"


def test_every_reason_code_in_gtm_sources_is_reviewed():
    """Every reason code the GTM tools can emit must be explicitly listed in flow_state (no silent default)."""
    sources = [HERMES_DIR / "gtm_api.py", HERMES_DIR / "consultation.py"]
    pattern = re.compile(r'(?:error_response\(\s*|"reason_code":\s*)"([A-Z_]+)"')
    found = set()
    for path in sources:
        found |= set(pattern.findall(path.read_text(encoding="utf-8")))
    found |= {"RETRYABLE_QUOTA", "HITL_KEY_NOT_CONFIGURED", "AWAITING_HITL_APPROVAL"}  # derived by outcome_of
    reviewed = {code for (_step, code) in F._EXPLICIT} | F._CLIENT_CODES | F.OPERATOR_CODES
    assert found, "no reason codes found: the scan pattern is broken"
    assert found <= reviewed, f"unreviewed reason codes: {sorted(found - reviewed)}"


def test_every_gtm_reason_code_maps_to_a_known_owner():
    codes = {"CONNECTED", "applied", "published", "NOT_INVITED", "PERMISSION_MISSING", "CONTAINER_NOT_FOUND",
             "INVALID_ID", "WORKSPACE_LIMIT", "RETRYABLE", "RETRYABLE_QUOTA", "LINT_FAILED", "INVALID_SPEC",
             "LINTER_UNAVAILABLE", "CREDENTIALS_PENDING", "API_DISABLED", "SCOPE_INSUFFICIENT", "MERGE_CONFLICT",
             "COMPILER_ERROR", "UNKNOWN", "AWAITING_HITL_APPROVAL", "HITL_KEY_NOT_CONFIGURED"}
    for code in codes:
        for step in ("verify_access", "workspace_apply", "cloud_publish"):
            assert F.owed_by(step, code) in {"agent", "client", "operator", "human", "none"}


# ---------------------------------------------------------------------------------------------------------
# Deadline, seq and stored fields
# ---------------------------------------------------------------------------------------------------------
def test_deadline_default_is_300_seconds(tmp_path):
    F.record(GTM, "verify_access", "CONNECTED", now=NOW)
    assert _read(tmp_path)["deadline"] == DEADLINE


def test_deadline_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_FLOW_DEADLINE_S", "60")
    F.record(GTM, "workspace_apply", "applied", now=NOW)
    assert _read(tmp_path)["deadline"] == "2026-10-11T12:01:00Z"


@pytest.mark.parametrize("value", ["garbage", "-5", ""])
def test_deadline_bad_env_falls_back_to_300(tmp_path, monkeypatch, value):
    monkeypatch.setenv("SENTINEL_FLOW_DEADLINE_S", value)
    F.record(GTM, "verify_access", "CONNECTED", now=NOW)
    assert _read(tmp_path)["deadline"] == DEADLINE


def test_seq_increments_per_container(tmp_path):
    seqs = [F.record(GTM, "verify_access", "CONNECTED", now=NOW)["seq"] for _ in range(3)]
    other = F.record("GTM-ZZZZ999", "verify_access", "CONNECTED", now=NOW)["seq"]
    assert seqs == [1, 2, 3]
    assert other == 1
    assert _read(tmp_path)["seq"] == 3


def test_seq_restarts_safely_when_file_is_corrupt(tmp_path):
    _flow_file(tmp_path).parent.mkdir(parents=True)
    _flow_file(tmp_path).write_text("{not json", encoding="utf-8")
    assert F.record(GTM, "verify_access", "CONNECTED", now=NOW)["seq"] == 1


def test_stored_fields_are_only_the_contract(tmp_path):
    F.record(GTM, "cloud_publish", "published", now=NOW)
    assert set(_read(tmp_path)) == {"container", "step", "outcome", "owed_by", "deadline", "updated_at", "seq"}
    assert _read(tmp_path)["updated_at"] == "2026-10-11T12:00:00Z"


def test_naive_now_is_treated_as_utc(tmp_path):
    F.record(GTM, "verify_access", "CONNECTED", now=datetime(2026, 10, 11, 12, 0, 0))
    assert _read(tmp_path)["deadline"] == DEADLINE


# ---------------------------------------------------------------------------------------------------------
# Container validation, atomic write, permissions, failure handling
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("container", ["gtm-abc1234", "GTM-../x", "", None, 123, "GTM-AB", "GTM-5C5N552P/../../x"])
def test_invalid_container_is_rejected_without_writing(tmp_path, container):
    assert F.record(container, "verify_access", "CONNECTED", now=NOW) is None
    assert not (tmp_path / "sentinel").exists()


def test_atomic_write_keeps_previous_file_when_replace_fails(tmp_path, monkeypatch, caplog):
    F.record(GTM, "verify_access", "CONNECTED", now=NOW)
    before = _flow_file(tmp_path).read_text(encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(F.os, "replace", failing_replace)
    with caplog.at_level(logging.WARNING, logger="hermes.sentinel.flow_state"):
        assert F.record(GTM, "workspace_apply", "applied", now=NOW) is None
    assert _flow_file(tmp_path).read_text(encoding="utf-8") == before
    assert not list(_flow_file(tmp_path).parent.glob("*.tmp"))
    assert any("record failed" in r.getMessage() for r in caplog.records)


def test_io_failure_is_swallowed_and_logged(tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    os.environ["HERMES_SENTINEL_DIR"] = str(blocker)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes.sentinel.flow_state"):
            assert F.record(GTM, "verify_access", "CONNECTED", now=NOW) is None
            assert F.record_result(GTM, "verify_access", {"status": "CONNECTED"}) is None
    finally:
        os.environ["HERMES_SENTINEL_DIR"] = str(tmp_path / "sentinel")
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_bad_now_value_is_swallowed():
    assert F.record(GTM, "verify_access", "CONNECTED", now="yesterday") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_file_is_0600_and_directories_are_0700(tmp_path):
    F.record(GTM, "verify_access", "CONNECTED", now=NOW)
    assert oct(_flow_file(tmp_path).stat().st_mode & 0o777) == "0o600"
    assert oct(_flow_file(tmp_path).parent.stat().st_mode & 0o777) == "0o700"


def test_writes_leave_no_tmp_files(tmp_path):
    for _ in range(3):
        F.record(GTM, "verify_access", "CONNECTED", now=NOW)
    assert [p.name for p in _flow_file(tmp_path).parent.iterdir()] == [f"{GTM}.json"]


# ---------------------------------------------------------------------------------------------------------
# outcome_of: deriving the outcome code from a tool result
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("payload,expected", [
    ({"status": "CONNECTED", "public_id": GTM}, "CONNECTED"),
    ({"status": "CONNECTED", "cache": "stale", "stale_reason": "RETRYABLE"}, "CONNECTED"),
    ({"status": "applied", "created": {}}, "applied"),
    ({"status": "success", "live_version_id": "9"}, "published"),
    ({"status": "error", "reason_code": "NOT_INVITED", "audience": "client"}, "NOT_INVITED"),
    ({"status": "error", "reason_code": "RETRYABLE", "quota_exhausted": True}, "RETRYABLE_QUOTA"),
    ({"status": "error", "reason_code": "RETRYABLE", "retryable": True}, "RETRYABLE"),
    ({"status": "pending_credentials", "reason_code": "CREDENTIALS_PENDING"}, "CREDENTIALS_PENDING"),
    ({"status": "AWAITING_HITL_APPROVAL", "approval_check": "token_missing"}, "AWAITING_HITL_APPROVAL"),
    ({"status": "AWAITING_HITL_APPROVAL", "approval_check": "verify_key_not_configured"}, "HITL_KEY_NOT_CONFIGURED"),
    ({"status": "weird"}, "UNKNOWN"),
    ([], "UNKNOWN"),
    ("not json", "UNKNOWN"),
    (None, "UNKNOWN"),
])
def test_outcome_of(payload, expected):
    assert F.outcome_of(payload) == expected
    if isinstance(payload, dict):
        assert F.outcome_of(json.dumps(payload)) == expected


def test_record_result_records_the_derived_outcome(tmp_path):
    F.record_result(GTM, "cloud_publish", json.dumps({"status": "success", "live_version_id": "9"}))
    data = _read(tmp_path)
    assert (data["outcome"], data["owed_by"], data["deadline"]) == ("published", "none", None)


# ---------------------------------------------------------------------------------------------------------
# Hook in consultation.py
# ---------------------------------------------------------------------------------------------------------
class _RecordingServer:
    def __init__(self):
        self.executor = object()
        self.tools = {}

    def tool(self, name, description, input_schema):
        def decorator(fn):
            self.tools[name] = fn
            return fn
        return decorator


def _load_into(monkeypatch, name, path):
    """Loads a file as a module registered in sys.modules for this test only (monkeypatch restores it)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def consultation(monkeypatch, tmp_path):
    pkg = types.ModuleType("fakehermes_sentinel")
    pkg.__path__ = []
    sub = types.ModuleType("fakehermes_sentinel.tools")
    sub.__path__ = []
    executor = types.ModuleType("fakehermes_sentinel.executor")
    executor.VPSExecutor = object
    for name, mod in (("fakehermes_sentinel", pkg), ("fakehermes_sentinel.tools", sub),
                      ("fakehermes_sentinel.executor", executor)):
        monkeypatch.setitem(sys.modules, name, mod)
    # Pre-load flow_state under the name consultation's file-path fallback uses, so tests can patch it.
    _load_into(monkeypatch, "hermes_sentinel_flow_state", SENTINEL_DIR / "flow_state.py")
    module = _load_into(monkeypatch, "fakehermes_sentinel.tools.consultation", HERMES_DIR / "consultation.py")
    key_file = tmp_path / "gtm-service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "GTM_SERVICE_ACCOUNT_PATH", str(key_file))
    monkeypatch.setenv("HITL_USED_NONCES_PATH", str(tmp_path / "state" / "nonces.json"))
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY_PATH", raising=False)
    return module


@pytest.fixture
def tools(consultation):
    srv = _RecordingServer()
    consultation.register_consultation_tools(srv)
    return srv.tools


@pytest.fixture
def verify_key(monkeypatch):
    """A real Ed25519 public key so an absent token yields AWAITING_HITL_APPROVAL (human), not a config error."""
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pub = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY", base64.urlsafe_b64encode(pub).rstrip(b"=").decode())


def _write_resolve_cache(tmp_path, public_id, numeric):
    cache = tmp_path / "gtm-cache"
    cache.mkdir(exist_ok=True)
    (cache / f"{public_id}.json").write_text(json.dumps(
        {"ts": time.time(), "result": {"status": "CONNECTED", "public_id": public_id, "container_id": numeric}}),
        encoding="utf-8")


def _patch_verify_ok(monkeypatch, consultation):
    api = consultation._gtm_api()
    monkeypatch.setattr(consultation, "_gtm_service", lambda scopes: (object(), None))
    monkeypatch.setattr(api, "verify_access", lambda svc, cid: {
        "status": "CONNECTED", "public_id": cid, "account_id": "111", "container_id": "555",
        "container_name": "x", "usage_context": [], "default_workspace_id": "1", "tariq_workspace_id": None})


def test_verify_hook_records_connected_with_agent_deadline(tmp_path, monkeypatch, consultation, tools):
    _patch_verify_ok(monkeypatch, consultation)
    out = json.loads(tools["hermes_gtm_verify_access"](public_container_id=GTM))
    assert out["status"] == "CONNECTED"
    data = _read(tmp_path)
    assert (data["step"], data["outcome"], data["owed_by"]) == ("verify_access", "CONNECTED", "agent")
    assert data["deadline"] is not None
    assert set(data) == {"container", "step", "outcome", "owed_by", "deadline", "updated_at", "seq"}


def test_verify_hook_records_invalid_id_as_client(tmp_path, consultation, tools):
    out = json.loads(tools["hermes_gtm_verify_access"](public_container_id="bad id"))
    assert out["reason_code"] == "INVALID_ID"
    assert not _flow_file(tmp_path, "bad id").exists()  # not a public GTM-ID, nothing is written


def test_verify_hook_records_missing_credentials_as_operator(tmp_path, consultation, tools, monkeypatch):
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(tmp_path / "missing.json"))
    out = json.loads(tools["hermes_gtm_verify_access"](public_container_id=GTM))
    assert out["reason_code"] == "CREDENTIALS_PENDING"
    data = _read(tmp_path)
    assert (data["outcome"], data["owed_by"], data["deadline"]) == ("CREDENTIALS_PENDING", "operator", None)


def test_apply_hook_records_applied_and_lint_failed(tmp_path, monkeypatch, consultation, tools):
    monkeypatch.setattr(consultation, "_gtm_service", lambda scopes: (object(), None))
    api = consultation._gtm_api()
    monkeypatch.setattr(api, "apply_container_spec", lambda svc, cid, spec: {"status": "applied", "workspace_id": "7"})
    tools["hermes_gtm_workspace_apply"](public_container_id=GTM, container_spec="{}")
    assert (_read(tmp_path)["outcome"], _read(tmp_path)["owed_by"]) == ("applied", "agent")

    monkeypatch.setattr(api, "apply_container_spec", lambda svc, cid, spec: {
        "status": "error", "reason_code": "LINT_FAILED", "audience": "operator", "retryable": False})
    tools["hermes_gtm_workspace_apply"](public_container_id=GTM, container_spec="{}")
    data = _read(tmp_path)
    assert (data["outcome"], data["owed_by"], data["seq"]) == ("LINT_FAILED", "agent", 2)


def test_publish_refusal_records_human_when_container_is_cached(tmp_path, consultation, tools, verify_key):
    _write_resolve_cache(tmp_path, GTM, "555")
    out = tools["hermes_gtm_cloud_publish"](account_id="111", container_id="555", workspace_id="2")
    assert json.loads(out)["status"] == "AWAITING_HITL_APPROVAL"
    data = _read(tmp_path)
    assert (data["step"], data["outcome"], data["owed_by"], data["deadline"]) == (
        "cloud_publish", "AWAITING_HITL_APPROVAL", "human", None)


def test_publish_without_key_config_records_operator(tmp_path, consultation, tools, monkeypatch):
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY", raising=False)
    _write_resolve_cache(tmp_path, GTM, "555")
    out = tools["hermes_gtm_cloud_publish"](account_id="111", container_id="555", workspace_id="2")
    assert json.loads(out)["status"] == "AWAITING_HITL_APPROVAL"
    assert (_read(tmp_path)["outcome"], _read(tmp_path)["owed_by"]) == ("HITL_KEY_NOT_CONFIGURED", "operator")


def test_publish_success_records_none(tmp_path, monkeypatch, consultation, tools):
    _write_resolve_cache(tmp_path, GTM, "555")
    monkeypatch.setattr(consultation, "_hitl_gate", lambda *a, **k: None)
    monkeypatch.setattr(consultation, "_gtm_service", lambda scopes: (object(), None))
    monkeypatch.setattr(consultation._gtm_api(), "create_and_publish_version", lambda *a, **k: {
        "status": "success", "account_id": "111", "container_id": "555", "live_version_id": "9"})
    tools["hermes_gtm_cloud_publish"](account_id="111", container_id="555", workspace_id="2")
    data = _read(tmp_path)
    assert (data["outcome"], data["owed_by"], data["deadline"]) == ("published", "none", None)


def test_publish_without_cached_public_id_writes_nothing(tmp_path, consultation, tools, verify_key):
    out = tools["hermes_gtm_cloud_publish"](account_id="111", container_id="999", workspace_id="2")
    assert json.loads(out)["status"] == "AWAITING_HITL_APPROVAL"
    assert not (tmp_path / "sentinel" / "flow").exists()


def _run_all_tools(monkeypatch, consultation, tools, verify_key_env=True):
    """Calls the three GTM tools with deterministic fakes; returns their raw return values."""
    api = consultation._gtm_api()
    monkeypatch.setattr(consultation, "_gtm_service", lambda scopes: (object(), None))
    monkeypatch.setattr(api, "verify_access", lambda svc, cid: {"status": "CONNECTED", "public_id": cid})
    monkeypatch.setattr(api, "apply_container_spec", lambda svc, cid, spec: {"status": "applied"})
    monkeypatch.setattr(api, "create_and_publish_version", lambda *a, **k: {"status": "success"})
    monkeypatch.setattr(consultation, "_hitl_gate", lambda *a, **k: None)
    return [
        tools["hermes_gtm_verify_access"](public_container_id=GTM),
        tools["hermes_gtm_verify_access"](public_container_id="nope"),
        tools["hermes_gtm_workspace_apply"](public_container_id=GTM, container_spec="{}"),
        tools["hermes_gtm_cloud_publish"](account_id="111", container_id="555", workspace_id="2"),
    ]


def test_hook_does_not_change_return_values_when_record_raises(tmp_path, monkeypatch, consultation, tools):
    _write_resolve_cache(tmp_path, GTM, "555")
    baseline = _run_all_tools(monkeypatch, consultation, tools)
    flow = sys.modules["hermes_sentinel_flow_state"]

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(flow, "record", boom)
    assert _run_all_tools(monkeypatch, consultation, tools) == baseline

    monkeypatch.setattr(flow, "record_result", boom)
    assert _run_all_tools(monkeypatch, consultation, tools) == baseline


def test_hook_does_not_change_return_values_when_loader_fails(tmp_path, monkeypatch, consultation, tools):
    _write_resolve_cache(tmp_path, GTM, "555")
    baseline = _run_all_tools(monkeypatch, consultation, tools)

    def no_loader(*a, **k):
        raise ImportError("sentinel package unavailable")

    monkeypatch.delitem(sys.modules, "hermes_sentinel_flow_state")
    monkeypatch.setattr(importlib.util, "spec_from_file_location", no_loader)
    assert _run_all_tools(monkeypatch, consultation, tools) == baseline
