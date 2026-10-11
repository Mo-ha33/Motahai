"""Tests for Hermes activity telemetry (sentinel T6): producer, consultation hook, consumer, template, sender.

Run: python -m pytest -q ops/hermes/sentinel

Everything is mocked: no network (httpx.MockTransport), no real Telegram, files go to pytest's tmp_path,
HERMES_SENTINEL_DIR is pinned per test.
"""

import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

HERMES_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERMES_DIR))  # ops/hermes, so the package imports as `sentinel`
from sentinel import detect as D  # noqa: E402
from sentinel import dispatch as DP  # noqa: E402
from sentinel import telemetry as TM  # noqa: E402
from sentinel import templates as T  # noqa: E402

T0 = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)
GTM = "GTM-ABC1234"
EMAIL = "someone.private@customer-mail.example"
TOKEN = "SECRET-APPROVAL-TOKEN-xyz.abc123"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SENTINEL_DIR", str(tmp_path / "sentinel"))
    monkeypatch.delenv("SENTINEL_TELEMETRY_EXCLUDE", raising=False)
    monkeypatch.delenv("SENTINEL_TELEMETRY_TELEGRAM", raising=False)
    monkeypatch.setenv("GTM_CACHE_DIR", str(tmp_path / "gtm-cache"))
    monkeypatch.setattr(TM, "_dropped", 0)
    monkeypatch.setattr(TM, "_drop_logged", False)


def events_file(tmp_path):
    return tmp_path / "sentinel" / "telemetry" / "events.jsonl"


