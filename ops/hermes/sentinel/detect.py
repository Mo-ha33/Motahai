"""Hermes token-exhaustion sentinel: the detector (T2).

One call to ``run_pass`` is one detection pass (``python -m sentinel --once``). It reads three inputs
(the router journal, the router ``/healthz`` page, the flow-state files), updates the incident book
(contract C2), decides which notifications are due, hands them to ``dispatch.dispatch`` and only THEN
persists ``state.json`` / ``incidents.json`` (a crash re-sends rather than drops).

Hard rules (docs/sprints/SENTINEL_T1_T5_SPECS.md section 0): no LLM anywhere, stdlib + httpx only,
no message content / keys / client PII in state or logs, every write atomic, files 0600, dirs 0700.
The sentinel only reads the router and hermes-mcp; it never raises into them.

Files under ``$HERMES_SENTINEL_DIR``:
  state.json      journal cursor, consecutive counters, budget day per provider, last tick   (written)
  incidents.json  {incident_key: C2 incident + bookkeeping fields}                           (written)
  flow/*.json     C1 flow-state records, written by flow_state.py                            (read)
  contacts.json   C4 client contacts, maintained by the operator                             (read)
  silence.json    {"<incident key | key prefix | kind>": "<until ISO UTC>"}                  (read)
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.sentinel")

DEFAULT_SENTINEL_DIR = "/home/deploy/.hermes/sentinel"
DEFAULT_ROUTER_URL = "http://127.0.0.1:47311"  # the port in hermes-llm-router.service
ROUTER_UNIT = "hermes-llm-router"

HEALTHZ_TIMEOUT_S = 10.0
HEARTBEAT_TIMEOUT_S = 5.0
JOURNAL_TIMEOUT_S = 20.0

REMINDER_INTERVAL = timedelta(hours=6)
RESOLVED_RETENTION = timedelta(days=7)
FOLLOWUP_WINDOW = timedelta(hours=24)
CLIENT_FAILED_RETRY_GAP = timedelta(minutes=10)  # failed SMTP is retried at most this often (outside_hours: every pass)
BUDGET_ALERT_RATIO = 0.8
UNREACHABLE_PASSES = 2          # failed /healthz passes in a row before router:unreachable
CLEAR_PASSES = 2                # clean passes in a row before router:exhausted / provider / sentinel incidents resolve

GTM_ID_RE = re.compile(r"^GTM-[A-Z0-9]{4,10}$")
CURSOR_RE = re.compile(r"^[A-Za-z0-9=;:_.\-]{1,512}$")

PROVIDER_KINDS = ("rate_limited", "daily_quota", "payment", "auth", "model_gone")
_KIND_PRIORITY = {"rate_limited": 1, "daily_quota": 2, "model_gone": 3, "auth": 4, "payment": 5}

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_OUTSIDE_HOURS = "skipped:outside_hours"
CHANNEL_CLIENT = "client_email"

KEY_EXHAUSTED = "router:exhausted"
KEY_UNREACHABLE = "router:unreachable"
KEY_SENTINEL_ERROR = "sentinel:error"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return _utc(when).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def budget_day(now: datetime) -> str:
    """The provider quota day. Free-tier daily quotas reset at Pacific midnight."""
    try:
        from zoneinfo import ZoneInfo

        return _utc(now).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 - no tzdata (Windows): fixed UTC-8, at worst one hour off in summer
        return _utc(now).astimezone(timezone(timedelta(hours=-8))).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# configuration and injectable dependencies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    sentinel_dir: Path
    router_url: str
    router_key: str
    heartbeat_url: str

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Config":
        env = os.environ if env is None else env

        def get(name: str) -> str:
            return (env.get(name) or "").strip()

        return cls(
            sentinel_dir=Path(get("HERMES_SENTINEL_DIR") or DEFAULT_SENTINEL_DIR),
            router_url=(get("ROUTER_URL") or DEFAULT_ROUTER_URL).rstrip("/"),
            # On the VPS ROUTER_API_KEY lives in a root-only file; the deploy user can read HERMES_ROUTER_KEY.
            # ROUTER_URL is deliberately NOT derived from HERMES_ROUTER_URL (that one is the client-side setting).
            router_key=get("ROUTER_API_KEY") or get("HERMES_ROUTER_KEY"),
            heartbeat_url=get("SENTINEL_HEARTBEAT_URL"),
        )


class JournalError(Exception):
    pass


def read_journal(cursor: Optional[str], run: Callable[..., Any] = subprocess.run) -> Tuple[List[str], Optional[str]]:
    """The ONE place that talks to journalctl. Returns (lines, new_cursor); new_cursor is None if none was printed.

    Raises JournalError when the journal cannot be read."""

    def call(args: List[str]):
        try:
            return run(
                ["journalctl", "-u", ROUTER_UNIT, "-o", "cat", "--no-pager", "--show-cursor", *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=JOURNAL_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise JournalError(type(exc).__name__) from None

    if cursor and CURSOR_RE.match(cursor):
        proc = call([f"--after-cursor={cursor}"])
        if proc.returncode != 0:  # cursor rotated away or invalid: fall back to the first-run window
            proc = call(["--since=-2min"])
    else:
        proc = call(["--since=-2min"])
    if proc.returncode != 0:
        raise JournalError(f"journalctl_exit_{proc.returncode}")

    lines: List[str] = []
    new_cursor: Optional[str] = None
    for raw in (proc.stdout or "").splitlines():
        if raw.startswith("-- cursor: "):
            candidate = raw[len("-- cursor: "):].strip()
            if CURSOR_RE.match(candidate):
                new_cursor = candidate
        else:
            lines.append(raw)
    return lines, new_cursor


def make_fetch_healthz(cfg: Config) -> Callable[[], Tuple[Optional[int], Any]]:
    def fetch() -> Tuple[Optional[int], Any]:
        import httpx

        response = httpx.get(
            f"{cfg.router_url}/healthz",
            headers={"Authorization": f"Bearer {cfg.router_key}"},
            timeout=HEALTHZ_TIMEOUT_S,
        )
        try:
            data = response.json()
        except ValueError:
            data = None
        return response.status_code, data

    return fetch


def default_heartbeat(url: str) -> None:
    import httpx

    httpx.get(url, timeout=HEARTBEAT_TIMEOUT_S)


def default_dispatch(notifications: List[dict], contacts: dict, now: datetime) -> List[dict]:
    """Calls dispatch.dispatch (T3). Imported lazily so a dispatcher import problem is a handled pass failure."""
    import inspect

    from . import dispatch as dispatch_mod

    kwargs: Dict[str, Any] = {}
    try:
        if "now" in inspect.signature(dispatch_mod.dispatch).parameters:
            kwargs["now"] = now
    except (TypeError, ValueError):
        pass
    return dispatch_mod.dispatch(notifications, contacts, **kwargs)


@dataclass
class Deps:
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    read_journal: Callable[[Optional[str]], Tuple[List[str], Optional[str]]] = read_journal
    fetch_healthz: Optional[Callable[[], Tuple[Optional[int], Any]]] = None  # None -> built from Config
    dispatch: Callable[[List[dict], dict, datetime], List[dict]] = default_dispatch
    heartbeat: Callable[[str], None] = default_heartbeat


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Tuple[Any, str]:
    """Returns (data, status) with status in ok | missing | corrupt."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError):
        return None, "corrupt"
    try:
        return json.loads(text), "ok"
    except ValueError:
        return None, "corrupt"


