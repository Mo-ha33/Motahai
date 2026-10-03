"""Tests for soul_router_client against a real local HTTP server (stdlib)."""

import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))
import soul_router_client as c


SEEN: dict = {}
MODE = {"status": 200, "headers": {"x-router-provider": "nvidia"}}


class FakeRouter(BaseHTTPRequestHandler):
    def do_POST(self):
        SEEN["auth"] = self.headers.get("Authorization")
        SEEN["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = (json.dumps({"model": "nvidia/meta/llama-3.3-70b-instruct",
                               "choices": [{"message": {"role": "assistant", "content": "VERDICT"}}]})
                   if MODE["status"] == 200 else json.dumps({"error": {"type": "router_exhausted"}}))
        self.send_response(MODE["status"])
        for k, v in MODE["headers"].items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def router(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (tmp_path / "SOUL.md").write_text("# SOUL\nYou are Hermes.", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("- D-002: purchase_<order_id>", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_ROUTER_URL", f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
    monkeypatch.setenv("HERMES_ROUTER_KEY", "rk")
    c._cache["key"] = None
    MODE.update(status=200, headers={"x-router-provider": "nvidia"})
    yield tmp_path
    srv.shutdown()


def test_soul_and_memory_sent_as_system_prompt_with_provenance(router):
    r = c.consult("Q?", context="ctx")
    system = SEEN["body"]["messages"][0]
    assert system["role"] == "system" and "You are Hermes." in system["content"] and "D-002" in system["content"]
    assert SEEN["body"]["messages"][1]["content"] == "Q?\n\n## Context\nctx"
    assert SEEN["auth"] == "Bearer rk"
    soul_sha = hashlib.sha256((router / "SOUL.md").read_bytes()).hexdigest()
    assert r["soul_sha256"] == soul_sha and soul_sha[:16] in r["provenance"]
    assert r["text"] == "VERDICT" and r["provider"] == "nvidia"


def test_soul_change_is_picked_up_without_restart(router):
    c.consult("Q")
    (router / "SOUL.md").write_text("# SOUL v2\nNew rules, longer file.", encoding="utf-8")
    c.consult("Q")
    assert "New rules" in SEEN["body"]["messages"][0]["content"]


def test_stale_cache_is_labelled(router):
    MODE["headers"] = {"x-router-provider": "cache:nvidia", "x-router-cache": "stale"}
    assert "(stale-cache)" in c.consult("Q")["provenance"]


def test_exhausted_and_unavailable_are_distinct(router, monkeypatch):
    MODE.update(status=503, headers={})
    with pytest.raises(c.RouterExhaustedError):
        c.consult("Q")
    monkeypatch.setenv("HERMES_ROUTER_URL", "http://127.0.0.1:1/v1/chat/completions")
    with pytest.raises(c.RouterUnavailableError):
        c.consult("Q")


def test_refuses_non_local_router(router, monkeypatch):
    monkeypatch.setenv("HERMES_ROUTER_URL", "https://evil.example/v1/chat/completions")
    with pytest.raises(c.RouterUnavailableError):
        c.consult("Q")
