"""Grader tests for tool_conformance.py (no network)."""

import asyncio
import json
import sys
from pathlib import Path

import httpx


sys.path.insert(0, str(Path(__file__).parent.parent))
import tool_conformance as tc


SC = {s["id"]: s for s in tc.scenarios()}


def reply(calls=None, text=None):
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                              "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
                             for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg}]}


def test_nested_enum_args_pass_and_schema_violation():
    good = {"name": "GA4 - purchase", "type": "gaawe", "trigger_ids": ["10", "11"],
            "params": {"event_name": "purchase", "currency": "EGP"}}
    assert tc.grade(SC["nested_enum_args"], reply([("gtm_create_tag", good)]))[0] == "pass"
    bad = {**good, "params": {"event_name": "purchase", "currency": "EGY"}}
    assert tc.grade(SC["nested_enum_args"], reply([("gtm_create_tag", bad)]))[0] == "malformed"
    ints = {**good, "trigger_ids": [10, 11]}
    assert tc.grade(SC["nested_enum_args"], reply([("gtm_create_tag", ints)]))[0] == "malformed"


def test_malformed_leaked_and_overcalling():
    assert tc.grade(SC["single_call"], reply([("terminal", "{oops")]))[0] == "malformed"
    v, d = tc.grade(SC["single_call"], reply(text='{"name": "terminal", "arguments": {"cmd": "ls"}}'))
    assert v == "fail" and "leaked" in d
    assert tc.grade(SC["no_tool_needed"], reply([("terminal", {"cmd": "echo 42"})]))[0] == "fail"
    assert tc.grade(SC["no_tool_needed"], reply(text="42"))[0] == "pass"


def test_arabic_and_parallel():
    assert tc.grade(SC["arabic_args"], reply([("web_search", {"query": "تتبع المشتريات"})]))[0] == "pass"
    assert tc.grade(SC["arabic_args"], reply([("web_search", {"query": "shopify tracking"})]))[0] == "fail"
    v, d = tc.grade(SC["parallel_calls"], reply([("read_file", {"path": "/srv/data/a.txt"}), ("read_file", {"path": "/srv/data/b.txt"})]))
    assert (v, d) == ("pass", "parallel")


def test_bench_end_to_end_with_mock(monkeypatch):
    monkeypatch.setenv("K", "x")

    def handler(request):
        body = json.loads(request.content)
        last = body["messages"][-1]
        if last["role"] == "tool":
            return httpx.Response(200, json=reply(text="87G free"))
        if "17 + 25" in str(last["content"]):
            return httpx.Response(200, json=reply(text="42"))
        return httpx.Response(200, json=reply([("terminal", {"cmd": "ls /var/log"})]))

    prov = [{"name": "mock", "base_url": "https://mock.example/v1", "model": "m", "key_env": "K"}]
    res = asyncio.run(tc.bench(prov, [], 1, transport=httpx.MockTransport(handler)))
    by = {r["scenario"]: r["verdict"] for r in res}
    assert by["single_call"] == "pass" and by["tool_result_followup"] == "pass" and by["no_tool_needed"] == "pass"
    assert by["nested_enum_args"] == "fail"   # called terminal instead of gtm_create_tag
    assert "| mock | m |" in tc.summarise(res)
