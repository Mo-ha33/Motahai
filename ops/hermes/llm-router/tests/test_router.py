"""Failover tests for hermes-llm-router. Run: python -m pytest tests -q"""

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient


sys.path.insert(0, str(Path(__file__).parent.parent))
import router as r


KEY = "test-router-key-0123456789abcdef"
HOSTS = {"integrate.api.nvidia.com": "nvidia", "openrouter.ai": "openrouter",
         "generativelanguage.googleapis.com": "gemini"}
TOOLS = [{"type": "function", "function": {"name": "terminal", "parameters": {
    "type": "object", "$schema": "x", "properties": {"cmd": {"type": "string"}}}}}]


def ok_text(text="done"):
    return httpx.Response(200, json={"id": "c1", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 42}})


def ok_tool(args='{"cmd": "ls"}', call_id="call_1", sig=None):
    tc = {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": args}}
    if sig:
        tc["extra_content"] = {"google": {"thought_signature": sig}}
    return httpx.Response(200, json={"id": "c2", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [tc]},
         "finish_reason": "tool_calls"}], "usage": {"total_tokens": 7}})


class Upstream:
    """Scripted fake for the three providers; default reply is ok_text()."""

    def __init__(self):
        self.script: dict[str, list] = {"nvidia": [], "openrouter": [], "gemini": []}
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = HOSTS[request.url.host]
        body = json.loads(request.content)
        self.calls.append((name, body))
        queue = self.script[name]
        resp = queue.pop(0) if queue else ok_text(f"from {name}")
        if isinstance(resp, Exception):
            raise resp
        return resp

    def called(self):
        return [c[0] for c in self.calls]


def cfg(tmp_path, **over):
    base = {
        "public_model": "hermes-auto", "advertised_context": 200000, "overall_deadline_s": 60,
        "sticky_seconds": 600, "breaker_threshold": 3, "state_file": str(tmp_path / "state.json"),
        "providers": [
            {"name": "nvidia", "base_url": "https://integrate.api.nvidia.com/v1", "model": "nv-model",
             "key_env": "NV_KEY", "context_window": 131072},
            {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "or-model",
             "key_env": "OR_KEY", "context_window": 65536, "extra_body": {"provider": {"require_parameters": True}}},
            {"name": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
             "model": "gemini-x", "key_env": "GM_KEY", "context_window": 1048576,
             "gemini_thought_signatures": True, "strip_schema_keys": ["$schema"]},
        ],
    }
    base.update(over)
    return base


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ROUTER_API_KEY", KEY)
    monkeypatch.delenv("ROUTER_ENABLE_CHAOS", raising=False)
    for k in ("NV_KEY", "OR_KEY", "GM_KEY"):
        monkeypatch.setenv(k, "upstream-key")


def make(tmp_path, up, **over):
    app = r.create_app(cfg(tmp_path, **over), transport=httpx.MockTransport(up.handler))
    return TestClient(app), app.state.router


def post(client, messages=None, **extra):
    body = {"model": "hermes-auto", "messages": messages or [
        {"role": "system", "content": "SOUL"}, {"role": "user", "content": "hello"}], **extra}
    return client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {KEY}"})


def msgs(user="hello"):
    return [{"role": "system", "content": "SOUL"}, {"role": "user", "content": user}]


# ─────────────────────────────── tests ──────────────────────────────────

def test_primary_serves_when_healthy(env, tmp_path):
    up = Upstream()
    c, _ = make(tmp_path, up)
    res = post(c)
    assert res.status_code == 200 and res.headers["x-router-provider"] == "nvidia"
    assert up.called() == ["nvidia"]
    assert res.json()["model"] == "nvidia/nv-model"


def test_429_fails_over_and_cools_down_primary(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(429, headers={"retry-after": "120"}, json={})]
    c, router = make(tmp_path, up)
    assert post(c).headers["x-router-provider"] == "openrouter"
    assert 115 < router.health["nvidia"].cooldown_until - time.time() < 140
    up.calls.clear()
    assert post(c, msgs("another session")).headers["x-router-provider"] == "openrouter"
    assert "nvidia" not in up.called()  # skipped without spending a request


