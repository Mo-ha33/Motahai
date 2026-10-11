"""
telemetry.py - Hermes activity telemetry (sentinel T6).

PRODUCER (hermes-mcp process): consultation.py wraps every registered hermes_* tool once; after the tool returns (or
raises) `emit()` appends ONE line to $HERMES_SENTINEL_DIR/telemetry/events.jsonl:

    {"ts": "2026-10-11T12:00:00Z", "tool": "hermes_storefront_probe", "outcome": "OK", "latency_ms": 14312,
     "subject": "epxsmk-y0.myshopify.com"}

CONSUMER (sentinel pass): `read_new()` returns the events appended since a saved byte offset; detect.py renders them
with templates.telemetry_summary() and sends ONE Telegram message per pass. `rotate_if_consumed()` keeps the file small.

Rules (same as flow_state.py, docs/sprints/SENTINEL_T1_T5_SPECS.md section 0 and T6):
  * Never raises into the caller. Any failure is logged at WARNING and swallowed.
  * No LLM, stdlib only, no sibling imports (consultation.py may load this file by path on the VPS).
  * Stores only: timestamp, tool name, an outcome CODE, latency, and a subject that is either a public GTM-ID or a bare
    hostname. Never the result body, the arguments, prompts, tokens, e-mail addresses or URL paths/queries. Every field
    is validated against a strict pattern before it is written AND again before it is read back (a hand-edited file
    cannot inject free text into a Telegram message).
  * One os.write on an O_APPEND descriptor per event (atomic for lines < 4 KB), so concurrent tool threads and the
    sentinel never see a torn line. Directory 0700, file 0600 (permission bits are skipped on Windows).
  * The file is capped at MAX_FILE_BYTES; past it events are dropped (counted in memory, logged once).
"""

from __future__ import annotations

import collections
import functools
import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("hermes.sentinel.telemetry")

DEFAULT_SENTINEL_DIR = "/home/deploy/.hermes/sentinel"
EVENTS_NAME = "events.jsonl"
ROTATED_NAME = "events.jsonl.1"

MAX_FILE_BYTES = 5 * 1024 * 1024      # producer stops writing past this (drops are counted in memory)
ROTATE_MIN_BYTES = 1024 * 1024        # consumer rotates a fully consumed file bigger than this
MAX_LINE_BYTES = 2048                 # one event line is far below the 4 KB atomic-append bound
MAX_BACKLOG_EVENTS = 200              # a failed send keeps the offset; only the last N events are summarised
_STUCK_TAIL_BYTES = 8192              # an unterminated tail longer than this is garbage, not a half-written line

GTM_ID_RE = re.compile(r"^GTM-[A-Z0-9]{4,10}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")
_OUTCOME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,48}(:[A-Za-z0-9_.\-]{1,48})?$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+$")

_dropped = 0
_drop_logged = False
_write_lock = threading.Lock()  # POSIX O_APPEND is atomic on its own; the lock also covers Windows' emulated append


# ---------------------------------------------------------------------------
# field derivation (pure, never raise)
# ---------------------------------------------------------------------------

