"""
hermes-llm-router — loopback OpenAI-compatible failover router over configurable tiers
(default order: NVIDIA NIM → OpenRouter → Gemini).

Hermes points at this as its single primary provider. Per request the router:
  1. estimates prompt size and skips providers whose context window is too small
     (so a long conversation goes straight to Gemini instead of failing on NVIDIA first)
  2. skips providers that are cooling down (429 / 402 / auth / circuit breaker) or
     whose local RPM bucket is empty, without spending a request on them
  3. calls upstream NON-streaming, validates the result (well-formed tool calls,
     known tool names, non-empty), and falls through to the next tier on any failure
  4. re-emits the answer as SSE if Hermes asked for a stream

Cross-provider hygiene: strips reasoning fields before forwarding history, caches
Gemini thought signatures by tool-call id and re-attaches them (a dummy for calls
another model made), and keeps a session on its fallback provider for a while so a
tool loop doesn't flip between models (only onto providers marked `sticky`, never the
scarcest daily quotas). Gemini daily-quota 429s bench a provider until Pacific midnight,
and a per-provider daily request budget stops the router before Google's 429 does.

Health (cooldowns, failure counts, daily tokens) is persisted to a JSON file, so a
restart does not hammer a provider that is still rate-limited.

Run:  ROUTER_API_KEY=... uvicorn router:app --host 127.0.0.1 --port 47311
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import email.utils
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


log = logging.getLogger("hermes-router")

CONTEXT_ERROR = re.compile(
    r"context.length|context_length|maximum context|too many tokens|prompt is too long|"
    r"input is too long|token limit|exceeds the (?:model's )?(?:maximum|context)",
    re.I,
)
# Tool call leaked into plain text (common Llama/Qwen failure on weak tool parsers).
LEAKED_TOOL_CALL = re.compile(r"^\s*(?:<tool_call>|<\|python_tag\|>|\{\s*\"(?:name|function)\"\s*:)")
REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_details", "thinking")
GEMINI_SKIP_SIGNATURE = "skip_thought_signature_validator"
PER_DAY = re.compile(r"per.?day", re.I)
# Outcomes that mean "this provider is just rate-limited", for the exhausted-kind header.
RATE_LIMITED_OUTCOMES = {"rate_limited", "rate_limited:daily", "skipped:cooldown", "skipped:daily_requests",
                         "skipped:local_rpm"}


def _load_pacific() -> tzinfo:
    try:
        return ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:          # Windows without tzdata: fixed UTC-8 (off by 1 h in DST, harmless)
        return timezone(timedelta(hours=-8))


PACIFIC = _load_pacific()


def _pacific_day() -> str:
    """Gemini free-tier quotas reset at midnight America/Los_Angeles."""
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def _seconds_to_pacific_reset() -> float:
    now = datetime.now(PACIFIC)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp() - now.timestamp() + 60.0


async def _sleep(seconds: float) -> None:      # indirection so tests can skip real waiting
    await asyncio.sleep(seconds)


# ─────────────────────────────── config ─────────────────────────────────

@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    key_env: str
    context_window: int
    max_output_tokens: int = 8192
    supports_tools: bool = True
    rpm: int = 0                       # local pre-emptive limit; 0 = unlimited
    max_concurrency: int = 4
    connect_timeout_s: float = 5.0
    timeout_s: float = 120.0
    daily_token_budget: int = 0        # 0 = unlimited
    extra_body: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    strip_schema_keys: list[str] = field(default_factory=list)
    gemini_thought_signatures: bool = False
    daily_request_budget: int = 0      # 0 = unlimited; set to the provider's RPD minus headroom
    sticky: bool = True                # may a session stay pinned here after failover?


@dataclass
class Health:
    cooldown_until: float = 0.0        # wall clock, survives restarts
    consecutive_failures: int = 0
    last_error: str = ""
    tokens_day: str = ""
    tokens_used: int = 0
    requests_day: str = ""             # America/Los_Angeles date (Gemini's quota reset)
    requests_used: int = 0
    last_kind: str = ""                # kind of the last failure ("" after a success)


@dataclass
class Attempt:
    provider: str
    outcome: str                       # ok | skipped:<why> | rate_limited | ...
    status: int | None = None
    latency_ms: int = 0
    detail: str = ""


class ExhaustedError(Exception):
    def __init__(self, attempts: list[Attempt], retry_after: int):
        super().__init__("all providers failed")
        self.attempts = attempts
        self.retry_after = retry_after


class _Bucket:
    def __init__(self, rpm: int):
        self.rpm = rpm
        self.tokens = float(rpm)
        self.updated = time.monotonic()

    def peek(self) -> float:
        """Current tokens, refill-computed without consuming."""
        return min(self.rpm, self.tokens + (time.monotonic() - self.updated) * self.rpm / 60.0)

    def seconds_until_token(self) -> float:
        if self.rpm <= 0 or (have := self.peek()) >= 1:
            return 0.0
        return (1 - have) * 60.0 / self.rpm

    def try_take(self) -> bool:
        if self.rpm <= 0:
            return True
        self.tokens = self.peek()
        self.updated = time.monotonic()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


# ─────────────────────────────── router ─────────────────────────────────

class Router:
    def __init__(self, cfg: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.providers = [Provider(**p) for p in cfg["providers"]]
        self.public_model = cfg.get("public_model", "hermes-auto")
        self.advertised_context = int(cfg.get("advertised_context", max(p.context_window for p in self.providers)))
        self.deadline_s = float(cfg.get("overall_deadline_s", 270))
        self.sticky_s = float(cfg.get("sticky_seconds", 600))
        self.local_wait_max_s = float(cfg.get("local_wait_max_s", 8.0))
        self.local_retry_rounds = int(cfg.get("local_retry_rounds", 2))
        self.breaker_threshold = int(cfg.get("breaker_threshold", 3))
        self.bytes_per_token = float(cfg.get("bytes_per_token", 3.2))
        self.state_path = Path(cfg["state_file"]) if cfg.get("state_file") else None
        self.by_name = {p.name: p for p in self.providers}
        self.health: dict[str, Health] = {p.name: Health() for p in self.providers}
        self.buckets = {p.name: _Bucket(p.rpm) for p in self.providers}
        self.inflight: dict[str, int] = {p.name: 0 for p in self.providers}
        self.sticky: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.signatures: OrderedDict[str, str] = OrderedDict()
        self._last_save = 0.0
        # Stale-if-error: when EVERY provider is down/rate-limited, answer an identical earlier
        # request from cache (labelled x-router-cache: stale). Off unless stale_if_error_s > 0.
        self.stale_s = float(cfg.get("stale_if_error_s", 0))
        self.cache_dir = Path(cfg["stale_cache_dir"]) if cfg.get("stale_cache_dir") else None
        self.cache_max = int(cfg.get("stale_cache_max", 500))
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cache_puts = 0
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self._load_state()

    # ── persistence ──
    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Health)}   # old state lacks new keys; newer state may have extras
            for name, h in raw.get("health", {}).items():
                if name in self.health:
                    self.health[name] = Health(**{k: v for k, v in h.items() if k in known})
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("router state unreadable, starting clean")

    def _save_state(self, force: bool = False) -> None:
        if not self.state_path or (not force and time.monotonic() - self._last_save < 30):
            return
        self._last_save = time.monotonic()
        tmp = self.state_path.with_suffix(".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"health": {k: asdict(v) for k, v in self.health.items()}}), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            log.exception("could not persist router state")

    # ── sizing & routing ──
    def estimate_tokens(self, body: dict[str, Any]) -> int:
        """Deliberately pessimistic (UTF-8 bytes / 3.2): Arabic is 2 bytes/char and tokenises poorly."""
        size = len(json.dumps(body.get("messages", []), ensure_ascii=False).encode())
        size += len(json.dumps(body.get("tools", []), ensure_ascii=False).encode())
        return int(size / self.bytes_per_token) + 8 * len(body.get("messages", []))

    @staticmethod
    def session_key(messages: list[dict[str, Any]]) -> str:
        first = {}
        for m in messages:
            if m.get("role") in ("system", "user") and m.get("role") not in first:
                first[m["role"]] = m.get("content")
        return hashlib.sha256(json.dumps(first, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def _output_budget(self, p: Provider, body: dict[str, Any]) -> int:
        asked = body.get("max_tokens") or body.get("max_completion_tokens") or p.max_output_tokens
        return min(int(asked), p.max_output_tokens)

    def plan(self, body: dict[str, Any], session: str) -> tuple[list[Provider], list[Attempt]]:
        est = self.estimate_tokens(body)
        has_tools = bool(body.get("tools"))
        now = time.time()
        order = list(self.providers)
        stick = self.sticky.get(session)
        if stick and stick[1] > now and (sp := self.by_name.get(stick[0])) and sp.sticky:
            order.sort(key=lambda p: p.name != stick[0])
        eligible, skipped = [], []
        for p in order:
            h = self.health[p.name]
            self._roll_day(h)
            if not os.environ.get(p.key_env, "").strip():
                skipped.append(Attempt(p.name, "skipped:no_key", detail=f"{p.key_env} is not set"))
            elif est + self._output_budget(p, body) > p.context_window * 0.95:
                skipped.append(Attempt(p.name, "skipped:context", detail=f"est {est} tok > window {p.context_window}"))
            elif has_tools and not p.supports_tools:
                skipped.append(Attempt(p.name, "skipped:no_tools"))
            elif h.cooldown_until > now:
                skipped.append(Attempt(p.name, "skipped:cooldown", detail=f"{int(h.cooldown_until - now)}s left"))
            elif p.daily_token_budget and h.tokens_used >= p.daily_token_budget:
                skipped.append(Attempt(p.name, "skipped:daily_budget"))
            elif p.daily_request_budget and h.requests_used >= p.daily_request_budget:
                skipped.append(Attempt(p.name, "skipped:daily_requests", detail=f"{h.requests_used} used today"))
            else:
                eligible.append(p)
        return eligible, skipped

    @staticmethod
    def _roll_day(h: Health) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if h.tokens_day != today:
            h.tokens_day, h.tokens_used = today, 0
        if h.requests_day != (pday := _pacific_day()):
            h.requests_day, h.requests_used = pday, 0

    # ── stale-if-error cache ──
    @staticmethod
    def cache_key(body: dict[str, Any]) -> str:
        material = {k: body.get(k) for k in ("messages", "tools", "tool_choice", "response_format")}
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()

    def cache_put(self, body: dict[str, Any], data: dict[str, Any], provider: str) -> None:
        if self.stale_s <= 0:
            return
        key = self.cache_key(body)
        entry = {"ts": time.time(), "provider": provider, "data": copy.deepcopy(data)}
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max:
            self._cache.popitem(last=False)
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.cache_dir / f"{key}.tmp"
                tmp.write_text(json.dumps(entry), encoding="utf-8")
                os.replace(tmp, self.cache_dir / f"{key}.json")
                self._cache_puts += 1
                if self._cache_puts % 50 == 0:
                    files = sorted(self.cache_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
                    for f in files[: max(0, len(files) - self.cache_max)]:
                        f.unlink(missing_ok=True)
            except OSError:
                log.exception("could not write stale cache entry")

    def cache_get(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if self.stale_s <= 0:
            return None
        key = self.cache_key(body)
        entry = self._cache.get(key)
        if entry is None and self.cache_dir:
            with contextlib.suppress(OSError, ValueError):
                entry = json.loads((self.cache_dir / f"{key}.json").read_text(encoding="utf-8"))
        if not entry or time.time() - float(entry.get("ts", 0)) > self.stale_s:
            return None
        return entry

    # ── payload shaping ──
    def build_payload(self, p: Provider, body: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in body.items() if k not in ("stream", "stream_options", "model", "max_completion_tokens")}
        payload["model"] = p.model
        payload["stream"] = False
        payload["max_tokens"] = self._output_budget(p, body)
        payload["messages"] = self._shape_messages(p, body.get("messages", []))
        if body.get("tools"):
            payload["tools"] = _strip_keys(copy.deepcopy(body["tools"]), set(p.strip_schema_keys))
        payload.update(copy.deepcopy(p.extra_body))
        return payload

    def _shape_messages(self, p: Provider, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = copy.deepcopy(messages)
        for m in out:
            if m.get("role") != "assistant":
                continue
            for f in REASONING_FIELDS:  # other providers reject or mis-handle foreign reasoning
                m.pop(f, None)
            self._capture_signatures(m)         # signatures Hermes echoed back; read before popping
            for tc in m.get("tool_calls") or []:
                incoming = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
                tc.pop("extra_content", None)
                if p.gemini_thought_signatures:    # incoming (Gemini's own) > cache > documented dummy
                    sig = incoming or self.signatures.get(tc.get("id", "")) or GEMINI_SKIP_SIGNATURE
                    tc["extra_content"] = {"google": {"thought_signature": sig}}
        return out

    def _capture_signatures(self, msg: dict[str, Any]) -> None:
        for tc in msg.get("tool_calls") or []:
            sig = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
            if sig and sig != GEMINI_SKIP_SIGNATURE and tc.get("id"):
                self.signatures[tc["id"]] = sig
                self.signatures.move_to_end(tc["id"])
                while len(self.signatures) > 5000:
                    self.signatures.popitem(last=False)

    # ── validation ──
    @staticmethod
    def validate(data: dict[str, Any], tool_names: set[str]) -> str | None:
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            return "no choices/message"
        msg = choices[0]["message"]
        calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""
        if not calls:
            if tool_names and isinstance(content, str) and LEAKED_TOOL_CALL.match(content):
                return "tool call leaked into content"
            if not (isinstance(content, str) and content.strip()) and not isinstance(content, list):
                return "empty completion"
            return None
        for tc in calls:
            fn = tc.get("function") or {}
            if tool_names and fn.get("name") not in tool_names:
                return f"unknown tool {fn.get('name')!r}"
            if not tc.get("id"):                # some providers omit ids; Hermes needs them
                tc["id"] = "call_" + uuid.uuid4().hex[:24]
            tc.setdefault("type", "function")
            args = fn.get("arguments", "{}")
            if isinstance(args, dict):          # some providers return an object; normalise
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
                continue
            try:
                if not isinstance(json.loads(args or "{}"), dict):
                    return "tool arguments are not a JSON object"
            except (TypeError, ValueError):
                return "malformed tool arguments"
        return None

    # ── health bookkeeping ──
    def _fail(self, p: Provider, kind: str, retry_after: float | None, detail: str) -> None:
        h = self.health[p.name]
        h.consecutive_failures += 1
        h.last_error = f"{kind}: {detail}"[:300]
        h.last_kind = kind
        n = h.consecutive_failures
        if kind == "daily_quota":                  # exhausted daily quota: bench until the Pacific reset
            cd = _seconds_to_pacific_reset()
        elif kind == "rate_limited":
            cd = retry_after if retry_after is not None else min(30 * 2 ** (n - 1), 900)
        elif kind == "payment":
            cd = 3600
        elif kind == "auth":
            cd = 1800
            log.error("provider %s rejected credentials — check its key", p.name)
        elif n >= self.breaker_threshold:          # 5xx / timeout / invalid output
            cd = min(60 * 2 ** (n - self.breaker_threshold), 900)
        else:
            cd = 0
        if cd:
            # A provider-declared window (e.g. Gemini free tier: "retry in 8h") is honoured up to
            # 24 h, so an exhausted daily quota isn't re-probed every few minutes.
            declared = kind == "daily_quota" or (kind == "rate_limited" and retry_after is not None)
            jitter = (1.0, 1.02) if kind == "daily_quota" else (1.0, 1.15)   # don't overshoot the reset
            cd = max(5.0, min(cd, 86400.0 if declared else 3600.0)) * random.uniform(*jitter)  # noqa: S311
            h.cooldown_until = time.time() + cd
        self._save_state(force=True)

    def _ok(self, p: Provider, usage: dict[str, Any] | None) -> None:
        h = self.health[p.name]
        changed = h.consecutive_failures or h.cooldown_until
        h.consecutive_failures, h.cooldown_until, h.last_error, h.last_kind = 0, 0.0, "", ""
        self._roll_day(h)
        h.tokens_used += int((usage or {}).get("total_tokens") or 0)
        self._save_state(force=bool(changed))

    # ── upstream call ──
    async def _post(self, p: Provider, payload: dict[str, Any], budget_s: float) -> httpx.Response:
        chaos = _chaos_for(p.name)
        if chaos is not None:
            return _chaos_response(chaos, payload)
        key = os.environ.get(p.key_env, "").strip()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **p.extra_headers}
        return await self.client.post(
            p.base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers,
            timeout=httpx.Timeout(budget_s, connect=p.connect_timeout_s),
        )

    @staticmethod
    def _note(attempts: list[Attempt], att: Attempt) -> None:
        """Append, but record each skip once even across several passes."""
        if att.outcome.startswith("skipped:") and any(
                a.provider == att.provider and a.outcome == att.outcome for a in attempts):
            return
        attempts.append(att)

    def exhausted_kind(self, attempts: list[Attempt]) -> str:
        """'rate_limited' if every provider we tried or skipped was merely rate-limited, else 'mixed'."""
        seen = False
        for a in attempts:
            if a.provider == "router":
                continue
            seen = True
            if a.outcome == "skipped:cooldown":     # a cooldown only counts if a 429 caused it
                ok = self.health[a.provider].last_kind in ("rate_limited", "daily_quota")
            else:
                ok = a.outcome in RATE_LIMITED_OUTCOMES
            if not ok:
                return "mixed"
        return "rate_limited" if seen else "mixed"

    async def complete(self, body: dict[str, Any]) -> tuple[dict[str, Any], Provider, list[Attempt]]:
        session = self.session_key(body.get("messages", []))
        eligible, attempts = self.plan(body, session)
        tool_names = {t.get("function", {}).get("name") for t in body.get("tools") or []} - {None}
        start = time.monotonic()
        primary = self.providers[0].name
        tried: set[str] = set()                    # providers already sent a request: never retried
        waits: list[float] = []                    # local_rpm waits seen in the latest pass

        for rnd in range(max(1, self.local_retry_rounds)):
            waits, busy = [], False
            for p in eligible:
                remaining = self.deadline_s - (time.monotonic() - start)
                if remaining < 5:
                    self._note(attempts, Attempt(p.name, "skipped:deadline"))
                    continue
                if self.inflight[p.name] >= p.max_concurrency:
                    busy = True
                    self._note(attempts, Attempt(p.name, "skipped:busy"))
                    continue
                if not self.buckets[p.name].try_take():
                    waits.append(self.buckets[p.name].seconds_until_token())
                    self._note(attempts, Attempt(p.name, "skipped:local_rpm"))
                    continue
                tried.add(p.name)
                data = await self._try(p, body, tool_names, session, primary, attempts, remaining)
                if data is not None:
                    return data, p, attempts

            # Everything left was only locally throttled: a short wait beats a 503 (and the
            # caller's break-glass fallback surfacing a raw upstream 429).
            cands = waits + ([1.0] if busy else [])
            if rnd + 1 >= self.local_retry_rounds or not cands:
                break
            wait = min(cands)
            if wait > self.local_wait_max_s or self.deadline_s - (time.monotonic() - start) <= wait + 5:
                break
            attempts.append(Attempt("router", f"waited:{wait:.1f}s"))
            await _sleep(wait)
            waits = []
            eligible = [q for q in self.plan(body, session)[0] if q.name not in tried]   # honours new cooldowns
            if not eligible:
                break

        now = time.time()
        left = [self.health[p.name].cooldown_until - now for p in self.providers
                if os.environ.get(p.key_env, "").strip() and self.health[p.name].cooldown_until > now]
        left += waits
        raise ExhaustedError(attempts, max(1, math.ceil(min(left))) if left else 30)

    async def _try(self, p: Provider, body: dict[str, Any], tool_names: set[Any], session: str, primary: str,
                   attempts: list[Attempt], remaining: float) -> dict[str, Any] | None:
        """One upstream request; the answer on success, else None with the attempt recorded."""
        h = self.health[p.name]
        self._roll_day(h)
        h.requests_used += 1
        self._save_state()
        t0 = time.monotonic()
        self.inflight[p.name] += 1
        try:
            resp = await self._post(p, self.build_payload(p, body), min(p.timeout_s, remaining))
        except httpx.TimeoutException as exc:
            self._fail(p, "timeout", None, type(exc).__name__)
            attempts.append(Attempt(p.name, "timeout", latency_ms=_ms(t0)))
            return None
        except httpx.TransportError as exc:
            self._fail(p, "network", None, type(exc).__name__)
            attempts.append(Attempt(p.name, "network", latency_ms=_ms(t0), detail=type(exc).__name__))
            return None
        finally:
            self.inflight[p.name] -= 1

        att = Attempt(p.name, "", resp.status_code, _ms(t0))
        attempts.append(att)
        text = resp.text[:500]
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            problem = self.validate(data, tool_names)
            if problem:
                att.outcome, att.detail = "invalid_output", problem
                self._fail(p, "invalid_output", None, problem)
                return None
            att.outcome = "ok"
            self._ok(p, data.get("usage"))
            if p.gemini_thought_signatures:
                self._capture_signatures(data["choices"][0]["message"])
            if p.name == primary:
                self.sticky.pop(session, None)
            elif p.sticky and self.sticky_s > 0:       # never pin onto scarce-quota providers
                self.sticky[session] = (p.name, time.time() + self.sticky_s)
                self.sticky.move_to_end(session)
                while len(self.sticky) > 2000:
                    self.sticky.popitem(last=False)
            self.cache_put(body, data, p.name)
            return data

        status = resp.status_code
        if status == 429 and _is_daily_quota(resp):
            att.outcome = "rate_limited:daily"
            self._fail(p, "daily_quota", None, text)
        elif status == 429:
            att.outcome = "rate_limited"
            self._fail(p, "rate_limited", _retry_after(resp), text)
        elif status == 402:
            att.outcome = "payment"
            self._fail(p, "payment", None, text)
        elif status in (401, 403):
            att.outcome = "auth"
            self._fail(p, "auth", None, text)
        elif status in (400, 413, 422) and CONTEXT_ERROR.search(text):
            att.outcome = "context_too_long"            # request-level: no penalty
        elif status >= 500 or status == 408:
            att.outcome = "server_error"
            self._fail(p, "server_error", _retry_after(resp), text)
        else:
            att.outcome, att.detail = "rejected", text[:200]   # e.g. schema the provider dislikes
        return None


# ─────────────────────────────── helpers ────────────────────────────────

def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _strip_keys(node: Any, keys: set[str]) -> Any:
    if not keys:
        return node
    if isinstance(node, dict):
        return {k: _strip_keys(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [_strip_keys(v, keys) for v in node]
    return node


def _retry_after(resp: httpx.Response) -> float | None:
    """Retry-After (seconds or HTTP date) or X-RateLimit-Reset (epoch s / ms, or seconds)."""
    ra = resp.headers.get("retry-after")
    if ra:
        if ra.strip().isdigit():
            return float(ra)
        with contextlib.suppress(TypeError, ValueError):
            return max(0.0, email.utils.parsedate_to_datetime(ra).timestamp() - time.time())
    reset = resp.headers.get("x-ratelimit-reset")
    if reset:
        with contextlib.suppress(ValueError):
            v = float(reset)
            if v > 1e12:
                return max(0.0, v / 1000 - time.time())
            if v > 1e9:
                return max(0.0, v - time.time())
            return v
    return _retry_after_from_body(resp)


DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)(?![a-z])", re.I)
RETRY_IN = re.compile(r"retry in ((?:\d+(?:\.\d+)?\s*(?:ms|h|m|s)\s*)+)", re.I)


def _duration_s(text: str) -> float | None:
    """'28800s' / '51.07s' / '8h' / '1h30m' / '500ms' -> seconds."""
    unit = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    parts = DURATION_PART.findall(text or "")
    return sum(float(n) * unit[u] for n, u in parts) if parts else None


def _retry_after_from_body(resp: httpx.Response) -> float | None:
    """Gemini puts the window in the body: error.details[RetryInfo].retryDelay or 'retry in 8h'."""
    try:
        body = resp.json()
    except ValueError:
        return None
    for item in body if isinstance(body, list) else [body]:
        err = (item or {}).get("error") if isinstance(item, dict) else None
        if not isinstance(err, dict):
            continue
        for d in err.get("details") or []:
            if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo") and d.get("retryDelay"):
                return _duration_s(str(d["retryDelay"]))
        m = RETRY_IN.search(str(err.get("message", "")))
        if m:
            return _duration_s(m.group(1))
    return None


def _is_daily_quota(resp: httpx.Response) -> bool:
    """Gemini 429 for an exhausted DAILY quota (its retryDelay is short and misleading)."""
    try:
        body = resp.json()
    except ValueError:
        return False
    for item in body if isinstance(body, list) else [body]:
        err = (item or {}).get("error") if isinstance(item, dict) else None
        if not isinstance(err, dict):
            continue
        for d in err.get("details") or []:
            if isinstance(d, dict) and str(d.get("@type", "")).endswith("QuotaFailure"):
                for v in d.get("violations") or []:
                    if isinstance(v, dict) and "perday" in str(v.get("quotaId", "")).lower():
                        return True
        if PER_DAY.search(str(err.get("message", ""))):
            return True
    return False


def _chaos_for(provider: str) -> Any:
    """Fault injection for failover drills. Off unless ROUTER_ENABLE_CHAOS=1."""
    if os.environ.get("ROUTER_ENABLE_CHAOS") != "1":
        return None
    path = os.environ.get("ROUTER_CHAOS_FILE", "")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get(provider) if path else None
    except (OSError, ValueError):
        return None


def _chaos_response(fault: Any, payload: dict[str, Any]) -> httpx.Response:
    req = httpx.Request("POST", "http://chaos.local/chat/completions")
    if fault == "timeout":
        raise httpx.ReadTimeout("chaos timeout", request=req)
    if fault == "context":
        return httpx.Response(400, json={"error": {"message": "chaos: maximum context length exceeded"}}, request=req)
    if fault == "malformed":
        name = ((payload.get("tools") or [{}])[0].get("function") or {}).get("name", "x")
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_chaos", "type": "function", "function": {"name": name, "arguments": "{bad json"}}]}
        return httpx.Response(200, json={"choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}]},
                              request=req)
    if fault == "429daily":                       # Gemini-shaped: exhausted daily quota, short retryDelay
        err = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": (
            "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20. Please retry in 51.2s."), "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "51s"}]}
        return httpx.Response(429, json=[{"error": err}], request=req)
    return httpx.Response(int(fault), json={"error": {"message": f"chaos {fault}"}},
                          headers={"retry-after": "60"} if int(fault) == 429 else {}, request=req)


def _sse(data: dict[str, Any], include_usage: bool) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        base = {"id": data.get("id") or "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion.chunk",
                "created": data.get("created") or int(time.time()), "model": data.get("model")}
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        delta: dict[str, Any] = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, "id": tc.get("id"), "type": "function",
                 "function": {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments", "{}")},
                 **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {})}
                for i, tc in enumerate(msg["tool_calls"])
            ]
        for chunk in (
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]},
        ):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        if include_usage and data.get("usage"):
            yield f"data: {json.dumps({**base, 'choices': [], 'usage': data['usage']})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return gen()


# ─────────────────────────────── HTTP app ───────────────────────────────

def create_app(cfg: dict[str, Any] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    if cfg is None:
        with Path(os.environ.get("ROUTER_CONFIG", "providers.yaml")).open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    config_name = Path(os.environ["ROUTER_CONFIG"]).name if os.environ.get("ROUTER_CONFIG") else "inline"
    api_key = os.environ.get("ROUTER_API_KEY", "")
    if len(api_key) < 24:
        raise RuntimeError("ROUTER_API_KEY must be set (>= 24 chars)")
    router = Router(cfg, transport)

    def authorized(request: Request) -> bool:
        presented = request.headers.get("authorization", "")[7:].strip()
        return hmac.compare_digest(presented.encode(), api_key.encode())

    async def chat(request: Request) -> JSONResponse | StreamingResponse:
        if not authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": {"message": "invalid JSON"}}, status_code=400)
        t0 = time.monotonic()
        try:
            data, p, attempts = await router.complete(body)
        except ExhaustedError as exc:
            hit = router.cache_get(body)
            if hit is not None:
                age = int(time.time() - float(hit["ts"]))
                _log_request(body, None, [*exc.attempts, Attempt("cache", f"stale_hit:{age}s")], t0)
                data = copy.deepcopy(hit["data"])
                data["model"] = f"cache/{hit['provider']}"
                headers = {"x-router-provider": f"cache:{hit['provider']}", "x-router-cache": "stale",
                           "x-router-cache-age": str(age), "x-router-attempts": str(len(exc.attempts))}
                return _respond(body, data, headers)
            _log_request(body, None, exc.attempts, t0)
            return JSONResponse(
                {"error": {"message": "all upstream providers unavailable", "type": "router_exhausted",
                           "attempts": [asdict(a) for a in exc.attempts]}},
                status_code=503, headers={"retry-after": str(exc.retry_after),   # never relay an upstream 429
                                          "x-router-exhausted-kind": router.exhausted_kind(exc.attempts)})
        _log_request(body, p, attempts, t0)
        data["model"] = f"{p.name}/{p.model}"
        headers = {"x-router-provider": p.name, "x-router-attempts": str(len(attempts))}
        return _respond(body, data, headers)

    def _respond(body: dict[str, Any], data: dict[str, Any], headers: dict[str, str]) -> JSONResponse | StreamingResponse:
        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            return StreamingResponse(_sse(data, include_usage), media_type="text/event-stream", headers=headers)
        return JSONResponse(data, headers=headers)

    async def models(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        return JSONResponse({"object": "list", "data": [{
            "id": router.public_model, "object": "model", "owned_by": "hermes-llm-router",
            "context_length": router.advertised_context}]})

    async def healthz(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        now = time.time()
        rows = []
        for p in router.providers:
            h, bucket = router.health[p.name], router.buckets[p.name]
            router._roll_day(h)
            rows.append({
                "name": p.name, "model": p.model, "cooling_down_s": max(0, int(h.cooldown_until - now)),
                "cooldown_kind": h.last_kind if h.cooldown_until > now else "",
                "consecutive_failures": h.consecutive_failures, "last_error": h.last_error,
                "tokens_today": h.tokens_used, "requests_today": h.requests_used,
                "daily_request_budget": p.daily_request_budget,
                "rpm_tokens": round(bucket.peek(), 1) if p.rpm > 0 else None,
                "inflight": router.inflight[p.name], "key_present": bool(os.environ.get(p.key_env))})
        return JSONResponse({"providers": rows, "sticky_sessions": len(router.sticky),
                             "stale_cache_entries": len(router._cache), "config": config_name})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        router._save_state(force=True)
        await router.client.aclose()

    app = Starlette(routes=[
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ], lifespan=lifespan)
    app.state.router = router
    return app


def _log_request(body: dict[str, Any], p: Provider | None, attempts: list[Attempt], t0: float) -> None:
    # Never log message content: it carries client data.
    log.info(json.dumps({
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "served_by": p.name if p else None,
        "latency_ms": _ms(t0),
        "messages": len(body.get("messages", [])),
        "tools": len(body.get("tools") or []),
        "attempts": [f"{a.provider}:{a.outcome}" for a in attempts],
    }))


if os.environ.get("ROUTER_API_KEY"):   # uvicorn router:app
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one JSON line per request is enough
    app = create_app()