def test_three_tiers_in_one_request(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(429, json={})]
    up.script["openrouter"] = [httpx.Response(503, json={})]
    c, _ = make(tmp_path, up)
    res = post(c)
    assert res.headers["x-router-provider"] == "gemini"
    assert up.called() == ["nvidia", "openrouter", "gemini"]


def test_long_context_goes_straight_to_gemini(env, tmp_path):
    up = Upstream()
    c, _ = make(tmp_path, up)
    res = post(c, msgs("x" * 600_000))  # ~190k estimated tokens
    assert res.headers["x-router-provider"] == "gemini"
    assert up.called() == ["gemini"]


def test_malformed_tool_arguments_fall_through(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [ok_tool(args="{not json")]
    up.script["openrouter"] = [ok_tool()]
    c, router = make(tmp_path, up)
    res = post(c, tools=TOOLS)
    assert res.headers["x-router-provider"] == "openrouter"
    assert router.health["nvidia"].last_error.startswith("invalid_output")


def test_tool_call_leaked_into_text_falls_through(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [ok_text('{"name": "terminal", "parameters": {"cmd": "ls"}}')]
    up.script["openrouter"] = [ok_tool()]
    c, _ = make(tmp_path, up)
    assert post(c, tools=TOOLS).headers["x-router-provider"] == "openrouter"


def test_unknown_tool_name_falls_through(env, tmp_path):
    up = Upstream()
    bad = ok_tool()
    payload = bad.json()
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "rm_rf"
    up.script["nvidia"] = [httpx.Response(200, json=payload)]
    up.script["openrouter"] = [ok_tool()]
    c, _ = make(tmp_path, up)
    assert post(c, tools=TOOLS).headers["x-router-provider"] == "openrouter"


def test_context_error_is_not_penalised(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(400, json={"error": {"message": "This model's maximum context length is 131072"}})]
    c, router = make(tmp_path, up)
    assert post(c).headers["x-router-provider"] == "openrouter"
    assert router.health["nvidia"].consecutive_failures == 0
    assert router.health["nvidia"].cooldown_until == 0


def test_all_down_returns_503_with_retry_after(env, tmp_path):
    up = Upstream()
    for name in up.script:
        up.script[name] = [httpx.Response(429, headers={"retry-after": "30"}, json={})]
    c, _ = make(tmp_path, up)
    res = post(c)
    assert res.status_code == 503
    assert res.json()["error"]["type"] == "router_exhausted"
    assert 1 <= int(res.headers["retry-after"]) <= 40


def test_timeouts_and_network_errors_fail_over(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.ReadTimeout("slow")]
    up.script["openrouter"] = [httpx.ConnectError("refused")]
    c, _ = make(tmp_path, up)
    assert post(c).headers["x-router-provider"] == "gemini"


def test_circuit_breaker_opens_after_threshold(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(502, json={})] * 3
    c, router = make(tmp_path, up, sticky_seconds=0)
    for i in range(3):
        post(c, msgs(f"s{i}"))
    assert router.health["nvidia"].cooldown_until > time.time() + 50
    up.calls.clear()
    post(c, msgs("s-next"))
    assert up.called() == ["openrouter"]


def test_stream_is_synthesised_as_sse(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [ok_tool()]
    c, _ = make(tmp_path, up)
    res = post(c, tools=TOOLS, stream=True, stream_options={"include_usage": True})
    assert res.headers["content-type"].startswith("text/event-stream")
    lines = [ln[6:] for ln in res.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    first = json.loads(lines[0])
    tc = first["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "terminal" and json.loads(tc["function"]["arguments"]) == {"cmd": "ls"}
    assert json.loads(lines[1])["choices"][0]["finish_reason"] == "tool_calls"
    assert json.loads(lines[2])["usage"]["total_tokens"] == 7
    assert up.calls[0][1]["stream"] is False  # upstream always buffered


def test_history_hygiene_and_gemini_thought_signatures(env, tmp_path):
    up = Upstream()
    c, _ = make(tmp_path, up, sticky_seconds=0)
    history = [
        *msgs(),
        {"role": "assistant", "content": None, "reasoning_content": "secret chain of thought",
         "tool_calls": [{"id": "call_foreign", "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_foreign", "content": "ok"},
    ]
    post(c, history, tools=TOOLS)
    sent = up.calls[-1][1]
    assert up.calls[-1][0] == "nvidia"
    asst = sent["messages"][2]
    assert "reasoning_content" not in asst and "extra_content" not in asst["tool_calls"][0]
    assert "$schema" in json.dumps(sent["tools"])  # only stripped for gemini

    # Force Gemini: foreign call gets the documented dummy signature, $schema stripped.
    up.script["nvidia"] = [httpx.Response(503, json={})]
    up.script["openrouter"] = [httpx.Response(503, json={})]
    up.script["gemini"] = [ok_tool(call_id="call_g1", sig="SIG1")]
    post(c, history, tools=TOOLS)
    name, sent = up.calls[-1]
    assert name == "gemini"
    assert sent["messages"][2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == \
        r.GEMINI_SKIP_SIGNATURE
    assert "$schema" not in json.dumps(sent["tools"])

    # Gemini's own call comes back later without the signature (Hermes dropped it): re-attached.
    history2 = [*history, {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_g1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_g1", "content": "ok"}]
    up.script["nvidia"] = [httpx.Response(503, json={})]
    up.script["openrouter"] = [httpx.Response(503, json={})]
    post(c, history2, tools=TOOLS)
    sent = up.calls[-1][1]
    assert sent["messages"][4]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG1"


def test_session_sticks_to_fallback_provider(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(503, json={})]  # 1 failure < breaker threshold → no cooldown
    c, router = make(tmp_path, up)
    assert post(c).headers["x-router-provider"] == "openrouter"
    assert router.health["nvidia"].cooldown_until == 0
    up.calls.clear()
    assert post(c).headers["x-router-provider"] == "openrouter"   # same session → stays
    assert up.called() == ["openrouter"]
    up.calls.clear()
    assert post(c, msgs("new task")).headers["x-router-provider"] == "nvidia"  # other session → primary


def test_cooldown_survives_restart(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(429, headers={"retry-after": "600"}, json={})]
    c, _ = make(tmp_path, up)
    post(c)
    c.close()
    up2 = Upstream()
    c2, router2 = make(tmp_path, up2)
    assert router2.health["nvidia"].cooldown_until > time.time() + 500
    post(c2, msgs("after restart"))
    assert up2.called() == ["openrouter"]


def test_local_rpm_bucket_spills_without_calling(env, tmp_path):
    up = Upstream()
    conf = cfg(tmp_path)
    conf["providers"][0]["rpm"] = 1
    c = TestClient(r.create_app(conf, transport=httpx.MockTransport(up.handler)))
    post(c, msgs("a"))
    post(c, msgs("b"))
    assert up.called() == ["nvidia", "openrouter"]


def test_daily_token_budget(env, tmp_path):
    up = Upstream()
    conf = cfg(tmp_path)
    conf["providers"][0]["daily_token_budget"] = 40   # first reply uses 42
    c = TestClient(r.create_app(conf, transport=httpx.MockTransport(up.handler)))
    post(c, msgs("a"))
    post(c, msgs("b"))
    assert up.called() == ["nvidia", "openrouter"]


def test_auth_models_and_health(env, tmp_path):
    up = Upstream()
    c, _ = make(tmp_path, up)
    assert c.post("/v1/chat/completions", json={"messages": []}).status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    m = c.get("/v1/models", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert m["data"][0]["context_length"] == 200000
    h = c.get("/healthz", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert [p["name"] for p in h["providers"]] == ["nvidia", "openrouter", "gemini"]
    assert "NV_KEY" not in json.dumps(h)


def test_refuses_to_start_without_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTER_API_KEY", "short")
    with pytest.raises(RuntimeError):
        r.create_app(cfg(tmp_path))


def test_chaos_drill(env, tmp_path, monkeypatch):
    chaos = tmp_path / "chaos.json"
    chaos.write_text(json.dumps({"nvidia": 429, "openrouter": "malformed"}))
    monkeypatch.setenv("ROUTER_ENABLE_CHAOS", "1")
    monkeypatch.setenv("ROUTER_CHAOS_FILE", str(chaos))
    up = Upstream()
    c, router = make(tmp_path, up)
    res = post(c, tools=TOOLS)
    assert res.headers["x-router-provider"] == "gemini"
    assert up.called() == ["gemini"]  # faults injected locally, never sent upstream
    assert router.health["nvidia"].cooldown_until > time.time()


def test_retry_after_parsing():
    now = time.time()
    assert r._retry_after(httpx.Response(429, headers={"retry-after": "17"})) == 17
    v = r._retry_after(httpx.Response(429, headers={"x-ratelimit-reset": str(int((now + 30) * 1000))}))
    assert 25 < v <= 31
    assert r._retry_after(httpx.Response(429)) is None


def test_missing_key_is_skipped_not_penalised(env, tmp_path, monkeypatch):
    monkeypatch.setenv("NV_KEY", "")
    up = Upstream()
    c, router = make(tmp_path, up)
    res = post(c)
    assert res.headers["x-router-provider"] == "openrouter"
    assert up.called() == ["openrouter"]
    assert router.health["nvidia"].consecutive_failures == 0


def test_upstream_receives_bearer_key_and_extra_body(env, tmp_path):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok_text()

    c = TestClient(r.create_app(cfg(tmp_path), transport=httpx.MockTransport(handler)))
    post(c, msgs("k"), max_tokens=999999)
    assert seen["auth"] == "Bearer upstream-key"
    assert seen["body"]["model"] == "nv-model" and seen["body"]["max_tokens"] == 8192


GEMINI_DAILY_429 = [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                               "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20. "
                                          "Please retry in 8h.",
                               "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                            "retryDelay": "28800s"}]}}]


def test_gemini_daily_quota_benches_until_reset(env, tmp_path):
    up = Upstream()
    up.script["nvidia"] = [httpx.Response(429, json=GEMINI_DAILY_429)]
    c, router = make(tmp_path, up)
    assert post(c).headers["x-router-provider"] == "openrouter"
    left = router.health["nvidia"].cooldown_until - time.time()
    assert 28000 < left < 28800 * 1.16       # ~8 h, not the 1 h default cap


def test_retry_delay_parsing_variants():
    mk = lambda body: httpx.Response(429, json=body)  # noqa: E731
    assert r._retry_after(mk({"error": {"message": "Please retry in 51.07s."}})) == pytest.approx(51.07)
    assert r._retry_after(mk({"error": {"message": "Please retry in 1h30m."}})) == 5400
    assert r._retry_after(mk({"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "8h0m0s"}]}})) == 28800
    assert r._retry_after(mk({"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "500ms"}]}})) == 0.5
    assert r._retry_after(httpx.Response(429, text="not json")) is None


def test_stale_if_error_serves_labelled_cache_and_survives_restart(env, tmp_path):
    over = {"stale_if_error_s": 3600, "stale_cache_dir": str(tmp_path / "cache")}
    up = Upstream()
    c, _ = make(tmp_path, up, **over)
    assert post(c, msgs("demo question")).status_code == 200          # warm the cache

    def all_429(request):
        return httpx.Response(429, headers={"retry-after": "120"}, json={})

    c2 = TestClient(r.create_app(cfg(tmp_path, **over), transport=httpx.MockTransport(all_429)))  # "restart"
    res = post(c2, msgs("demo question"))
    assert res.status_code == 200
    assert res.headers["x-router-cache"] == "stale" and res.headers["x-router-provider"] == "cache:nvidia"
    assert res.json()["choices"][0]["message"]["content"] == "from nvidia"
    assert post(c2, msgs("never asked before")).status_code == 503       # no cache → honest 503


def test_stale_cache_off_by_default_and_expires(env, tmp_path):
    up = Upstream()
    c, router = make(tmp_path, up)
    post(c, msgs("q"))
    assert router.cache_get({"messages": msgs("q")}) is None           # default: disabled
    router.stale_s = 10
    router.cache_put({"messages": msgs("q")}, {"choices": []}, "nvidia")
    router._cache[router.cache_key({"messages": msgs("q")})]["ts"] -= 60
    assert router.cache_get({"messages": msgs("q")}) is None           # older than stale_if_error_s