def _token(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _TOKEN_RE.match(value) else None


def outcome_of(result: Any) -> str:
    """Outcome CODE of a tool result (the JSON string the tool returned). Never raises, never returns free text.

    reason_code wins (CORE_NOT_CONFIGURED, NOT_INVITED, ...); a probe's UNKNOWN + reason becomes UNKNOWN:<reason>;
    AWAITING_HITL_APPROVAL, CONNECTED, applied, OK pass through; status "success" is a publish ("published");
    a tool that returns plain prose (hermes_consult, hermes_run_task) is "OK"."""
    try:
        data = result
        if isinstance(result, (bytes, bytearray)):
            result = result.decode("utf-8", "replace")
        if isinstance(result, str):
            text = result.strip()
            if text.startswith("```"):                       # hermes_consult wraps its BLOCKED envelope in a fence
                text = text.strip("`").strip()
                if text[:4].lower() == "json":
                    text = text[4:].strip()
            if not text.startswith("{"):
                return "OK"
            data = json.loads(text)
        if not isinstance(data, dict):
            return "OK"
        status, code, reason = _token(data.get("status")), _token(data.get("reason_code")), _token(data.get("reason"))
        if status == "AWAITING_HITL_APPROVAL":
            return status
        if code:
            return code
        if status == "UNKNOWN":
            return f"UNKNOWN:{reason}" if reason else "UNKNOWN"
        if status == "success":
            return "published"
        return status or "OK"
    except Exception:  # noqa: BLE001
        return "OK"


def hostname_of(value: Any) -> Optional[str]:
    """Bare lowercase hostname of a URL or host string (no scheme, port, userinfo, path or query), else None."""
    try:
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if "://" not in text and "@" in text:       # an e-mail address, not a host
            return None
        host = urlsplit(text if "://" in text else "//" + text).hostname
        host = (host or "").rstrip(".").lower()
        return host if _HOST_RE.match(host) else None
    except Exception:  # noqa: BLE001
        return None


def subject_of(arguments: Any, lookup: Optional[Callable[[Any], Optional[str]]] = None) -> Optional[str]:
    """Public subject for a tool call: a public GTM-ID, else a bare hostname, else None.

    Priority: public_container_id / container_id (resolved through `lookup` when it is a numeric id) -> store_url host
    -> tenant_key (only when it is a domain; a numeric merchant id is dropped) -> gtm_public_id.
    Nothing else in the arguments is ever read."""
    try:
        if not isinstance(arguments, dict):
            return None
        for key in ("public_container_id", "container_id"):
            value = arguments.get(key)
            if isinstance(value, str) and GTM_ID_RE.match(value.strip()):
                return value.strip()
            if key == "container_id" and value is not None and lookup is not None:
                known = lookup(value)
                if isinstance(known, str) and GTM_ID_RE.match(known):
                    return known
        host = hostname_of(arguments.get("store_url")) or hostname_of(arguments.get("tenant_key"))
        if host:
            return host
        value = arguments.get("gtm_public_id")
        if isinstance(value, str) and GTM_ID_RE.match(value.strip()):
            return value.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# producer
# ---------------------------------------------------------------------------

def telemetry_dir(sentinel_dir: Any = None) -> Path:
    base = sentinel_dir if sentinel_dir is not None else (os.environ.get("HERMES_SENTINEL_DIR", "").strip() or DEFAULT_SENTINEL_DIR)
    return Path(base) / "telemetry"


def events_path(sentinel_dir: Any = None) -> Path:
    return telemetry_dir(sentinel_dir) / EVENTS_NAME


def _iso(when: Optional[datetime]) -> str:
    if when is None:
        when = datetime.now(timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_fields(tool: Any, outcome: Any, latency_ms: Any, subject: Any) -> Dict[str, Any]:
    subject_ok = subject if isinstance(subject, str) and (GTM_ID_RE.match(subject) or _HOST_RE.match(subject)) else None
    try:
        latency = max(0, int(round(float(latency_ms))))
    except (TypeError, ValueError, OverflowError):
        latency = 0
    return {
        "tool": tool if isinstance(tool, str) and _TOOL_RE.match(tool) else "unknown",
        "outcome": outcome if isinstance(outcome, str) and _OUTCOME_RE.match(outcome) else "UNKNOWN",
        "latency_ms": latency,
        "subject": subject_ok,
    }


def dropped_count() -> int:
    return _dropped


def emit(tool: Any, outcome: Any, latency_ms: Any, subject: Any = None, now: Optional[datetime] = None) -> bool:
    """Appends one event. Returns True if a line was written. Never raises."""
    global _dropped, _drop_logged
    try:
        fields = _clean_fields(tool, outcome, latency_ms, subject)
        line = (json.dumps({"ts": _iso(now), **fields}, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            return False
        directory = telemetry_dir()
        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(directory, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        with _write_lock:
            fd = os.open(directory / EVENTS_NAME, flags, 0o600)
            try:
                if os.fstat(fd).st_size > MAX_FILE_BYTES:
                    _dropped += 1
                    if not _drop_logged:
                        _drop_logged = True
                        logger.warning("telemetry: events file exceeds %d bytes; dropping events until the sentinel "
                                       "rotates it", MAX_FILE_BYTES)
                    return False
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except Exception as exc:  # noqa: BLE001 - telemetry never affects a tool call
        logger.warning("telemetry: emit failed (%s)", type(exc).__name__)
        return False


def excluded_tools(env: Optional[Dict[str, str]] = None) -> frozenset:
    """Tool names listed in SENTINEL_TELEMETRY_EXCLUDE (comma list). "*" excludes every tool."""
    raw = (os.environ if env is None else env).get("SENTINEL_TELEMETRY_EXCLUDE", "") or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def instrument(name: str, fn: Callable, lookup: Optional[Callable[[Any], Optional[str]]] = None) -> Callable:
    """Returns `fn` wrapped so every call is timed and emitted after it returns or raises (the exception is re-raised
    unchanged, the return value is returned unchanged). Returns `fn` itself if the tool is excluded or async."""
    excluded = excluded_tools()
    if "*" in excluded or name in excluded or inspect.iscoroutinefunction(fn):
        return fn
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None

    def record(outcome: str, started: float, args: tuple, kwargs: dict) -> None:
        try:
            latency_ms = (time.monotonic() - started) * 1000.0
            subject = None
            if signature is not None:
                subject = subject_of(signature.bind_partial(*args, **kwargs).arguments, lookup)
            emit(name, outcome, latency_ms, subject)
        except Exception:  # noqa: BLE001
            pass

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            record(type(exc).__name__, started, args, kwargs)
            raise
        record(outcome_of(result), started, args, kwargs)
        return result

    return wrapper


# ---------------------------------------------------------------------------
# consumer
# ---------------------------------------------------------------------------

@dataclass
class ReadResult:
    exists: bool = False
    events: List[dict] = field(default_factory=list)   # at most MAX_BACKLOG_EVENTS, oldest first
    total: int = 0                                      # events found since the offset (can exceed len(events))
    offset: int = 0                                     # where the next read should start
    size: int = 0                                       # file size at the time of the read
    ino: int = 0
    reset: bool = False                                 # truncated or replaced since the saved offset


def parse_line(raw: bytes) -> Optional[dict]:
    """One validated event, or None. Re-validates every field (the file is not trusted)."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("ts"), str):
            return None
        fields = _clean_fields(data.get("tool"), data.get("outcome"), data.get("latency_ms"), data.get("subject"))
        if fields["tool"] == "unknown" and data.get("tool") != "unknown":
            return None
        return {"ts": data["ts"][:25], **fields}
    except Exception:  # noqa: BLE001
        return None


def read_new(path: Path, offset: Any = 0, ino: Any = None) -> ReadResult:
    """Events appended after byte `offset`. If the file is smaller than the offset, or its inode changed, it was
    truncated or replaced and reading restarts at 0. A trailing line without a newline (a write in flight) is left for
    the next call. Never raises (an unreadable file returns exists=False)."""
    try:
        start = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0 else 0
        st = os.stat(path)
        result = ReadResult(exists=True, offset=start, size=st.st_size, ino=st.st_ino)
        if st.st_size < start or (isinstance(ino, int) and ino and st.st_ino and st.st_ino != ino):
            start, result.reset = 0, True
        if st.st_size > start:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(st.st_size - start)
            end = data.rfind(b"\n")
            if end < 0 and len(data) > _STUCK_TAIL_BYTES:
                end = len(data) - 1                      # garbage without a newline: consume it
            chunk = data[: end + 1] if end >= 0 else b""
            kept: Deque[dict] = collections.deque(maxlen=MAX_BACKLOG_EVENTS)
            total = 0
            for raw in chunk.splitlines():
                event = parse_line(raw)
                if event is not None:
                    total += 1
                    kept.append(event)
            result.events, result.total = list(kept), total
            result.offset = start + len(chunk)
        else:
            result.offset = start
        return result
    except FileNotFoundError:
        return ReadResult(exists=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry: read failed (%s)", type(exc).__name__)
        return ReadResult(exists=False)


def rotate_if_consumed(path: Path, offset: int, min_bytes: int = ROTATE_MIN_BYTES) -> bool:
    """Renames a fully consumed file bigger than `min_bytes` to events.jsonl.1 (appenders reopen per write, so the
    next event starts a fresh file). Returns True if it rotated. Bytes appended in the instant between the size check
    and the rename are copied from the rotated file back into the new one. Never raises."""
    try:
        size = os.stat(path).st_size
        if size <= min_bytes or size > offset:
            return False
        rotated = path.with_name(ROTATED_NAME)
        os.replace(path, rotated)
        late = os.stat(rotated).st_size - offset
        if late > 0:
            with open(rotated, "rb") as fh:
                fh.seek(offset)
                tail = fh.read(late)
            end = tail.rfind(b"\n")
            if end >= 0:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    os.write(fd, tail[: end + 1])
                finally:
                    os.close(fd)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry: rotate failed (%s)", type(exc).__name__)
        return False
