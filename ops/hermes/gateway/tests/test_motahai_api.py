"""Unit tests for ops/hermes/motahai_api.py and consultation.py tool wrappers (G1 / S3-2)."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[4]
MOTAHAI_API_PATH = ROOT / "ops" / "hermes" / "motahai_api.py"
CONSULTATION_PATH = ROOT / "ops" / "hermes" / "consultation.py"


def _load_motahai_api():
    spec = importlib.util.spec_from_file_location("hermes_motahai_api_test", MOTAHAI_API_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


motahai = _load_motahai_api()

API_KEY = "super-secret-tenant-api-key-9999"
APPROVAL_TOKEN = "eyJhY3QiOiJwcm9tb3RlX2xpdmUifQ.secretSignatureValue12345"


class FakeHttp:
    def __init__(self, status: int = 200, body: Any = None, exc: Optional[Exception] = None):
        self.status = status
        if isinstance(body, (dict, list)):
            self.body_text = json.dumps(body)
        elif isinstance(body, str):
            self.body_text = body
        else:
            self.body_text = "{}"
        self.exc = exc
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body_bytes: Optional[bytes],
        timeout_s: float,
    ) -> Tuple[int, str]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": json.loads(body_bytes.decode("utf-8")) if body_bytes else None,
                "timeout_s": timeout_s,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.status, self.body_text


@pytest.fixture
def core_env(monkeypatch):
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", API_KEY)
    monkeypatch.setenv("MOTAHAI_CORE_URL", "http://core.test:8080/")


def test_module_docstring_records_known_pilot_risk():
    doc = motahai.__doc__ or ""
    assert "KNOWN RISK" in doc
    assert "tenant_key" in doc and "Wesam conversation" in doc


@pytest.mark.parametrize(
    "valid_key",
    [
        "epxsmk-y0.myshopify.com",
        "shop.example.co.uk",
        "abc",
        "1234567890",
        "1",
        "9" * 20,
    ],
)
def test_validate_tenant_key_accepts_valid_domains_and_merchant_ids(valid_key):
    assert motahai.validate_tenant_key(valid_key) is None


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        None,
        12345,
        "Shop.MyShopify.com",
        "ab",
        "../admin",
        "shop..myshopify.com",
        ".shop.com",
        "shop.com.",
        "shop.myshopify.com/status",
        "shop myshopify com",
        "9" * 21,
        "a" * 256,
    ],
)
def test_validate_tenant_key_rejects_bad_inputs_without_http_call(core_env, bad_key):
    http = FakeHttp()
    out1 = motahai.tenant_status(bad_key, http_call=http)
    out2 = motahai.signal_health(bad_key, 7, http_call=http)
    out3 = motahai.promote_live(bad_key, APPROVAL_TOKEN, http_call=http)
    for out in (out1, out2, out3):
        assert out["ok"] is False and out["reason_code"] == "INVALID_TENANT_KEY"
        assert out["audience"] == "operator"
    assert http.calls == []


@pytest.mark.parametrize("bad_window", [0, 29, -5, True, False, "7", None, 1.5])
def test_validate_window_days_rejects_out_of_range_without_http_call(core_env, bad_window):
    http = FakeHttp()
    out = motahai.signal_health("shop.myshopify.com", window_days=bad_window, http_call=http)
    assert out["ok"] is False and out["reason_code"] == "INVALID_WINDOW_DAYS"
    assert http.calls == []


def _all_three(http):
    return (
        lambda: motahai.tenant_status("shop.myshopify.com", http_call=http),
        lambda: motahai.signal_health("shop.myshopify.com", 7, http_call=http),
        lambda: motahai.promote_live("shop.myshopify.com", APPROVAL_TOKEN, http_call=http),
    )


def test_missing_env_key_makes_no_http_call(monkeypatch):
    monkeypatch.setenv("MOTAHAI_CORE_URL", "http://core.test:8080/")
    monkeypatch.delenv("MOTAHAI_TENANT_API_KEY", raising=False)
    http = FakeHttp()
    for fn in _all_three(http):
        out = fn()
        assert out["ok"] is False
        assert out["reason_code"] == "CORE_NOT_CONFIGURED"
        assert out["audience"] == "operator"
    assert http.calls == []


def test_missing_url_makes_no_http_call(monkeypatch):
    monkeypatch.delenv("MOTAHAI_CORE_URL", raising=False)
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", API_KEY)
    http = FakeHttp()
    for fn in _all_three(http):
        out = fn()
        assert out["ok"] is False and out["reason_code"] == "CORE_NOT_CONFIGURED"
        assert "MOTAHAI_CORE_URL" in out["detail"]
    assert http.calls == []


def test_both_unset_makes_no_http_call(monkeypatch):
    monkeypatch.delenv("MOTAHAI_CORE_URL", raising=False)
    monkeypatch.delenv("MOTAHAI_TENANT_API_KEY", raising=False)
    http = FakeHttp()
    for fn in _all_three(http):
        out = fn()
        assert out["ok"] is False and out["reason_code"] == "CORE_NOT_CONFIGURED"
        assert "MOTAHAI_CORE_URL" in out["detail"] and "MOTAHAI_TENANT_API_KEY" in out["detail"]
    assert http.calls == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_values_are_not_configured_and_make_no_call(monkeypatch, blank):
    monkeypatch.setenv("MOTAHAI_CORE_URL", blank)
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", blank)
    http = FakeHttp()
    for fn in _all_three(http):
        out = fn()
        assert out["ok"] is False and out["reason_code"] == "CORE_NOT_CONFIGURED"
    assert http.calls == []


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8000",
        "http://127.0.0.2:8000/",
        "http://localhost:8000/",
        "http://LOCALHOST:8000",
        "http://[::1]:8000",
        "http://0.0.0.0:8000",
        "http://[::]:8000",
        "http://[::ffff:127.0.0.1]:8000",
        "ftp://core.test",
        "not a url",
        "http://core.test:notaport",
        "http://[::1",
        "http://:8080",
    ],
)
def test_unsafe_or_malformed_core_url_is_refused_without_call(monkeypatch, bad_url):
    monkeypatch.setenv("MOTAHAI_CORE_URL", bad_url)
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", API_KEY)
    http = FakeHttp()
    for fn in _all_three(http):
        out = fn()
        assert out["ok"] is False and out["reason_code"] == "CORE_NOT_CONFIGURED"
        assert out["audience"] == "operator"
    assert http.calls == []


def test_reserved_port_refusal_explains_the_reason(monkeypatch):
    monkeypatch.setenv("MOTAHAI_CORE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", API_KEY)
    out = motahai.tenant_status("shop.myshopify.com", http_call=FakeHttp())
    assert "8000" in out["detail"] and "reserved" in out["detail"]
    assert "127.0.0.1" not in out["detail"]


@pytest.mark.parametrize("ok_url", ["http://127.0.0.1:8080", "http://0.0.0.0:8080", "https://core.example.com"])
def test_allowed_core_url_makes_call(monkeypatch, ok_url):
    monkeypatch.setenv("MOTAHAI_CORE_URL", ok_url)
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", API_KEY)
    http = FakeHttp(status=200, body={"tenant_key": "shop.myshopify.com"})
    out = motahai.tenant_status("shop.myshopify.com", http_call=http)
    assert out["ok"] is True
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == f"{ok_url}/v1/tenants/shop.myshopify.com/status"


def test_real_core_401_still_maps_to_auth_failed(core_env):
    http = FakeHttp(status=401, body={"detail": "Unauthorized"})
    out = motahai.tenant_status("shop.myshopify.com", http_call=http)
    assert out["reason_code"] == "CORE_AUTH_FAILED"
    assert len(http.calls) == 1


def test_default_core_url_constant_is_gone():
    assert hasattr(motahai, "DEFAULT_CORE_URL") is False


def test_missing_key_detail_never_contains_key_value(monkeypatch):
    monkeypatch.setenv("MOTAHAI_CORE_URL", "http://core.test:8080/")
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", "   ")
    out = motahai.tenant_status("shop.myshopify.com", http_call=FakeHttp())
    assert out["reason_code"] == "CORE_NOT_CONFIGURED"
    assert "MOTAHAI_TENANT_API_KEY" in out["detail"]


def test_missing_url_detail_never_contains_key_value(monkeypatch):
    monkeypatch.delenv("MOTAHAI_CORE_URL", raising=False)
    monkeypatch.setenv("MOTAHAI_TENANT_API_KEY", "secret-xyz")
    out = motahai.tenant_status("shop.myshopify.com", http_call=FakeHttp())
    assert out["reason_code"] == "CORE_NOT_CONFIGURED"
    assert "secret-xyz" not in json.dumps(out)


def test_tenant_status_200_passes_core_contract_unchanged(core_env):
    core_body = {
        "tenant_key": "epxsmk-y0.myshopify.com",
        "platform": "shopify",
        "mode": "shadow",
        "active": True,
        "created_at": "2026-10-01T00:00:00Z",
        "has_meta_credentials": True,
        "last_webhook_at": "2026-10-10T12:00:00Z",
        "webhook_failures_7d": 0,
        "last_capi_event_at": "2026-10-10T12:01:00Z",
        "webhooks_verified": True,
        "checklist_ready": True,
        "onboarded": True,
        "missing_required_steps": [],
        "test_event_code_set": False,
    }
    http = FakeHttp(status=200, body=core_body)
    out = motahai.tenant_status("epxsmk-y0.myshopify.com", http_call=http)
    assert out == {"ok": True, **core_body}
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://core.test:8080/v1/tenants/epxsmk-y0.myshopify.com/status"
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert call["timeout_s"] == 15.0


def test_signal_health_200_passes_core_contract_unchanged(core_env):
    core_body = {
        "tenant_key": "epxsmk-y0.myshopify.com",
        "window": {"start": "2026-10-04T00:00:00Z", "end": "2026-10-11T00:00:00Z", "days": 14},
        "cohort_window": {"start": "2026-09-20T00:00:00Z", "end": "2026-10-04T00:00:00Z"},
        "orders": {"placed": 42, "cod": 38, "confirmed": 35},
        "cohort": {"orders": 30, "delivered": 24, "refused": 6},
        "capi_events": {"sent": 0, "shadow": 24, "failed": 0, "stale": 0, "late_delivery": 1},
        "would_have_sent": 24,
        "open_incidents": [{"type": "courier_lag", "opened_at": "2026-10-09T00:00:00Z"}],
    }
    http = FakeHttp(status=200, body=core_body)
    out = motahai.signal_health("epxsmk-y0.myshopify.com", window_days=14, http_call=http)
    assert out == {"ok": True, **core_body}
    assert http.calls[0]["url"] == "http://core.test:8080/v1/tenants/epxsmk-y0.myshopify.com/signal-health?window_days=14"


def test_promote_live_200_and_missing_token_handling(core_env):
    http = FakeHttp(
        status=200,
        body={"tenant_key": "epxsmk-y0.myshopify.com", "mode": "live", "promoted_live_at": "2026-10-11T04:00:00Z"},
    )
    missing = motahai.promote_live("epxsmk-y0.myshopify.com", "   ", http_call=http)
    assert missing["ok"] is False and missing["reason_code"] == "TOKEN_REJECTED"
    assert http.calls == []

    out = motahai.promote_live("epxsmk-y0.myshopify.com", APPROVAL_TOKEN, http_call=http)
    assert out == {
        "ok": True,
        "tenant_key": "epxsmk-y0.myshopify.com",
        "mode": "live",
        "promoted_live_at": "2026-10-11T04:00:00Z",
    }
    assert http.calls[0]["method"] == "POST"
    assert http.calls[0]["url"] == "http://core.test:8080/v1/tenants/epxsmk-y0.myshopify.com/promote-live"
    assert http.calls[0]["body"] == {"approval_token": APPROVAL_TOKEN}


@pytest.mark.parametrize(
    "status,body,expected_code,expected_retryable",
    [
        (401, {"detail": "Unauthorized"}, "CORE_AUTH_FAILED", False),
        (404, {"detail": "Not found"}, "TENANT_NOT_FOUND", False),
        (403, {"error": "token_rejected"}, "TOKEN_REJECTED", False),
        (409, {"error": "already_live"}, "ALREADY_LIVE", False),
        (
            409,
            {
                "error": "precondition_failed",
                "failed": ["test_event_code_still_set", "no_delivered_evidence"],
            },
            "PRECONDITION_FAILED",
            False,
        ),
        (500, {"detail": "Internal Server Error"}, "CORE_UNREACHABLE", True),
        (502, "Bad Gateway", "CORE_UNREACHABLE", True),
        (503, {"detail": "Unavailable"}, "CORE_UNREACHABLE", True),
    ],
)
def test_status_code_mappings(core_env, status, body, expected_code, expected_retryable):
    http = FakeHttp(status=status, body=body)
    out = motahai.promote_live("shop.myshopify.com", APPROVAL_TOKEN, http_call=http)
    assert out["ok"] is False
    assert out["reason_code"] == expected_code
    assert out["audience"] == "operator"
    assert out["retryable"] is expected_retryable
    if expected_code == "TOKEN_REJECTED":
        assert "NEW" in out["detail"]
    if expected_code == "PRECONDITION_FAILED":
        assert out["failed"] == ["test_event_code_still_set", "no_delivered_evidence"]


def test_network_exception_maps_to_core_unreachable_and_never_leaks_secrets(core_env, caplog):
    caplog.set_level(logging.WARNING)
    boom = OSError(f"Connection failed with header Bearer {API_KEY} and token {APPROVAL_TOKEN}")
    http = FakeHttp(exc=boom)
    out = motahai.promote_live("shop.myshopify.com", APPROVAL_TOKEN, http_call=http)
    dumped = json.dumps(out)
    assert out["ok"] is False
    assert out["reason_code"] == "CORE_UNREACHABLE"
    assert out["retryable"] is True
    assert API_KEY not in dumped and APPROVAL_TOKEN not in dumped
    assert API_KEY not in caplog.text and APPROVAL_TOKEN not in caplog.text


def test_secrets_never_appear_in_error_responses(core_env):
    http = FakeHttp(status=403, body={"error": f"bad token {APPROVAL_TOKEN} with key {API_KEY}"})
    out = motahai.promote_live("shop.myshopify.com", APPROVAL_TOKEN, http_call=http)
    dumped = json.dumps(out)
    assert out["reason_code"] == "TOKEN_REJECTED"
    assert API_KEY not in dumped
    assert APPROVAL_TOKEN not in dumped


def test_consultation_registers_all_three_motahai_tools(core_env, monkeypatch):
    pkg = types.ModuleType("fakehermes_motahai")
    pkg.__path__ = []
    sub = types.ModuleType("fakehermes_motahai.tools")
    sub.__path__ = []
    executor = types.ModuleType("fakehermes_motahai.executor")
    executor.VPSExecutor = object
    for name, mod in (
        ("fakehermes_motahai", pkg),
        ("fakehermes_motahai.tools", sub),
        ("fakehermes_motahai.executor", executor),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("fakehermes_motahai.tools.consultation", CONSULTATION_PATH)
    consultation = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, consultation)
    spec.loader.exec_module(consultation)

    tools = {}

    class DummyServer:
        executor = object()

        def tool(self, name, description, input_schema):
            def dec(fn):
                tools[name] = fn
                return fn

            return dec

    consultation.register_consultation_tools(DummyServer())
    for expected in (
        "hermes_motahai_tenant_status",
        "hermes_motahai_signal_health",
        "hermes_motahai_promote_live",
    ):
        assert expected in tools

    # Verify the wrapper delegates to motahai_api and returns JSON string
    res = json.loads(tools["hermes_motahai_tenant_status"]("BAD KEY"))
    assert res["ok"] is False and res["reason_code"] == "INVALID_TENANT_KEY"