def _fresh_state() -> dict:
    return {"cursor": None, "healthz_fail": 0, "clear": {}, "budget_day": {}, "tick": None}


def _valid_state(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("cursor") is not None and not isinstance(data.get("cursor"), str):
        return False
    fail = data.get("healthz_fail", 0)
    if not isinstance(fail, int) or isinstance(fail, bool) or fail < 0:
        return False
    clear = data.get("clear", {})
    if not isinstance(clear, dict) or not all(isinstance(k, str) and isinstance(v, int) for k, v in clear.items()):
        return False
    days = data.get("budget_day", {})
    return isinstance(days, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in days.items())


def _valid_incidents(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for key, inc in data.items():
        if not isinstance(inc, dict) or inc.get("incident_key") != key or inc.get("state") not in ("open", "resolved"):
            return False
    return True


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "state.json"
        self.incidents_path = root / "incidents.json"
        self.corrupt: List[Path] = []

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.root, 0o700)
        probe = self.root / f".write-probe.{os.getpid()}"
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        probe.unlink()

    def load_state(self) -> Tuple[dict, bool]:
        data, status = _read_json(self.state_path)
        if status == "ok" and _valid_state(data):
            merged = _fresh_state()
            merged.update(data)
            return merged, False
        if status == "missing":
            return _fresh_state(), False
        self.corrupt.append(self.state_path)
        return _fresh_state(), True

    def load_incidents(self) -> Tuple[dict, bool]:
        data, status = _read_json(self.incidents_path)
        if status == "ok" and _valid_incidents(data):
            return data, False
        if status == "missing":
            return {}, False
        self.corrupt.append(self.incidents_path)
        return {}, True

    def save(self, state: dict, incidents: dict) -> None:
        for path in self.corrupt:  # keep one copy of what we could not parse
            try:
                os.replace(path, path.with_name(path.name + ".corrupt"))
            except OSError:
                pass
        _atomic_write_json(self.incidents_path, incidents)
        _atomic_write_json(self.state_path, state)


def _load_optional_dict(path: Path) -> Tuple[dict, bool]:
    """Read-only operator files (contacts.json, silence.json). Returns (dict, corrupt)."""
    data, status = _read_json(path)
    if status == "missing":
        return {}, False
    if status == "ok" and isinstance(data, dict):
        return data, False
    return {}, True


# ---------------------------------------------------------------------------
# journal parsing and exhaustion_kind derivation
# ---------------------------------------------------------------------------

def parse_journal(lines: List[str]) -> Tuple[int, List[dict]]:
    """Returns (served_count, null_lines). Non-JSON lines and JSON without ``served_by`` are ignored."""
    served = 0
    nulls: List[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or "served_by" not in obj:
            continue
        if obj["served_by"] is None:
            nulls.append(obj)
        elif isinstance(obj["served_by"], str) and obj["served_by"]:
            served += 1
    return served, nulls


_NEUTRAL_SKIPS = {"no_key", "context", "no_tools", "deadline", "busy"}
_MODEL_GONE_RE = re.compile(r"^(?:http_?)?(?:404|410)$")


def _outcome_category(outcome: str) -> Optional[str]:
    """Category of one attempt outcome: a PROVIDER_KINDS value, 'cooldown', 'other', or None (neutral)."""
    if outcome == "rate_limited:daily" or outcome in ("skipped:daily_requests", "skipped:daily_budget"):
        return "daily_quota"
    if outcome == "rate_limited" or outcome == "skipped:local_rpm":
        return "rate_limited"
    if outcome == "skipped:cooldown":
        return "cooldown"
    if outcome in ("payment", "auth", "model_gone"):
        return outcome
    if _MODEL_GONE_RE.match(outcome):
        return "model_gone"
    if outcome.startswith("skipped:") and outcome[len("skipped:"):] in _NEUTRAL_SKIPS:
        return None
    return "other"


def _healthz_kind(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    if entry.get("model_gone"):
        return "model_gone"
    kind = entry.get("cooldown_kind")
    return kind if kind in PROVIDER_KINDS else None


def derive_exhaustion(null_lines: List[dict], healthz: Optional[dict]) -> Tuple[str, List[str]]:
    """(exhaustion_kind, providers) for this pass's served_by:null lines, refined with /healthz cooldown_kind."""
    by_name: Dict[str, dict] = {}
    if isinstance(healthz, dict) and isinstance(healthz.get("providers"), list):
        for entry in healthz["providers"]:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                by_name[entry["name"]] = entry

    cats: Dict[str, set] = {}
    order: List[str] = []
    for line in null_lines:
        attempts = line.get("attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, str) or ":" not in attempt:
                continue
            provider, outcome = attempt.split(":", 1)
            if not provider:
                continue
            if provider not in cats:
                cats[provider] = set()
                order.append(provider)
            cats[provider].add(_outcome_category(outcome))

    if not order and by_name:  # no usable attempts: fall back to what /healthz says is benched
        for name, entry in by_name.items():
            if _healthz_kind(entry):
                order.append(name)
                cats[name] = {"cooldown"}

    kinds: List[str] = []
    for provider in order:
        hz = _healthz_kind(by_name.get(provider))
        found = set()
        for cat in cats[provider]:
            if cat == "cooldown":
                found.add(hz or "rate_limited")
            elif cat in PROVIDER_KINDS:
                found.add(cat)
        if hz and (found or "other" not in cats[provider]) and cats[provider] - {None}:
            found.add(hz)
        if found:
            kinds.append(max(found, key=lambda k: _KIND_PRIORITY[k]))
        elif "other" in cats[provider]:
            kinds.append("other")
    distinct = set(kinds)
    if len(distinct) == 1 and "other" not in distinct:
        return distinct.pop(), order
    return "mixed", order


# ---------------------------------------------------------------------------
# incident book
# ---------------------------------------------------------------------------

def _new_incident(key: str, kind: str, severity: str, scope: str, now: datetime, **fields: Any) -> dict:
    inc = {
        "incident_key": key, "severity": severity, "state": "open", "kind": kind,
        "exhaustion_kind": None, "scope": scope, "container_id": None, "last_successful_step": None,
        "providers": [], "http_status": None, "opened_at": _iso(now), "timestamp": _iso(now), "count": 1,
        "last_notified_at": None,
    }
    inc.update(fields)
    if kind == "flow_stalled":
        inc.update({"client_holding_sent": False, "client_followup_sent": False})
    return inc


@dataclass
class _Pass:
    """Mutable working set of one pass. Nothing here is persisted until dispatch has returned."""
    now: datetime
    state: dict
    incidents: dict
    opened: set = field(default_factory=set)
    resolved: set = field(default_factory=set)
    errors: Dict[str, None] = field(default_factory=dict)  # sentinel error incident keys raised this pass

    def is_open(self, key: str) -> bool:
        inc = self.incidents.get(key)
        return bool(inc) and inc["state"] == "open"

    def raise_incident(self, key: str, kind: str, severity: str, scope: str, count: int = 1, **fields: Any) -> dict:
        """Open a new incident, or refresh the open one in place (no new notification)."""
        if self.is_open(key):
            inc = self.incidents[key]
            inc.update(fields)
            inc["timestamp"] = _iso(self.now)
            inc["count"] = (inc.get("count") or 0) + count
            return inc
        inc = _new_incident(key, kind, severity, scope, self.now, count=count, **fields)
        self.incidents[key] = inc
        self.opened.add(key)
        self.state["clear"].pop(key, None)
        return inc

    def resolve(self, key: str) -> None:
        inc = self.incidents[key]
        inc["state"] = "resolved"
        inc["resolved_at"] = _iso(self.now)
        inc["timestamp"] = _iso(self.now)
        self.resolved.add(key)
        self.state["clear"].pop(key, None)

    def clear_tick(self, key: str, needed: int = CLEAR_PASSES) -> None:
        """One clean pass for an open incident; resolves it after `needed` clean passes in a row."""
        count = self.state["clear"].get(key, 0) + 1
        if count >= needed:
            self.resolve(key)
        else:
            self.state["clear"][key] = count


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------

def _detect_healthz(p: _Pass, cfg: Config, deps: Deps) -> Tuple[Optional[dict], bool]:
    """Returns (healthz dict or None, healthz_ok). Handles router:unreachable and the provider incidents."""
    if not cfg.router_key:
        p.errors[KEY_SENTINEL_ERROR] = None  # healthz config missing
        return None, False
    fetch = deps.fetch_healthz or make_fetch_healthz(cfg)
    data: Optional[dict] = None
    try:
        status, body = fetch()
        if status == 200 and isinstance(body, dict):
            data = body
    except Exception as exc:  # noqa: BLE001 - unreachable, timeout, TLS, ...
        logger.info("healthz unreachable (%s)", type(exc).__name__)

    state = p.state
    if data is None:
        state["healthz_fail"] += 1
        if state["healthz_fail"] >= UNREACHABLE_PASSES:
            p.raise_incident(KEY_UNREACHABLE, "router_unreachable", "CRIT", "router")
        return None, False

    state["healthz_fail"] = 0
    if p.is_open(KEY_UNREACHABLE):
        p.resolve(KEY_UNREACHABLE)

    providers = data.get("providers") if isinstance(data.get("providers"), list) else []
    by_name = {e["name"]: e for e in providers if isinstance(e, dict) and isinstance(e.get("name"), str)}

    # provider:<name>:degraded
    degraded = [n for n in (data.get("degraded") or []) if isinstance(n, str) and n] if isinstance(data.get("degraded"), list) else []
    for name in degraded:
        p.raise_incident(
            f"provider:{name}:degraded", "provider_degraded", "CRIT", "provider",
            providers=[name], exhaustion_kind=_healthz_kind(by_name.get(name)),
        )
        p.state["clear"].pop(f"provider:{name}:degraded", None)
    for key, inc in list(p.incidents.items()):
        if inc["kind"] == "provider_degraded" and inc["state"] == "open" and (inc.get("providers") or [None])[0] not in degraded:
            p.clear_tick(key)

    # provider:<name>:budget80 (once per provider per budget day)
    today = budget_day(p.now)
    for name, entry in by_name.items():
        budget, used = entry.get("daily_request_budget"), entry.get("requests_today")
        if not (_is_num(budget) and budget > 0 and _is_num(used)) or used / budget < BUDGET_ALERT_RATIO:
            continue
        if state["budget_day"].get(name) == today:
            continue
        state["budget_day"][name] = today
        key = f"provider:{name}:budget80"
        p.incidents.pop(key, None)
        inc = _new_incident(key, "budget_80", "WARN", "provider", p.now, providers=[name])
        p.incidents[key] = inc
        p.opened.add(key)
    return data, True


def _detect_budget_rollover(p: _Pass) -> None:
    """A budget80 incident from an earlier budget day is dropped silently (no RECOVERED for it)."""
    today = budget_day(p.now)
    for key, inc in list(p.incidents.items()):
        if inc["kind"] != "budget_80":
            continue
        name = (inc.get("providers") or [None])[0]
        if p.state["budget_day"].get(name) != today and key not in p.opened:
            del p.incidents[key]


def _detect_journal(p: _Pass, deps: Deps, healthz: Optional[dict]) -> None:
    cursor = p.state.get("cursor")
    try:
        lines, new_cursor = deps.read_journal(cursor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("journal unreadable (%s)", type(exc).__name__)
        p.errors[KEY_SENTINEL_ERROR] = None
        return
    if new_cursor:
        p.state["cursor"] = new_cursor

    served, nulls = parse_journal(lines)
    if nulls:
        kind, providers = derive_exhaustion(nulls, healthz)
        existing = p.incidents.get(KEY_EXHAUSTED)
        if p.is_open(KEY_EXHAUSTED):
            providers = list(dict.fromkeys([*(existing.get("providers") or []), *providers]))
        p.raise_incident(
            KEY_EXHAUSTED, "router_exhausted", "CRIT", "router", count=len(nulls),
            exhaustion_kind=kind, providers=providers, http_status=503,
        )
        p.state["clear"].pop(KEY_EXHAUSTED, None)
    elif served > 0 and p.is_open(KEY_EXHAUSTED):
        p.clear_tick(KEY_EXHAUSTED)


def _detect_flows(p: _Pass, cfg: Config) -> None:
    flow_dir = cfg.sentinel_dir / "flow"
    stalled: Dict[str, dict] = {}
    unreadable: set = set()
    try:
        files = sorted(flow_dir.glob("*.json"))
    except OSError:
        files = []
    for path in files:
        stem = path.stem if GTM_ID_RE.match(path.stem) else "unknown"
        data, status = _read_json(path)
        record = None
        if status == "ok" and isinstance(data, dict):
            seq, container, owed = data.get("seq"), data.get("container"), data.get("owed_by")
            deadline = _parse_ts(data.get("deadline"))
            if (isinstance(container, str) and GTM_ID_RE.match(container)
                    and isinstance(seq, int) and not isinstance(seq, bool) and isinstance(owed, str)
                    and (owed != "agent" or deadline is not None)):
                record = (container, seq, owed, deadline, data.get("step"))
        if record is None:
            unreadable.add(stem)
            p.errors[f"{KEY_SENTINEL_ERROR}:flow:{stem}"] = None
            continue
        container, seq, owed, deadline, step = record
        if owed == "agent" and p.now > deadline:
            stalled[f"flow:{container}:{seq}"] = {"container": container, "step": step}

    for key, info in stalled.items():
        step = info["step"] if isinstance(info["step"], str) else None
        p.raise_incident(
            key, "flow_stalled", "CRIT", "container", container_id=info["container"], last_successful_step=step,
        )
    for key, inc in list(p.incidents.items()):
        if inc["kind"] == "flow_stalled" and inc["state"] == "open" and key not in stalled:
            if inc.get("container_id") in unreadable or (unreadable and "unknown" in unreadable):
                continue  # cannot tell: keep it open until the file is readable again
            p.resolve(key)  # seq changed, or the file is gone


def _detect_errors(p: _Pass) -> None:
    for key in p.errors:
        p.raise_incident(key, "sentinel_error", "WARN", "router" if key == KEY_SENTINEL_ERROR else "container")
        p.state["clear"].pop(key, None)
    for key, inc in list(p.incidents.items()):
        if inc["kind"] == "sentinel_error" and inc["state"] == "open" and key not in p.errors:
            p.clear_tick(key)


# ---------------------------------------------------------------------------
# silences, notifications, dispatch results
# ---------------------------------------------------------------------------

def _silenced(inc: dict, silences: dict, now: datetime) -> bool:
    key = inc["incident_key"]
    for pattern, until in silences.items():
        end = _parse_ts(until)
        if end is None or end <= now or not isinstance(pattern, str) or not pattern:
            continue
        if key == pattern or key.startswith(pattern) or inc.get("kind") == pattern:
            return True
    return False


def _has_contact(contacts: dict, container: Any) -> bool:
    entry = contacts.get(container) if isinstance(container, str) else None
    return isinstance(entry, dict) and isinstance(entry.get("email"), str) and bool(entry["email"].strip())


def _retry_due(status: Any, last_at: Any, now: datetime) -> bool:
    """A client email attempt that was deferred (outside hours) or failed is retried on a later pass."""
    if status == STATUS_OUTSIDE_HOURS:
        return True
    if status == STATUS_FAILED:
        previous = _parse_ts(last_at)
        return previous is None or now - previous >= CLIENT_FAILED_RETRY_GAP
    return False


def build_notifications(p: _Pass, contacts: dict, silences: dict) -> List[dict]:
    now = p.now
    out: List[dict] = []

    def add(inc: dict, event: str) -> None:
        out.append({"incident": copy.deepcopy(inc), "event": event})

    for key in sorted(p.incidents):
        inc = p.incidents[key]
        silenced = _silenced(inc, silences, now)
        flow = inc["kind"] == "flow_stalled" and inc["severity"] == "CRIT"
        notified_at = _parse_ts(inc.get("last_notified_at"))

        if inc["state"] == "open":
            if silenced:
                continue
            event = None
            if key in p.opened or notified_at is None:
                event = "open"
            elif inc["severity"] == "CRIT" and now - notified_at >= REMINDER_INTERVAL:
                event = "reminder"
            if event:
                add(inc, event)
            if (flow and event != "open" and inc.get("client_holding_sent") is not True
                    and _has_contact(contacts, inc.get("container_id"))
                    and _retry_due(inc.get("client_status"), inc.get("client_attempt_at"), now)):
                add(inc, "client_holding")
            continue

        # resolved
        if silenced:
            continue
        if key in p.resolved:
            if notified_at is not None and inc["kind"] != "budget_80":
                add(inc, "recovered")
            continue
        if flow and inc.get("client_holding_sent") is True and inc.get("client_followup_sent") is not True:
            resolved_at = _parse_ts(inc.get("resolved_at"))
            if resolved_at is None or now - resolved_at > FOLLOWUP_WINDOW:
                if not inc.get("followup_given_up") and inc.get("followup_status") in (STATUS_OUTSIDE_HOURS, STATUS_FAILED):
                    logger.info("client follow-up given up after 24h (incident=%s)", key)
                    inc["followup_given_up"] = True
                continue
            if (_has_contact(contacts, inc.get("container_id"))
                    and _retry_due(inc.get("followup_status"), inc.get("followup_attempt_at"), now)):
                add(inc, "client_followup")
    return out


_CLIENT_EVENTS = ("open", "client_holding", "recovered", "client_followup")


def apply_results(p: _Pass, notifications: List[dict], rows: Any) -> None:
    """Folds dispatch result rows back into the incident book (still in memory)."""
    now_s = _iso(p.now)
    client_rows: Dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("channel") == CHANNEL_CLIENT and isinstance(row.get("incident_key"), str):
            client_rows[row["incident_key"]] = str(row.get("status"))

    for note in notifications:
        key, event = note["incident"]["incident_key"], note["event"]
        inc = p.incidents.get(key)
        if inc is None:
            continue
        if event in ("open", "reminder"):
            inc["last_notified_at"] = now_s
        elif event == "recovered":
            inc["recovered_notified_at"] = now_s
        if event not in _CLIENT_EVENTS or key not in client_rows:
            continue
        status = client_rows[key]
        if event in ("open", "client_holding"):
            inc["client_status"], inc["client_attempt_at"] = status, now_s
            if status == STATUS_SENT:
                inc["client_holding_sent"] = True
        else:
            inc["followup_status"], inc["followup_attempt_at"] = status, now_s
            if status == STATUS_SENT:
                inc["client_followup_sent"] = True


def _prune(p: _Pass) -> None:
    for key, inc in list(p.incidents.items()):
        if inc["state"] != "resolved":
            continue
        resolved_at = _parse_ts(inc.get("resolved_at"))
        if resolved_at is None or p.now - resolved_at > RESOLVED_RETENTION:
            del p.incidents[key]


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------

@dataclass
class PassResult:
    exit_code: int = 0
    notifications: List[dict] = field(default_factory=list)
    persisted: bool = False
    summary: str = ""


def run_pass(cfg: Config, deps: Optional[Deps] = None, *, dry_run: bool = False) -> PassResult:
    deps = deps or Deps()
    now = _utc(deps.now())
    store = Store(cfg.sentinel_dir)
    if not dry_run:
        try:
            store.ensure()
        except OSError as exc:
            logger.error("state directory not writable (%s)", type(exc).__name__)
            return PassResult(exit_code=1, summary="state directory not writable")

    state, state_bad = store.load_state()
    incidents, incidents_bad = store.load_incidents()
    contacts, contacts_bad = _load_optional_dict(cfg.sentinel_dir / "contacts.json")
    silences, silence_bad = _load_optional_dict(cfg.sentinel_dir / "silence.json")
    p = _Pass(now=now, state=state, incidents=incidents)
    if state_bad or incidents_bad or contacts_bad or silence_bad:
        p.errors[KEY_SENTINEL_ERROR] = None

    healthz, _ok = _detect_healthz(p, cfg, deps)
    _detect_budget_rollover(p)
    _detect_journal(p, deps, healthz)
    _detect_flows(p, cfg)
    _detect_errors(p)

    notifications = build_notifications(p, contacts, silences)
    result = PassResult(notifications=notifications)
    open_count = sum(1 for i in p.incidents.values() if i["state"] == "open")
    counts = "opened=%d resolved=%d open_incidents=%d notifications=%d errors=%d" % (
        len(p.opened), len(p.resolved), open_count, len(notifications), len(p.errors))

    if dry_run:
        result.summary = f"dry-run {counts}"
        logger.info("sentinel pass (%s)", result.summary)
        return result

    rows: Any = []
    if notifications:
        try:
            rows = deps.dispatch(notifications, contacts, now)
        except Exception as exc:  # noqa: BLE001 - state is NOT advanced: the next pass re-sends
            logger.error("dispatch failed (%s); state not advanced", type(exc).__name__)
            result.summary = f"dispatch failed {counts}"
            return result
    if notifications and not any(
        isinstance(r, dict) and r.get("channel") in ("telegram", "admin_email") and r.get("status") != "skipped:not_configured"
        for r in (rows if isinstance(rows, list) else [])
    ):
        logger.warning("no channel configured: %d notification(s) could not be delivered", len(notifications))
    apply_results(p, notifications, rows)
    _prune(p)
    p.state["tick"] = _iso(now)
    try:
        store.save(p.state, p.incidents)
    except OSError as exc:
        logger.error("state directory not writable (%s)", type(exc).__name__)
        result.exit_code = 1
        result.summary = "state directory not writable"
        return result
    result.persisted = True
    result.summary = counts
    logger.info("sentinel pass (%s)", counts)

    if cfg.heartbeat_url:
        try:
            deps.heartbeat(cfg.heartbeat_url)
        except Exception as exc:  # noqa: BLE001 - logged only, never fails the pass; the URL is a secret
            logger.warning("heartbeat failed (%s)", type(exc).__name__)
    return result


def main(argv: Optional[List[str]] = None, deps: Optional[Deps] = None, env: Optional[Dict[str, str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel", description="Hermes token-exhaustion sentinel (one detection pass).")
    parser.add_argument("--once", action="store_true", help="run exactly one pass (the only mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="detect and print the notifications that WOULD be sent; no dispatch, no persistence")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore"):  # their INFO lines carry full URLs (the heartbeat URL is a secret)
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        result = run_pass(Config.from_env(env), deps, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - a pass never crashes
        logger.error("sentinel pass aborted (%s)", type(exc).__name__)
        return 0
    if args.dry_run:
        for note in result.notifications:
            inc = note["incident"]
            print(f"{inc['incident_key']} {inc['severity']} {note['event']}")
        print(result.summary)
    return result.exit_code