def read_events(tmp_path):
    path = events_file(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------------------------------------
# Producer: emit()
# ---------------------------------------------------------------------------------------------------------
def test_emit_writes_one_json_line_with_the_documented_fields(tmp_path):
    assert TM.emit("hermes_storefront_probe", "OK", 14312.4, "epxsmk-y0.myshopify.com", now=T0) is True
    raw = events_file(tmp_path).read_text(encoding="utf-8")
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert json.loads(raw) == {"ts": "2026-10-11T10:00:00Z", "tool": "hermes_storefront_probe", "outcome": "OK",
                               "latency_ms": 14312, "subject": "epxsmk-y0.myshopify.com"}
    assert list(json.loads(raw)) == ["ts", "tool", "outcome", "latency_ms", "subject"]


def test_emit_appends_and_subject_may_be_null(tmp_path):
    TM.emit("hermes_consult", "OK", 5, now=T0)
    TM.emit("hermes_run_task", "ValueError", 7, None, now=T0)
    events = read_events(tmp_path)
    assert [e["tool"] for e in events] == ["hermes_consult", "hermes_run_task"]
    assert all(e["subject"] is None for e in events)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_emit_creates_private_dir_and_file(tmp_path):
    TM.emit("hermes_consult", "OK", 1)
    assert (events_file(tmp_path).stat().st_mode & 0o777) == 0o600
    assert (events_file(tmp_path).parent.stat().st_mode & 0o777) == 0o700


def test_emit_rejects_free_text_in_every_field(tmp_path):
    TM.emit("tool with spaces", "an outcome with free text and spaces", "NaN-ish", EMAIL, now=T0)
    TM.emit("hermes_consult", TOKEN + " leaked", 3, "https://shop.example/path?email=" + EMAIL, now=T0)
    raw = events_file(tmp_path).read_text(encoding="utf-8")
    for secret in (EMAIL, TOKEN, "spaces", "free text", "path?"):
        assert secret not in raw
    first, second = read_events(tmp_path)
    assert (first["tool"], first["outcome"], first["latency_ms"], first["subject"]) == ("unknown", "UNKNOWN", 0, None)
    assert second["subject"] is None


def test_emit_never_raises_when_the_directory_is_unusable(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("HERMES_SENTINEL_DIR", str(blocker / "sub"))
    with caplog.at_level(logging.WARNING, logger="hermes.sentinel.telemetry"):
        assert TM.emit("hermes_consult", "OK", 1) is False
    assert "emit failed" in caplog.text


def test_size_cap_drops_events_counts_them_and_logs_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(TM, "MAX_FILE_BYTES", 300)
    written = 0
    while TM.emit("hermes_consult", "OK", 1):
        written += 1
        assert written < 50
    size = events_file(tmp_path).stat().st_size
    assert size > 300
    with caplog.at_level(logging.WARNING, logger="hermes.sentinel.telemetry"):
        for _ in range(5):
            assert TM.emit("hermes_consult", "OK", 1) is False
    assert events_file(tmp_path).stat().st_size == size
    assert TM.dropped_count() == 6          # the first failed emit plus these five
    assert caplog.text.count("exceeds") <= 1


def test_concurrent_emits_never_tear_a_line(tmp_path):
    def worker(n):
        for i in range(40):
            TM.emit(f"tool_{n}", "OK", i, f"shop{n}.example.com")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = read_events(tmp_path)           # json.loads on every line: a torn line would raise
    assert len(events) == 320


OUTCOME_TABLE = [
    (json.dumps({"status": "CONNECTED", "container_id": "555"}), "CONNECTED"),
    (json.dumps({"status": "applied", "workspace_id": "7"}), "applied"),
    (json.dumps({"status": "success", "live_version_id": "9"}), "published"),
    (json.dumps({"status": "OK", "gtm_ids_seen": []}), "OK"),
    (json.dumps({"status": "UNKNOWN", "reason": "BLOCKED_HOST"}), "UNKNOWN:BLOCKED_HOST"),
    (json.dumps({"status": "UNKNOWN", "reason": "free text with spaces"}), "UNKNOWN"),
    (json.dumps({"status": "error", "reason_code": "CORE_NOT_CONFIGURED", "message": "x@y.example"}), "CORE_NOT_CONFIGURED"),
    (json.dumps({"status": "error", "message": "boom"}), "error"),
    (json.dumps({"status": "AWAITING_HITL_APPROVAL", "approval_check": "token_missing"}), "AWAITING_HITL_APPROVAL"),
    (json.dumps({"status": "error", "reason_code": "NOT_INVITED", "audience": "client"}), "NOT_INVITED"),
    (json.dumps({"tenant": "x", "mode": "shadow"}), "OK"),
    ('```json\n{"status": "BLOCKED", "decision": "rate-limited"}\n```', "BLOCKED"),
    ("A long prose answer from Hermes.\n\nprovenance", "OK"),
    ("{not json", "OK"),
    (None, "OK"),
    (b'{"status": "CONNECTED"}', "CONNECTED"),
]


@pytest.mark.parametrize("result,expected", OUTCOME_TABLE)
def test_outcome_of(result, expected):
    assert TM.outcome_of(result) == expected


SUBJECT_TABLE = [
    ({"public_container_id": GTM}, GTM),
    ({"container_id": "GTM-5C5N552P", "version_name": "v1"}, "GTM-5C5N552P"),
    ({"store_url": "https://EPXSMK-y0.myshopify.com/ar/products/x?utm=1&email=" + EMAIL}, "epxsmk-y0.myshopify.com"),
    ({"store_url": "https://user:pw@shop.example.com:8443/a/b?q=1#frag"}, "shop.example.com"),
    ({"store_url": "shop.example.com/path"}, "shop.example.com"),
    ({"store_url": "http://not a url"}, None),
    ({"tenant_key": "store.myshopify.com"}, "store.myshopify.com"),
    ({"tenant_key": "123456789"}, None),                      # numeric Salla merchant id is not published
    ({"tenant_key": EMAIL}, None),
    ({"store_url": "https://shop.example.com", "gtm_public_id": GTM}, "shop.example.com"),
    ({"gtm_public_id": GTM}, GTM),
    ({"prompt": f"mail {EMAIL} token {TOKEN}", "context": "https://x.example/a?b=c"}, None),
    ({"hitl_approval_token": TOKEN, "container_id": "265962858"}, None),
    ({"container_id": "not-a-gtm-id"}, None),
    (None, None),
]


@pytest.mark.parametrize("arguments,expected", SUBJECT_TABLE)
def test_subject_of(arguments, expected):
    assert TM.subject_of(arguments) == expected


def test_subject_of_resolves_a_numeric_container_through_the_lookup():
    assert TM.subject_of({"container_id": "555"}, lookup=lambda numeric: GTM if numeric == "555" else None) == GTM
    assert TM.subject_of({"container_id": "556"}, lookup=lambda numeric: None) is None


# ---------------------------------------------------------------------------------------------------------
# instrument(): the wrapper
# ---------------------------------------------------------------------------------------------------------
def test_instrument_keeps_result_exception_signature_and_records_latency(tmp_path):
    def slow(store_url: str, note: str = "x") -> str:
        """doc"""
        time.sleep(0.05)
        return json.dumps({"status": "OK"})

    def boom(prompt: str):
        raise KeyError("secret detail " + EMAIL)

    wrapped = TM.instrument("hermes_storefront_probe", slow)
    assert wrapped("https://s.example.com/x?e=1", note="n") == json.dumps({"status": "OK"})
    assert wrapped.__name__ == "slow" and wrapped.__doc__ == "doc"
    assert inspect.signature(wrapped) == inspect.signature(slow)
    failing = TM.instrument("hermes_consult", boom)
    with pytest.raises(KeyError) as caught:
        failing(prompt=f"hi {EMAIL}")
    assert str(caught.value) == repr("secret detail " + EMAIL)
    ok, err = read_events(tmp_path)
    assert (ok["tool"], ok["outcome"], ok["subject"]) == ("hermes_storefront_probe", "OK", "s.example.com")
    assert 40 <= ok["latency_ms"] < 5000
    assert (err["tool"], err["outcome"], err["subject"]) == ("hermes_consult", "KeyError", None)
    assert EMAIL not in events_file(tmp_path).read_text(encoding="utf-8")


def test_instrument_is_transparent_when_emit_fails(monkeypatch):
    monkeypatch.setattr(TM, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    wrapped = TM.instrument("hermes_consult", lambda prompt: "answer")
    assert wrapped("q") == "answer"


def test_instrument_skips_excluded_and_async_tools(monkeypatch):
    def fn():
        return "x"

    async def afn():
        return "x"

    monkeypatch.setenv("SENTINEL_TELEMETRY_EXCLUDE", "hermes_consult, hermes_run_task")
    assert TM.instrument("hermes_consult", fn) is fn
    assert TM.instrument("hermes_run_task", fn) is fn
    assert TM.instrument("hermes_audit_code", fn) is not fn
    assert TM.instrument("hermes_audit_code", afn) is afn
    monkeypatch.setenv("SENTINEL_TELEMETRY_EXCLUDE", "*")
    assert TM.instrument("hermes_audit_code", fn) is fn


# ---------------------------------------------------------------------------------------------------------
# The hook in consultation.py
# ---------------------------------------------------------------------------------------------------------
class _RecordingServer:
    def __init__(self, executor=None):
        self.executor = executor if executor is not None else object()
        self.tools = {}

    def tool(self, name, description, input_schema):
        def decorator(fn):
            self.tools[name] = fn
            return fn
        return decorator


class _Executor:
    def execute_hermes_cli(self, prompt, model=None, skills=None):
        return f"HERMES SAYS ({len(prompt)})", True


def _load_into(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def consultation(monkeypatch, tmp_path):
    pkg = types.ModuleType("fakehermes_telemetry")
    pkg.__path__ = []
    sub = types.ModuleType("fakehermes_telemetry.tools")
    sub.__path__ = []
    executor = types.ModuleType("fakehermes_telemetry.executor")
    executor.VPSExecutor = object
    for name, mod in (("fakehermes_telemetry", pkg), ("fakehermes_telemetry.tools", sub),
                      ("fakehermes_telemetry.executor", executor)):
        monkeypatch.setitem(sys.modules, name, mod)
    module = _load_into(monkeypatch, "fakehermes_telemetry.tools.consultation", HERMES_DIR / "consultation.py")
    key_file = tmp_path / "gtm-service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "GTM_SERVICE_ACCOUNT_PATH", str(key_file))
    monkeypatch.setenv("HITL_USED_NONCES_PATH", str(tmp_path / "state" / "nonces.json"))
    return module


@pytest.fixture
def tools(consultation):
    server = _RecordingServer(_Executor())
    consultation.register_consultation_tools(server)
    return server.tools


ALL_TOOLS = ["hermes_consult", "hermes_run_task", "hermes_audit_code", "hermes_gtm_deploy", "hermes_gtm_verify_access",
             "hermes_gtm_workspace_apply", "hermes_gtm_cloud_publish", "hermes_motahai_tenant_status",
             "hermes_motahai_signal_health", "hermes_motahai_promote_live", "hermes_storefront_probe"]


def test_every_tool_is_registered_with_the_hook_and_keeps_its_signature(tools, consultation):
    assert sorted(tools) == sorted(ALL_TOOLS)
    for name, fn in tools.items():
        assert hasattr(fn, "__wrapped__"), name
        assert inspect.signature(fn) == inspect.signature(fn.__wrapped__)
        assert fn.__name__ == name


def test_storefront_probe_result_is_unchanged_and_one_event_is_written(tmp_path, tools, consultation, monkeypatch):
    probe = consultation._storefront_probe()
    payload = {"status": "OK", "gtm_ids_seen": [GTM]}
    monkeypatch.setattr(probe, "probe_storefront", lambda **kw: payload)
    out = tools["hermes_storefront_probe"]("https://epxsmk-y0.myshopify.com/ar?email=" + EMAIL, gtm_public_id=GTM)
    assert out == json.dumps(payload, ensure_ascii=False)
    (event,) = read_events(tmp_path)
    assert (event["tool"], event["outcome"], event["subject"]) == ("hermes_storefront_probe", "OK", "epxsmk-y0.myshopify.com")
    assert EMAIL not in events_file(tmp_path).read_text(encoding="utf-8")


def test_blocked_probe_outcome_carries_the_reason(tmp_path, tools):
    out = json.loads(tools["hermes_storefront_probe"]("http://not-https.example.com"))
    assert out["status"] == "UNKNOWN"
    (event,) = read_events(tmp_path)
    assert event["outcome"] == "UNKNOWN:BLOCKED_HOST" and event["subject"] == "not-https.example.com"


def test_verify_apply_publish_events_use_the_public_gtm_id(tmp_path, tools, consultation, monkeypatch):
    api = consultation._gtm_api()
    monkeypatch.setattr(consultation, "_gtm_service", lambda scopes: (object(), None))
    monkeypatch.setattr(api, "verify_access", lambda svc, cid: {"status": "CONNECTED", "container_id": "555"})
    monkeypatch.setattr(api, "apply_container_spec", lambda svc, cid, spec: {"status": "applied", "container_id": "555"})
    monkeypatch.setattr(api, "create_and_publish_version", lambda *a, **k: {"status": "success", "container_id": "555"})
    monkeypatch.setattr(consultation, "_hitl_gate", lambda *a, **k: None)

    assert json.loads(tools["hermes_gtm_verify_access"](public_container_id=GTM))["status"] == "CONNECTED"
    assert json.loads(tools["hermes_gtm_workspace_apply"](public_container_id=GTM, container_spec="{}"))["status"] == "applied"
    assert json.loads(tools["hermes_gtm_cloud_publish"](account_id="1", container_id="555", hitl_approval_token=TOKEN))["status"] == "success"
    got = [(e["tool"], e["outcome"], e["subject"]) for e in read_events(tmp_path)]
    assert got == [("hermes_gtm_verify_access", "CONNECTED", GTM), ("hermes_gtm_workspace_apply", "applied", GTM),
                   ("hermes_gtm_cloud_publish", "published", GTM)]
    assert TOKEN not in events_file(tmp_path).read_text(encoding="utf-8")


def test_publish_refusal_is_reported_as_awaiting_approval(tmp_path, tools, monkeypatch):
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY", raising=False)
    out = json.loads(tools["hermes_gtm_cloud_publish"](account_id="1", container_id="555", hitl_approval_token=TOKEN))
    assert out["status"] == "AWAITING_HITL_APPROVAL"
    (event,) = read_events(tmp_path)
    assert event["outcome"] == "AWAITING_HITL_APPROVAL" and event["subject"] is None
    assert TOKEN not in events_file(tmp_path).read_text(encoding="utf-8")


def test_motahai_tools_report_reason_code_and_tenant_domain(tmp_path, tools, consultation, monkeypatch):
    api = consultation._motahai_api()
    monkeypatch.setattr(api, "tenant_status", lambda tenant_key: {"status": "error", "reason_code": "CORE_NOT_CONFIGURED"})
    monkeypatch.setattr(api, "signal_health", lambda tenant_key, window_days=7: {"orders": {"placed": 3}})
    monkeypatch.setattr(api, "promote_live", lambda tenant_key, token: {"status": "error", "reason_code": "APPROVAL_INVALID"})
    tools["hermes_motahai_tenant_status"]("store.myshopify.com")
    tools["hermes_motahai_signal_health"]("store.myshopify.com", window_days=7)
    tools["hermes_motahai_promote_live"]("store.myshopify.com", TOKEN)
    got = [(e["tool"], e["outcome"], e["subject"]) for e in read_events(tmp_path)]
    assert got == [("hermes_motahai_tenant_status", "CORE_NOT_CONFIGURED", "store.myshopify.com"),
                   ("hermes_motahai_signal_health", "OK", "store.myshopify.com"),
                   ("hermes_motahai_promote_live", "APPROVAL_INVALID", "store.myshopify.com")]
    assert TOKEN not in events_file(tmp_path).read_text(encoding="utf-8")


def test_consult_run_task_and_audit_code_log_no_prompt_text(tmp_path, tools):
    prompt = f"please email {EMAIL} with token {TOKEN}"
    assert tools["hermes_consult"](prompt).startswith("HERMES SAYS")
    assert tools["hermes_run_task"](prompt).startswith("HERMES SAYS")
    assert tools["hermes_audit_code"](f"code {EMAIL}", file_path="a.py").startswith("HERMES SAYS")
    events = read_events(tmp_path)
    # audit_code calls consult internally through its local name: that inner call is NOT a second event
    assert [e["tool"] for e in events] == ["hermes_consult", "hermes_run_task", "hermes_audit_code"]
    assert all(e["subject"] is None and e["outcome"] == "OK" for e in events)
    raw = events_file(tmp_path).read_text(encoding="utf-8")
    assert EMAIL not in raw and TOKEN not in raw and "please" not in raw


def test_tool_exception_is_reraised_unchanged_and_recorded(tmp_path, consultation):
    class Exploding:
        def execute_hermes_cli(self, **kw):
            raise TimeoutError("ssh timed out for " + EMAIL)

    server = _RecordingServer(Exploding())
    consultation.register_consultation_tools(server)
    with pytest.raises(TimeoutError, match="ssh timed out"):
        server.tools["hermes_run_task"]("do it")
    (event,) = read_events(tmp_path)
    assert (event["tool"], event["outcome"]) == ("hermes_run_task", "TimeoutError")
    assert EMAIL not in events_file(tmp_path).read_text(encoding="utf-8")


def test_exclude_env_leaves_those_tools_unwrapped(tmp_path, consultation, monkeypatch):
    monkeypatch.setenv("SENTINEL_TELEMETRY_EXCLUDE", "hermes_consult,hermes_run_task")
    server = _RecordingServer(_Executor())
    consultation.register_consultation_tools(server)
    assert not hasattr(server.tools["hermes_consult"], "__wrapped__")
    assert not hasattr(server.tools["hermes_run_task"], "__wrapped__")
    assert hasattr(server.tools["hermes_audit_code"], "__wrapped__")
    server.tools["hermes_consult"]("q")
    server.tools["hermes_run_task"]("q")
    assert read_events(tmp_path) == []
    server.tools["hermes_audit_code"]("code")
    assert [e["tool"] for e in read_events(tmp_path)] == ["hermes_audit_code"]


def test_tools_still_register_and_work_if_telemetry_cannot_load(tmp_path, consultation, monkeypatch):
    def broken():
        raise ImportError("telemetry.py missing on this host")

    monkeypatch.setattr(consultation, "_telemetry", broken)
    server = _RecordingServer(_Executor())
    consultation.register_consultation_tools(server)
    assert sorted(server.tools) == sorted(ALL_TOOLS)
    assert not hasattr(server.tools["hermes_consult"], "__wrapped__")
    assert server.tools["hermes_run_task"]("x").startswith("HERMES SAYS")
    assert read_events(tmp_path) == []


def test_hook_is_negligible_overhead(tmp_path, tools, consultation, monkeypatch):
    monkeypatch.setattr(consultation._storefront_probe(), "probe_storefront", lambda **kw: {"status": "OK"})
    started = time.perf_counter()
    for _ in range(50):
        tools["hermes_storefront_probe"]("https://s.example.com")
    assert (time.perf_counter() - started) / 50 < 0.05   # well under 50 ms per call, including the file append


# ---------------------------------------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------------------------------------
def ev(tool="hermes_storefront_probe", subject="epxsmk-y0.myshopify.com", outcome="OK", latency_ms=14300):
    return {"ts": "2026-10-11T10:00:00Z", "tool": tool, "outcome": outcome, "latency_ms": latency_ms, "subject": subject}


def test_summary_sample_rendering():
    text = T.telemetry_summary([ev(), ev("hermes_gtm_verify_access", GTM, "CONNECTED", 850),
                                ev("hermes_consult", None, "OK", 2100), ev("hermes_storefront_probe", "x.example.com",
                                                                            "UNKNOWN:BLOCKED_HOST", 40)], window_s=61)
    assert text.splitlines() == [
        "Hermes activity (last 60 s): 4 calls",
        "نشاط هيرمس: 4 استدعاء",
        "",
        "• hermes_storefront_probe  epxsmk-y0.myshopify.com  OK  14.3 s",
        f"• hermes_gtm_verify_access  {GTM}  CONNECTED  850 ms",
        "• hermes_consult  OK  2.1 s",
        "• hermes_storefront_probe  x.example.com  UNKNOWN:BLOCKED_HOST  40 ms",
    ]


def test_summary_caps_lines_and_says_how_many_more():
    text = T.telemetry_summary([ev(latency_ms=i) for i in range(30)])
    lines = text.splitlines()
    assert sum(1 for line in lines if line.startswith("• ")) == 25
    assert lines[-1] == "…and 5 more"
    assert lines[0] == "Hermes activity: 30 calls"


def test_summary_counts_events_dropped_by_the_backlog_cap():
    text = T.telemetry_summary([ev() for _ in range(200)], total=250)
    assert text.splitlines()[0] == "Hermes activity: 250 calls"
    assert text.splitlines()[-1] == "…and 225 more"


def test_summary_singular_plain_text_and_length_bound():
    assert T.telemetry_summary([ev()]).splitlines()[0] == "Hermes activity: 1 call"
    text = T.telemetry_summary([ev(tool="*bold* `code`", subject=None, outcome="A" * 500)] * 25)
    assert "*" not in text and "`" not in text and len(text) <= T.TELEGRAM_LIMIT
    assert T.telemetry_summary([None, 5, "x"]) .startswith("Hermes activity")


# ---------------------------------------------------------------------------------------------------------
# Sender: dispatch.send_admin_telegram
# ---------------------------------------------------------------------------------------------------------
BOT_TOKEN = "123456:SECRET-BOT-TOKEN"
CHAT = "987654321"


def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", CHAT)


def test_send_admin_telegram_posts_once_with_the_alert_transport(monkeypatch, caplog):
    telegram_env(monkeypatch)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    with caplog.at_level(logging.DEBUG):
        status = DP.send_admin_telegram("hello", sleep=lambda s: None, http_transport=httpx.MockTransport(handler))
    assert status == DP.SENT and len(seen) == 1
    assert "/sendMessage" in str(seen[0].url)
    assert json.loads(seen[0].content) == {"chat_id": CHAT, "text": "hello", "disable_web_page_preview": True}
    assert BOT_TOKEN not in caplog.text


def test_send_admin_telegram_retries_once_then_reports_failed(monkeypatch, caplog):
    telegram_env(monkeypatch)
    sleeps, calls = [], []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    with caplog.at_level(logging.DEBUG):
        status = DP.send_admin_telegram("hello", sleep=sleeps.append, http_transport=httpx.MockTransport(handler))
    assert status == DP.FAILED and len(calls) == 2 and sleeps == [DP.RETRY_DELAY_S]
    assert BOT_TOKEN not in caplog.text


def test_send_admin_telegram_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)
    assert DP.send_admin_telegram("hello") == DP.NOT_CONFIGURED


def test_default_send_telemetry_goes_through_the_telegram_sender(monkeypatch):
    seen = []
    monkeypatch.setattr(DP, "send_admin_telegram", lambda text, *a, **k: seen.append(text) or DP.SENT)
    assert D.default_send_telemetry("msg") == "sent" and seen == ["msg"]


# ---------------------------------------------------------------------------------------------------------
# Consumer: the sentinel pass
# ---------------------------------------------------------------------------------------------------------
class Env:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_SENTINEL_DIR", str(tmp_path / "sentinel"))
        self.dir = tmp_path / "sentinel"
        self.now = T0
        self.sent = []
        self.attempts = 0
        self.status = "sent"
        self.alert_calls = []
        self.cfg = D.Config(sentinel_dir=self.dir, router_url="http://router.invalid", router_key="k", heartbeat_url="")
        self.deps = D.Deps(now=lambda: self.now, read_journal=lambda cursor: ([], "s=1"),
                           fetch_healthz=lambda: (200, {"providers": [], "degraded": []}),
                           dispatch=self._alerts, heartbeat=lambda url: None, send_telemetry=self._send)

    def _alerts(self, notifications, contacts, now):
        self.alert_calls.append(notifications)
        return []

    def _send(self, text):
        if isinstance(self.status, Exception):
            raise self.status
        self.attempts += 1
        if self.status == "sent":
            self.sent.append(text)
        return self.status

    def emit(self, n=1, tool="hermes_consult", subject=None, outcome="OK"):
        for i in range(n):
            assert TM.emit(tool, outcome, 10 + i, subject, now=self.now)

    def run(self, advance=timedelta(seconds=60), dry_run=False, cfg=None):
        result = D.run_pass(cfg or self.cfg, self.deps, dry_run=dry_run)
        self.now += advance
        return result

    def state(self):
        return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))

    def path(self):
        return TM.events_path(self.dir)

    def silence(self, until):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "silence.json").write_text(json.dumps({"telemetry": until}), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_no_events_means_no_message_and_no_alert_dispatch(env):
    result = env.run()
    assert env.sent == [] and result.telemetry is None and env.alert_calls == []
    env.emit(1)
    env.run()
    env.sent.clear()
    env.run()                                   # nothing new since the offset
    assert env.sent == []


def test_one_message_per_pass_summarising_all_new_events(env):
    env.emit(1, "hermes_storefront_probe", "epxsmk-y0.myshopify.com")
    env.emit(1, "hermes_gtm_verify_access", GTM, "CONNECTED")
    env.emit(2, "hermes_consult")
    result = env.run()
    assert len(env.sent) == 1 and result.telemetry == env.sent[0]
    lines = env.sent[0].splitlines()
    assert lines[0].startswith("Hermes activity") and lines[0].endswith(": 4 calls")
    assert any("hermes_storefront_probe  epxsmk-y0.myshopify.com  OK" in line for line in lines)
    assert any(f"hermes_gtm_verify_access  {GTM}  CONNECTED" in line for line in lines)
    assert env.state()["telemetry_offset"] == env.path().stat().st_size
    env.emit(1)
    env.run()
    assert len(env.sent) == 2 and env.sent[1].splitlines()[0].endswith(": 1 call")   # only the new event


def test_header_reports_the_time_since_the_previous_pass(env):
    env.run()                                    # first pass records the tick
    env.emit(3)
    env.run()
    assert env.sent[0].startswith("Hermes activity (last 60 s): 3 calls")


def test_message_is_capped_at_25_lines_with_a_more_count(env):
    env.emit(30)
    env.run()
    (text,) = env.sent
    assert sum(1 for line in text.splitlines() if line.startswith("• ")) == 25
    assert text.splitlines()[-1] == "…and 5 more" and len(text) <= 3500


def test_alerts_and_telemetry_stay_separate_channels(env):
    env.emit(2)
    env.run()
    assert env.alert_calls == []                 # telemetry never goes through dispatch(notifications, contacts)
    assert len(env.sent) == 1


def test_disabled_flag_sends_nothing_but_consumes_the_events(env):
    env.cfg = D.Config(sentinel_dir=env.dir, router_url="http://x", router_key="k", heartbeat_url="", telemetry_enabled=False)
    env.emit(3)
    env.run()
    assert env.sent == [] and env.state()["telemetry_offset"] == env.path().stat().st_size
    env.cfg = D.Config(sentinel_dir=env.dir, router_url="http://x", router_key="k", heartbeat_url="")
    env.run()
    assert env.sent == []                        # re-enabling does not replay what happened while it was off


def test_config_reads_the_telegram_flag_from_the_environment():
    assert D.Config.from_env({}).telemetry_enabled is True
    assert D.Config.from_env({"SENTINEL_TELEMETRY_TELEGRAM": "1"}).telemetry_enabled is True
    assert D.Config.from_env({"SENTINEL_TELEMETRY_TELEGRAM": "0"}).telemetry_enabled is False


def test_silence_key_telemetry_suppresses_the_message(env):
    env.silence("2026-10-11T12:00:00Z")
    env.emit(2)
    env.run()
    assert env.sent == [] and env.state()["telemetry_offset"] == env.path().stat().st_size
    env.silence("2026-10-11T09:00:00Z")          # expired
    env.emit(1)
    env.run()
    assert len(env.sent) == 1


def test_not_configured_telegram_consumes_without_a_retry_storm(env):
    env.status = "skipped:not_configured"
    env.emit(2)
    result = env.run()
    assert result.telemetry is None
    assert env.state()["telemetry_offset"] == env.path().stat().st_size
    env.run()
    assert env.attempts == 1                     # one attempt, not one per pass


@pytest.mark.parametrize("failure", ["failed", RuntimeError("telegram down")])
def test_failed_send_keeps_the_offset_so_the_next_pass_retries(env, failure):
    env.emit(2)
    env.status = failure
    result = env.run()
    assert result.telemetry is None
    assert env.state().get("telemetry_offset", 0) == 0
    env.emit(1)
    env.status = "sent"
    env.run()
    assert len(env.sent) == 1 and env.sent[0].splitlines()[0].endswith(": 3 calls")   # retried, nothing lost
    assert env.state()["telemetry_offset"] == env.path().stat().st_size


def test_backlog_is_capped_at_the_last_200_events_and_counts_the_rest(env):
    env.emit(250)
    env.status = "failed"
    env.run()
    env.status = "sent"
    env.run()
    (text,) = env.sent
    assert text.splitlines()[0].endswith(": 250 calls")
    assert text.splitlines()[-1] == "…and 225 more"
    read = TM.read_new(env.path(), 0)
    assert len(read.events) == 200 and read.total == 250 and read.events[-1]["latency_ms"] == 10 + 249


def test_telegram_failure_for_an_alert_defers_telemetry(env):
    env.emit(1)
    p = D._Pass(now=T0, state=D._fresh_state(), incidents={})
    rows = [{"incident_key": "router:exhausted", "channel": "telegram", "status": "failed"}]
    assert D._telemetry_stage(p, env.cfg, env.deps, {}, rows, False) is None
    assert env.sent == [] and "telemetry_offset" not in p.state
    assert D._telemetry_stage(p, env.cfg, env.deps, {}, [], False) is not None


def test_truncated_file_restarts_from_the_beginning(env):
    env.emit(5)
    env.run()
    env.path().write_text("", encoding="utf-8")        # truncated by hand / logrotate copytruncate
    env.emit(2)
    env.run()
    assert len(env.sent) == 2 and env.sent[1].splitlines()[0].endswith(": 2 calls")


def test_replaced_file_with_a_new_inode_is_read_from_the_start(env):
    env.emit(2)
    env.run()
    saved = env.path().with_name("replacement.jsonl")
    lines = [json.dumps({"ts": "x", "tool": "hermes_consult", "outcome": "OK", "latency_ms": 1, "subject": None})] * 6
    saved.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(saved, env.path())                      # bigger than the old offset, but a different file
    env.run()
    assert len(env.sent) == 2 and env.sent[1].splitlines()[0].endswith(": 6 calls")


def test_partial_trailing_line_is_left_for_the_next_pass(env):
    env.emit(1)
    with open(env.path(), "ab") as fh:
        fh.write(b'{"ts":"2026-10-11T10:00:00Z","tool":"hermes_cons')
    env.run()
    assert len(env.sent) == 1 and env.sent[0].splitlines()[0].endswith(": 1 call")
    with open(env.path(), "ab") as fh:
        fh.write(b'ult","outcome":"OK","latency_ms":3,"subject":null}\n')
    env.run()
    assert len(env.sent) == 2 and env.sent[1].splitlines()[0].endswith(": 1 call")


def test_garbage_lines_are_skipped_not_rendered(env):
    env.emit(1)
    with open(env.path(), "ab") as fh:
        fh.write(b'not json\n{"ts":"x","tool":"evil tool *md*","outcome":"o k","latency_ms":1}\n[1,2]\n'
                 b'{"ts":"x","tool":"hermes_consult","outcome":"OK","latency_ms":1,"subject":"a@b.example"}\n')
    env.run()
    (text,) = env.sent
    assert text.splitlines()[0].endswith(": 2 calls") and "evil" not in text and "a@b.example" not in text


def test_fully_consumed_big_file_is_rotated_with_a_rename(env, monkeypatch):
    line = json.dumps({"ts": "2026-10-11T10:00:00Z", "tool": "hermes_consult", "outcome": "OK", "latency_ms": 1,
                       "subject": None}) + "\n"
    env.path().parent.mkdir(parents=True)
    env.path().write_text(line * (TM.ROTATE_MIN_BYTES // len(line) + 10), encoding="utf-8")
    env.run()
    assert len(env.sent) == 1
    assert not env.path().exists() and env.path().with_name("events.jsonl.1").exists()
    assert env.state()["telemetry_offset"] == 0
    env.emit(1)                                         # the next producer write starts a fresh file
    env.run()
    assert len(env.sent) == 2 and env.sent[1].splitlines()[0].endswith(": 1 call")


def test_small_consumed_file_is_not_rotated(env):
    env.emit(3)
    env.run()
    assert env.path().exists() and not env.path().with_name("events.jsonl.1").exists()


def test_rotate_if_consumed_requires_a_big_fully_consumed_file(env):
    env.path().parent.mkdir(parents=True)
    body = b"A" * (TM.ROTATE_MIN_BYTES + 10) + b"\n"
    env.path().write_bytes(body)
    assert TM.rotate_if_consumed(env.path(), 10) is False                                    # not fully consumed
    assert TM.rotate_if_consumed(env.path(), len(body), min_bytes=len(body) + 1) is False    # too small
    assert TM.rotate_if_consumed(env.path(), len(body)) is True
    assert not env.path().exists() and env.path().with_name("events.jsonl.1").read_bytes() == body


def test_late_append_between_check_and_rename_is_carried_over(env, monkeypatch):
    env.path().parent.mkdir(parents=True)
    body = b"A" * (TM.ROTATE_MIN_BYTES + 10) + b"\n"
    env.path().write_bytes(body)
    real_replace = os.replace

    def replace_after_a_late_write(src, dst):
        with open(src, "ab") as fh:
            fh.write(b'{"late":"line"}\n')
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_after_a_late_write)
    assert TM.rotate_if_consumed(env.path(), len(body)) is True
    monkeypatch.setattr(os, "replace", real_replace)
    assert env.path().read_bytes() == b'{"late":"line"}\n'


def test_dry_run_prints_the_message_and_persists_nothing(env, capsys):
    env.emit(3, "hermes_gtm_verify_access", GTM, "CONNECTED")
    result = env.run(dry_run=True)
    assert env.sent == [] and not (env.dir / "state.json").exists()
    assert result.telemetry and "hermes_gtm_verify_access" in result.telemetry
    code = D.main(["--once", "--dry-run"], env.deps, {"HERMES_SENTINEL_DIR": str(env.dir), "ROUTER_API_KEY": "k"})
    out = capsys.readouterr().out
    assert code == 0 and "telemetry message that would be sent:" in out and f"hermes_gtm_verify_access  {GTM}  CONNECTED" in out
    assert env.sent == [] and not (env.dir / "state.json").exists()
    env.run()                                          # a real pass afterwards still sends all three
    assert env.sent[0].splitlines()[0].endswith(": 3 calls")


def test_dry_run_stays_quiet_when_disabled(env, capsys):
    env.emit(2)
    cfg = D.Config(sentinel_dir=env.dir, router_url="http://x", router_key="k", heartbeat_url="", telemetry_enabled=False)
    assert env.run(dry_run=True, cfg=cfg).telemetry is None


def test_a_broken_telemetry_stage_never_fails_the_pass(env, monkeypatch):
    env.emit(1)
    monkeypatch.setattr(TM, "read_new", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = env.run()
    assert result.exit_code == 0 and result.persisted and env.sent == []


def test_existing_state_without_telemetry_keys_loads_fine(env):
    env.dir.mkdir(parents=True)
    (env.dir / "state.json").write_text(json.dumps({"cursor": None, "healthz_fail": 0, "clear": {}, "budget_day": {},
                                                    "tick": None}), encoding="utf-8")
    env.emit(1)
    env.run()
    assert len(env.sent) == 1 and env.state()["telemetry_offset"] > 0
