"""
flow_state.py - sentinel flow-state writer (contract C1, docs/sprints/SENTINEL_T1_T5_SPECS.md section 2).

After each GTM tool result, consultation.py calls record_result(); this module writes
$HERMES_SENTINEL_DIR/flow/<GTM-ID>.json saying who owes the next step and, when the agent owes it, a deadline.
The sentinel detector (T2) reads these files and raises flow_stalled when an agent-owed deadline passes.

Rules:
  * Never raises. Any failure is logged at WARNING and swallowed; callers must not depend on the outcome.
  * Stores only the public GTM-ID, the step name, the outcome code, owed_by, deadline, updated_at and seq.
    No account/workspace ids, no spec, no args, no text.
  * Atomic writes (tmp + os.replace), file 0600, directories 0700 (permission bits are skipped on Windows).
  * Standalone: stdlib only, no sibling imports (consultation.py may load this file by path on the VPS).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("hermes.sentinel.flow_state")

DEFAULT_SENTINEL_DIR = "/home/deploy/.hermes/sentinel"
DEFAULT_FLOW_DEADLINE_S = 300
PUBLIC_ID_RE = re.compile(r"^GTM-[A-Z0-9]{4,10}$")

AGENT, CLIENT, OPERATOR, HUMAN, NONE = "agent", "client", "operator", "human", "none"

# Reason codes that mean the merchant must act (the agent relays client_action_* text). Audience "client" in gtm_api.
_CLIENT_CODES = frozenset({"NOT_INVITED", "PERMISSION_MISSING", "CONTAINER_NOT_FOUND", "INVALID_ID", "WORKSPACE_LIMIT"})

# Explicit (step, outcome) -> owed_by. Anything not listed here or in _CLIENT_CODES is "operator" (never "agent").
_EXPLICIT: Dict[Tuple[str, str], str] = {
    # verify_access (gtm_api.verify_access / hermes_gtm_verify_access)
    ("verify_access", "CONNECTED"): AGENT,
    ("verify_access", "RETRYABLE"): AGENT,               # Google 5xx: transient, the agent may retry
    ("verify_access", "RETRYABLE_QUOTA"): OPERATOR,      # Tag Manager quota: operator-side, not an agent retry
    # workspace_apply (gtm_api.apply_container_spec / hermes_gtm_workspace_apply)
    ("workspace_apply", "applied"): HUMAN,               # next GTM call needs the operator's D-003 token; no deadline
    ("workspace_apply", "LINT_FAILED"): AGENT,           # the agent fixes the spec and re-applies
    ("workspace_apply", "RETRYABLE"): AGENT,
    ("workspace_apply", "RETRYABLE_QUOTA"): OPERATOR,
    # cloud_publish (gtm_api.create_and_publish_version / hermes_gtm_cloud_publish)
    ("cloud_publish", "published"): NONE,
    ("cloud_publish", "AWAITING_HITL_APPROVAL"): HUMAN,  # missing / expired / rejected D-003 token
    ("cloud_publish", "RETRYABLE"): OPERATOR,            # publish is non-idempotent: retry needs a new human token
    ("cloud_publish", "RETRYABLE_QUOTA"): OPERATOR,
}

# Operator-side outcomes listed for documentation; they resolve to "operator" through the default.
OPERATOR_CODES = frozenset({
    "CREDENTIALS_PENDING", "API_DISABLED", "SCOPE_INSUFFICIENT", "UNKNOWN", "INVALID_SPEC", "LINTER_UNAVAILABLE",
    "MERGE_CONFLICT", "COMPILER_ERROR", "HITL_KEY_NOT_CONFIGURED", "RETRYABLE_QUOTA",
})

_lock = threading.Lock()


def owed_by(step: Any, outcome: Any) -> str:
    """Maps a (step, outcome) pair to owed_by. Unknown pairs are "operator"."""
    key = (str(step), str(outcome))
    if key in _EXPLICIT:
        return _EXPLICIT[key]
    if str(outcome) in _CLIENT_CODES:
        return CLIENT
    return OPERATOR


def outcome_of(payload: Any) -> str:
    """Derives the outcome code from a GTM tool result (dict or the JSON string the tool returned). Never raises."""
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
        if not isinstance(data, dict):
            return "UNKNOWN"
        status = data.get("status")
        if status == "AWAITING_HITL_APPROVAL":
            if data.get("approval_check") == "verify_key_not_configured":
                return "HITL_KEY_NOT_CONFIGURED"            # VPS config problem, not a human approval
            return "AWAITING_HITL_APPROVAL"
        if data.get("reason_code") == "RETRYABLE" and data.get("quota_exhausted"):
            return "RETRYABLE_QUOTA"
        if isinstance(data.get("reason_code"), str) and data["reason_code"]:
            return data["reason_code"]
        if status in ("CONNECTED", "applied"):
            return status
        if status == "success":
            return "published"
        return "UNKNOWN"
    except Exception:  # noqa: BLE001 - a result we cannot read is recorded as an operator-side unknown
        return "UNKNOWN"


def _sentinel_dir() -> Path:
    return Path(os.environ.get("HERMES_SENTINEL_DIR", "").strip() or DEFAULT_SENTINEL_DIR)


def _deadline_s() -> float:
    try:
        value = float(os.environ.get("SENTINEL_FLOW_DEADLINE_S", "").strip() or DEFAULT_FLOW_DEADLINE_S)
        return value if value >= 0 else float(DEFAULT_FLOW_DEADLINE_S)
    except ValueError:
        return float(DEFAULT_FLOW_DEADLINE_S)


def _utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_seq(path: Path) -> int:
    """Previous seq + 1. A missing file starts at 1. An unreadable or corrupt file gets int(time.time()) instead,
    so a new flow:<id>:<seq> incident key cannot collide with one written before the corruption."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        seq = data.get("seq") if isinstance(data, dict) else None
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            return seq + 1
    except FileNotFoundError:
        return 1
    except (OSError, ValueError):
        pass
    return int(time.time())


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def record(container: Any, step: Any, outcome: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Writes the flow state for one container. Returns the stored payload, or None if nothing was written.

    `container` is the public GTM-ID (GTM-XXXXXXX). Never raises."""
    try:
        if not isinstance(container, str) or not PUBLIC_ID_RE.match(container):
            logger.warning("flow_state: record skipped (container is not a public GTM-ID)")
            return None
        when = _utc(now)
        owed = owed_by(step, outcome)
        deadline = _iso(when + timedelta(seconds=_deadline_s())) if owed == AGENT else None
        flow_dir = _sentinel_dir() / "flow"
        with _lock:
            _ensure_private_dir(flow_dir)
            path = flow_dir / f"{container}.json"
            payload = {
                "container": container,
                "step": str(step),
                "outcome": str(outcome),
                "owed_by": owed,
                "deadline": deadline,
                "updated_at": _iso(when),
                "seq": _next_seq(path),
            }
            _atomic_write_json(path, payload)
        return payload
    except Exception as exc:  # noqa: BLE001 - the sentinel never raises into the router or the tools
        logger.warning("flow_state: record failed (%s)", type(exc).__name__)
        return None


def record_result(container: Any, step: Any, payload: Any) -> Optional[Dict[str, Any]]:
    """Derives the outcome from a GTM tool result and records it. Never raises."""
    try:
        return record(container, step, outcome_of(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("flow_state: record_result failed (%s)", type(exc).__name__)
        return None
