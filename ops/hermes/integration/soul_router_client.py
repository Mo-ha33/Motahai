"""
soul_router_client — drop-in for the hermes_mcp wrapper (hermes_consult / hermes_audit_code).

Does two things the current wrapper path does not guarantee:
  1. sends ~/.hermes/SOUL.md + MEMORY.md as the SYSTEM prompt on every call
  2. calls the local llm-router (NVIDIA → Gemini → OpenRouter failover) instead of one
     Gemini key, so a single provider's 429 never reaches Wesam

and returns a provenance line (provider + SHA-256 of the SOUL/MEMORY actually sent) so the
behaviour can be verified from outside: `sha256sum ~/.hermes/SOUL.md` must match.

Stdlib only. Wire-in (inside the wrapper's executor):

    from soul_router_client import consult, RouterUnavailableError
    try:
        r = consult(prompt, context=context)
        return r["text"] + "\\n\\n" + r["provenance"]
    except RouterUnavailableError:
        ...existing direct-Gemini code path (break-glass)...

Env: HERMES_HOME (~/.hermes), HERMES_ROUTER_URL (http://127.0.0.1:47311/v1/chat/completions),
     HERMES_ROUTER_KEY (= the router's ROUTER_API_KEY).
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://127.0.0.1:47311/v1/chat/completions"
MAX_SOUL_BYTES = 16_000      # guard: a runaway SOUL/MEMORY must not eat the whole context
MAX_MEMORY_BYTES = 4_000

_cache: dict[str, Any] = {"key": None, "prompt": "", "soul_sha": "", "mem_sha": ""}


class RouterUnavailableError(Exception):
    """Router not reachable / not answering — caller should use its break-glass path."""


class RouterExhaustedError(Exception):
    """Router reachable but every provider is rate-limited (HTTP 503)."""


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _read(path: Path, limit: int) -> tuple[str, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return "", "missing"
    return raw[:limit].decode("utf-8", errors="replace"), hashlib.sha256(raw).hexdigest()


def system_prompt() -> tuple[str, str, str]:
    """(prompt, soul_sha256, memory_sha256); re-read only when the files change."""
    soul_p, mem_p = _home() / "SOUL.md", _home() / "MEMORY.md"
    key = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in (soul_p, mem_p))
    if key != _cache["key"]:
        soul, soul_sha = _read(soul_p, MAX_SOUL_BYTES)
        mem, mem_sha = _read(mem_p, MAX_MEMORY_BYTES)
        prompt = soul
        if mem:
            prompt += "\n\n---\n\n# MEMORY (hot index — verified facts and standing decisions)\n\n" + mem
        _cache.update(key=key, prompt=prompt, soul_sha=soul_sha, mem_sha=mem_sha)
    return _cache["prompt"], _cache["soul_sha"], _cache["mem_sha"]


def _post(body: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, str]]:
    url = os.environ.get("HERMES_ROUTER_URL", DEFAULT_URL)
    if not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        raise RouterUnavailableError("HERMES_ROUTER_URL must point at the local router")
    req = urllib.request.Request(  # noqa: S310 — scheme/host restricted to loopback http above
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('HERMES_ROUTER_KEY', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback only (checked above)
            return json.loads(resp.read()), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise RouterExhaustedError(exc.read()[:300].decode("utf-8", "replace")) from exc
        raise RouterUnavailableError(f"router HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RouterUnavailableError(str(exc)) from exc


def consult(prompt: str, context: str | None = None, *, max_tokens: int = 1500,
            temperature: float = 0.2, timeout: float = 150) -> dict[str, Any]:
    """Ask Hermes-with-SOUL. Returns {text, provider, cache, soul_sha256, memory_sha256, provenance}."""
    system, soul_sha, mem_sha = system_prompt()
    user = prompt if not context else f"{prompt}\n\n## Context\n{context}"
    body = {"model": "hermes-auto", "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    data, headers = _post(body, timeout)
    try:
        text = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterUnavailableError("malformed router response") from exc
    provider = headers.get("x-router-provider", "?")
    cache = headers.get("x-router-cache", "")
    provenance = (f"— hermes provenance · served_by={provider}{' (stale-cache)' if cache else ''}"
                  f" · model={data.get('model', '?')} · SOUL sha256:{soul_sha[:16]}"
                  f" · MEMORY sha256:{mem_sha[:16]}")
    return {"text": text, "provider": provider, "cache": cache, "soul_sha256": soul_sha,
            "memory_sha256": mem_sha, "provenance": provenance}
