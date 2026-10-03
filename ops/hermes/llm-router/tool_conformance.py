#!/usr/bin/env python3
"""
tool_conformance.py — measure how reliably each candidate model does OpenAI-style tool calling.

Calls each provider DIRECTLY (not through the router) with six agent-realistic scenarios
and grades the raw responses. Use it to choose Tier-1/Tier-2 models and re-run it after
any catalog change.

  python3 tool_conformance.py --config providers.yaml --runs 5
  python3 tool_conformance.py --provider nvidia --model meta/llama-3.3-70b-instruct \
      --model openai/gpt-oss-120b --model nvidia/nemotron-3-super-120b-a12b --runs 5

Keys come from the env vars named in providers.yaml. Output: a table + JSON (--json).
Free tiers are rate-limited: the script paces itself by the provider's `rpm`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Any

import httpx
import yaml


TERMINAL = {"type": "function", "function": {
    "name": "terminal", "description": "Run a shell command on the server.",
    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}}
READ_FILE = {"type": "function", "function": {
    "name": "read_file", "description": "Read a text file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
WEB_SEARCH = {"type": "function", "function": {
    "name": "web_search", "description": "Search the web.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
GTM_TAG = {"type": "function", "function": {
    "name": "gtm_create_tag", "description": "Create a GTM tag.",
    "parameters": {"type": "object", "required": ["name", "type", "trigger_ids", "params"], "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["html", "gaawe", "googtag"]},
        "trigger_ids": {"type": "array", "items": {"type": "string"}},
        "params": {"type": "object", "required": ["event_name", "currency"], "properties": {
            "event_name": {"type": "string"}, "currency": {"type": "string", "enum": ["EGP", "USD", "SAR"]}}}}}}}

SYSTEM = {"role": "system", "content": "You are an autonomous agent. Use tools when they are needed; "
                                       "answer directly when they are not."}
ARABIC = re.compile(r"[؀-ۿ]")


def scenarios() -> list[dict[str, Any]]:
    return [
        {"id": "single_call", "tools": [TERMINAL, READ_FILE],
         "messages": [SYSTEM, {"role": "user", "content": "List the files in /var/log."}],
         "expect": {"calls": ["terminal"]}},
        {"id": "nested_enum_args", "tools": [GTM_TAG, TERMINAL],
         "messages": [SYSTEM, {"role": "user", "content":
                      "Create a GA4 event tag (type gaawe) named 'GA4 - purchase' on trigger IDs 10 and 11, "
                      "event_name purchase, currency EGP."}],
         "expect": {"calls": ["gtm_create_tag"],
                    "check": lambda a: a["type"] == "gaawe" and set(a["trigger_ids"]) == {"10", "11"}
                    and a["params"]["currency"] == "EGP"}},
        {"id": "arabic_args", "tools": [WEB_SEARCH, TERMINAL],
         "messages": [SYSTEM, {"role": "user", "content": "ابحث في الويب عن: تتبع المشتريات في شوبيفاي"}],
         "expect": {"calls": ["web_search"], "check": lambda a: bool(ARABIC.search(a.get("query", "")))}},
        {"id": "parallel_calls", "tools": [READ_FILE, TERMINAL],
         "messages": [SYSTEM, {"role": "user", "content": "Read both /srv/data/a.txt and /srv/data/b.txt."}],
         "expect": {"calls": ["read_file"], "min_calls": 1, "bonus_parallel": 2}},
        {"id": "tool_result_followup", "tools": [TERMINAL],
         "messages": [SYSTEM, {"role": "user", "content": "How much free disk space is there?"},
                      {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                       "function": {"name": "terminal", "arguments": "{\"cmd\": \"df -h /\"}"}}]},
                      {"role": "tool", "tool_call_id": "call_1",
                       "content": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 13G 87G 13% /"}],
         "expect": {"text": True, "check_text": lambda t: "87" in t}},
        {"id": "no_tool_needed", "tools": [TERMINAL, WEB_SEARCH],
         "messages": [SYSTEM, {"role": "user", "content": "What is 17 + 25? Answer with the number only."}],
         "expect": {"text": True, "check_text": lambda t: "42" in t}},
    ]


# ── minimal JSON-schema check (type / required / enum / properties / items) ──
def schema_ok(value: Any, schema: dict[str, Any]) -> bool:
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t in types and not isinstance(value, types[t]):
        return False
    if t in ("number", "integer") and (isinstance(value, bool) or not isinstance(value, int | float)):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if t == "object":
        if any(k not in value for k in schema.get("required", [])):
            return False
        props = schema.get("properties", {})
        return all(schema_ok(v, props[k]) for k, v in value.items() if k in props)
    if t == "array" and "items" in schema:
        return all(schema_ok(v, schema["items"]) for v in value)
    return True


def grade(sc: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
    """Return (verdict, detail). verdict: pass | fail | malformed."""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "malformed", "no choices/message"
    calls = msg.get("tool_calls") or []
    text = msg.get("content") or ""
    exp = sc["expect"]
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in sc["tools"]}
    parsed = []
    for tc in calls:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        try:
            args = args if isinstance(args, dict) else json.loads(args or "{}")
        except (TypeError, ValueError):
            return "malformed", f"bad JSON args for {fn.get('name')}"
        if fn.get("name") not in schemas:
            return "malformed", f"unknown tool {fn.get('name')!r}"
        if not schema_ok(args, schemas[fn["name"]]):
            return "malformed", f"args violate schema: {json.dumps(args, ensure_ascii=False)[:120]}"
        parsed.append((fn["name"], args))
    if exp.get("text"):
        if calls:
            return "fail", "called a tool when a direct answer was expected"
        return ("pass", "") if exp["check_text"](text) else ("fail", f"wrong answer: {text[:80]!r}")
    if not calls:
        leaked = "leaked tool call in text" if re.search(r"\"name\"\s*:|<tool_call>", text) else "no tool call"
        return "fail", leaked
    if [n for n, _ in parsed][:1] != exp["calls"][:1]:
        return "fail", f"called {[n for n, _ in parsed]}"
    if "check" in exp:
        try:
            if not exp["check"](parsed[0][1]):
                return "fail", f"wrong args: {json.dumps(parsed[0][1], ensure_ascii=False)[:120]}"
        except (KeyError, TypeError):
            return "fail", "missing nested args"
    detail = "parallel" if len(parsed) >= exp.get("bonus_parallel", 99) else ""
    return "pass", detail


async def run_one(client: httpx.AsyncClient, p: dict[str, Any], model: str, sc: dict[str, Any]) -> dict[str, Any]:
    body = {"model": model, "messages": sc["messages"], "tools": sc["tools"], "tool_choice": "auto",
            "temperature": 0.2, "max_tokens": 1024, **(p.get("extra_body") or {})}
    headers = {"Authorization": f"Bearer {os.environ.get(p['key_env'], '')}", **(p.get("extra_headers") or {})}
    t0 = time.monotonic()
    try:
        r = await client.post(p["base_url"].rstrip("/") + "/chat/completions", json=body, headers=headers,
                              timeout=httpx.Timeout(p.get("timeout_s", 120), connect=10))
    except httpx.HTTPError as exc:
        return {"verdict": "error", "detail": type(exc).__name__, "ms": int((time.monotonic() - t0) * 1000)}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"verdict": "error", "detail": f"HTTP {r.status_code}: {r.text[:120]}", "ms": ms}
    try:
        verdict, detail = grade(sc, r.json())
    except ValueError:
        verdict, detail = "malformed", "non-JSON body"
    return {"verdict": verdict, "detail": detail, "ms": ms}


async def bench(providers: list[dict[str, Any]], models: list[str], runs: int,
                transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    results = []
    async with httpx.AsyncClient(transport=transport) as client:
        for p in providers:
            pace = 60.0 / p["rpm"] if p.get("rpm") else 0.0
            for model in models or [p["model"]]:
                for sc in scenarios():
                    for _ in range(runs):
                        res = await run_one(client, p, model, sc)
                        results.append({"provider": p["name"], "model": model, "scenario": sc["id"], **res})
                        if pace:
                            await asyncio.sleep(pace)
    return results


def summarise(results: list[dict[str, Any]]) -> str:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in results:
        k = (r["provider"], r["model"])
        s = rows.setdefault(k, {"n": 0, "pass": 0, "malformed": 0, "error": 0, "ms": [], "fails": {}})
        s["n"] += 1
        s[r["verdict"]] = s.get(r["verdict"], 0) + 1
        s["ms"].append(r["ms"])
        if r["verdict"] != "pass":
            s["fails"][r["scenario"]] = s["fails"].get(r["scenario"], 0) + 1
    out = ["| provider | model | pass % | malformed | errors | p50 ms | failing scenarios |", "|---|---|---|---|---|---|---|"]
    for (prov, model), s in sorted(rows.items(), key=lambda kv: -kv[1]["pass"] / max(1, kv[1]["n"] - kv[1]["error"])):
        graded = max(1, s["n"] - s["error"])
        p50 = sorted(s["ms"])[len(s["ms"]) // 2]
        fails = ", ".join(f"{k} x{v}" for k, v in s["fails"].items()) or "—"
        out.append(f"| {prov} | {model} | {100 * s['pass'] / graded:.0f} | {s['malformed']} | {s['error']} | {p50} | {fails} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tool-calling conformance benchmark.")
    ap.add_argument("--config", default="providers.yaml")
    ap.add_argument("--provider", action="append", help="limit to provider name(s)")
    ap.add_argument("--model", action="append", default=[], help="override model(s) for the selected provider")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        providers = yaml.safe_load(fh)["providers"]
    if args.provider:
        providers = [p for p in providers if p["name"] in args.provider]
    if args.model and len(providers) != 1:
        sys.stderr.write("--model requires exactly one --provider\n")
        return 2
    results = asyncio.run(bench(providers, args.model, args.runs))
    sys.stdout.write(summarise(results) + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
