"""
Hermes MCP Gateway — policy guard (drop-in for the FastAPI JSON-RPC handler).

Enforces tool-policy.yaml on every `tools/call` and filters `tools/list`:
  * bearer-token authentication (SHA-256 hashes from env, constant-time compare)
  * role-based tool allowlists (+ private-only roles that never work via the public edge)
  * per-principal token-bucket rate limits and per-principal / global concurrency
  * hard wall-clock timeouts
  * SSRF protection for URL arguments, skill allowlist, script-size limits
  * daily token budgets and a JSONL audit log (argument hashes only, never values)

In-memory state → valid for a single uvicorn worker. For >1 worker move buckets,
semaphores and budgets to Redis.

Wiring (sketch):

    guard = PolicyGuard.from_file("/home/deploy/.hermes/gateway/tool-policy.yaml")

    @app.post("/mcp")            # and the SSE message endpoint
    async def mcp(request: Request):
        body = await request.body()
        try:
            # Nginx overwrites X-Hermes-Edge on every public request (see nginx-mcp.conf).
            # Do NOT use request.client.host: behind Nginx it is always 127.0.0.1.
            principal = guard.authenticate(request.headers.get("authorization"),
                                           request.headers.get("x-hermes-edge") == "public",
                                           len(body))
        except PolicyError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        msg = json.loads(body)
        if msg.get("method") == "tools/list":
            result = await list_tools()
            result["tools"] = guard.filter_tools(principal, result["tools"])
            return rpc_result(msg, result)
        if msg.get("method") == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments", {})
            try:
                guard.authorize(principal, name, args)
                out = await guard.run(principal, name, args, call_tool(name, args))
            except PolicyError as e:
                return rpc_error(msg, -32001, e.message)
            guard.record_tokens(principal, tokens_used_from(out))
            return rpc_result(msg, out)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import time
from collections import defaultdict
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import yaml


T = TypeVar("T")


class PolicyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Principal:
    name: str
    role: str
    tools: frozenset[str]
    daily_token_budget: int
    loopback_only: bool


@dataclass
class _Bucket:
    capacity: float
    tokens: float
    updated: float = field(default_factory=time.monotonic)

    def take(self, rate_per_min: float) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * rate_per_min / 60.0)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class PolicyGuard:
    def __init__(self, policy: dict[str, Any]):
        self.policy = policy
        self.defaults = policy.get("defaults", {})
        self.tool_limits: dict[str, dict[str, Any]] = policy.get("tools", {})
        self.egress = policy.get("egress", {})
        self.roles = self._resolve_roles(policy.get("roles", {}))
        self._principals = self._load_principals(policy.get("principals", []))
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._local_sem: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._global_sem: dict[str, asyncio.Semaphore] = {}
        self._usage: dict[tuple[str, str], int] = defaultdict(int)  # (principal, yyyy-mm-dd) -> tokens

    @classmethod
    def from_file(cls, path: str | Path) -> PolicyGuard:
        with Path(path).open(encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    # ── setup ────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_roles(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        resolved: dict[str, dict[str, Any]] = {}

        def resolve(name: str, seen: tuple[str, ...] = ()) -> dict[str, Any]:
            if name in seen:
                raise ValueError(f"role inheritance cycle: {' -> '.join((*seen, name))}")
            if name in resolved:
                return resolved[name]
            r = raw[name]
            tools = set(r.get("tools", []))
            if r.get("inherits"):
                tools |= resolve(r["inherits"], (*seen, name))["tools"]
            resolved[name] = {**r, "tools": frozenset(tools)}
            return resolved[name]

        for n in raw:
            resolve(n)
        return resolved

    def _load_principals(self, raw: list[dict[str, Any]]) -> list[tuple[bytes, Principal]]:
        out: list[tuple[bytes, Principal]] = []
        for p in raw:
            digest = os.environ.get(p["token_sha256_env"], "").strip().lower()
            if len(digest) != 64:
                continue  # principal disabled until its hash is configured
            role = self.roles[p["role"]]
            out.append((bytes.fromhex(digest), Principal(
                name=p["name"], role=p["role"], tools=role["tools"],
                daily_token_budget=int(role.get("daily_token_budget", 0)),
                loopback_only=bool(role.get("loopback_only", False)))))
        return out

    # ── auth ─────────────────────────────────────────────────────────────
    def authenticate(self, authorization: str | None, via_public_edge: bool, body_len: int) -> Principal:
        if body_len > int(self.defaults.get("max_request_bytes", 262144)):
            raise PolicyError(413, "request too large")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PolicyError(401, "missing bearer token")
        presented = hashlib.sha256(authorization[7:].strip().encode()).digest()
        match = None
        for digest, principal in self._principals:  # compare all → no timing oracle on position
            if hmac.compare_digest(digest, presented):
                match = principal
        if match is None:
            raise PolicyError(401, "invalid token")
        if match.loopback_only and via_public_edge:
            raise PolicyError(403, "this role is only available via SSH tunnel")
        return match

    def filter_tools(self, principal: Principal, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hide tools the principal may not call, so agents don't even plan with them."""
        return [t for t in tools if t.get("name") in principal.tools]

    # ── authorization ────────────────────────────────────────────────────
    def limits(self, tool: str) -> dict[str, Any]:
        return {**self.tool_limits.get("*", {}), **self.tool_limits.get(tool, {})}

    def authorize(self, principal: Principal, tool: str, args: dict[str, Any]) -> None:
        if tool not in principal.tools:
            self._audit(principal, tool, args, "denied:tool")
            raise PolicyError(403, f"tool '{tool}' not permitted for {principal.name}")

        if principal.daily_token_budget and self._usage[(principal.name, _today())] >= principal.daily_token_budget:
            self._audit(principal, tool, args, "denied:budget")
            raise PolicyError(429, "daily token budget exhausted")

        lim = self.limits(tool)
        key = (principal.name, tool)
        rate = float(lim.get("rate", 30))
        bucket = self._buckets.setdefault(key, _Bucket(capacity=rate, tokens=rate))
        if not bucket.take(rate):
            self._audit(principal, tool, args, "denied:rate")
            raise PolicyError(429, f"rate limit for {tool}: {int(rate)}/min")

        if "allowed_skills" in lim:
            # hermes_skill_execute takes one skill name; hermes_run_task takes `skills: [...]`.
            requested = args.get("skills")
            if not isinstance(requested, list):
                requested = [args.get("skill") or args.get("name") or args.get("skill_name") or ""]
                if tool == "hermes_run_task" and requested == [""]:
                    requested = []          # run_task without preloaded skills is fine
            for skill in map(str, requested):
                if skill not in lim["allowed_skills"]:
                    self._audit(principal, tool, args, "denied:skill")
                    raise PolicyError(403, f"skill '{skill}' is not on the allowlist")

        # Pin cost-relevant arguments, e.g. which `model` or `reasoning_effort` a caller may pick.
        for arg, allowed in (lim.get("allowed_values") or {}).items():
            if arg in args and args[arg] not in allowed:
                self._audit(principal, tool, args, f"denied:{arg}")
                raise PolicyError(403, f"{arg}={args[arg]!r} is not allowed for {tool}")

        if "max_script_chars" in lim:
            script = str(args.get("script") or args.get("expression") or args.get("code") or "")
            if len(script) > int(lim["max_script_chars"]):
                raise PolicyError(400, "script too long")

        if "url_arg" in lim:
            self.check_url(str(args.get(lim["url_arg"], "")))

        # Only for tools whose schema accepts it — the live hermes_* tools do NOT, and a
        # strict server rejects unknown arguments. Off unless the policy opts in.
        if self.defaults.get("inject_max_output_tokens"):
            args.setdefault("max_output_tokens", int(self.defaults.get("max_output_tokens", 1500)))

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in self.egress.get("allowed_schemes", ["https"]):
            raise PolicyError(400, f"scheme '{parts.scheme}' not allowed")
        if not host or host in self.egress.get("denied_hosts", []):
            raise PolicyError(400, "host not allowed")
        suffixes = self.egress.get("allowed_host_suffixes") or []
        if suffixes and not any(host == s or host.endswith("." + s) for s in suffixes):
            raise PolicyError(403, f"host '{host}' is not on the egress allowlist")
        if self.egress.get("block_private_ips", True):
            try:
                infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
            except socket.gaierror as exc:
                raise PolicyError(400, "host does not resolve") from exc
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if not ip.is_global or ip.is_multicast:
                    raise PolicyError(403, "URL resolves to a non-public address")
        # NOTE: DNS can change between this check and the browser's own lookup
        # (rebinding). The OS-level egress firewall in README §D is the real backstop.

    # ── execution ────────────────────────────────────────────────────────
    async def run(self, principal: Principal, tool: str, args: dict[str, Any], coro: Awaitable[T]) -> T:
        lim = self.limits(tool)
        local = self._local_sem.setdefault((principal.name, tool), asyncio.Semaphore(int(lim.get("concurrency", 2))))
        glob = self._global_sem.setdefault(tool, asyncio.Semaphore(int(lim.get("global_concurrency", 8))))
        if local.locked():
            raise PolicyError(429, f"{principal.name} already has the max concurrent {tool} calls")
        started = time.monotonic()
        status = "ok"
        async with local:
            try:
                async with asyncio.timeout(float(lim.get("timeout_s", 60))):
                    async with glob:  # queueing time counts against the timeout
                        return await coro
            except TimeoutError as exc:
                status = "timeout"
                raise PolicyError(504, f"{tool} exceeded {lim.get('timeout_s')}s") from exc
            except PolicyError:
                status = "policy_error"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                self._audit(principal, tool, args, status, latency_ms=int((time.monotonic() - started) * 1000))

    def record_tokens(self, principal: Principal, tokens: int) -> None:
        self._usage[(principal.name, _today())] += max(0, int(tokens))

    # ── audit ────────────────────────────────────────────────────────────
    def _audit(self, principal: Principal, tool: str, args: dict[str, Any], status: str, **extra: Any) -> None:
        path = self.defaults.get("audit_log")
        if not path:
            return
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "principal": principal.name,
            "role": principal.role,
            "tool": tool,
            "status": status,
            # Arguments may contain client data or pasted secrets: log a hash, never values.
            "args_sha256": hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16],
            "args_bytes": len(json.dumps(args, default=str)),
            "tokens_today": self._usage[(principal.name, _today())],
            **extra,
        }
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            pass  # auditing must never take the gateway down


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")
