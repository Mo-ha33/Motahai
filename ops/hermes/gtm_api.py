"""
gtm_api.py - Google Tag Manager API v2 logic for the Hermes MCP tools (headless, service-account based).

Pure functions that take an injected `service` (a googleapiclient Tag Manager v2 resource) so they can be
unit-tested with a fake. Nothing here touches the network on import; `build_service` is the only function
that creates a real client and it imports the Google libraries lazily.

Deployed next to consultation.py (same package directory on the VPS). The thin MCP wrappers live in
consultation.py; the Rule D-003 human-approval gate also stays there (it must wrap every production publish).

Tools backed by this module:
  * hermes_gtm_verify_access   -> resolve_container()               (read-only, no approval)
  * hermes_gtm_workspace_apply -> apply_container_spec()            (draft workspace only, no approval)
  * hermes_gtm_cloud_publish   -> create_and_publish_version()      (production; D-003 gate in consultation.py)
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVICE_ACCOUNT_EMAIL = "tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com"
GCP_PROJECT = "agentic-ai-494313"

SCOPE_READONLY = "https://www.googleapis.com/auth/tagmanager.readonly"
SCOPE_EDIT_CONTAINERS = "https://www.googleapis.com/auth/tagmanager.edit.containers"
SCOPE_EDIT_VERSIONS = "https://www.googleapis.com/auth/tagmanager.edit.containerversions"
SCOPE_PUBLISH = "https://www.googleapis.com/auth/tagmanager.publish"

PUBLIC_ID_RE = re.compile(r"^GTM-[A-Z0-9]{4,10}$")

# Entity kinds written by workspace_apply, in dependency order (variables -> triggers -> tags).
# (spec key, API collection method, list-response key, id field)
_KINDS: Tuple[Tuple[str, str, str, str], ...] = (
    ("variables", "variables", "variable", "variableId"),
    ("triggers", "triggers", "trigger", "triggerId"),
    ("tags", "tags", "tag", "tagId"),
)

# Server-assigned / read-only fields that must never be sent back in a request body.
_READONLY_KEYS = {
    "accountId", "containerId", "workspaceId", "tagId", "triggerId", "variableId",
    "fingerprint", "path", "tagManagerUrl",
}
# Helper keys of the spec format that are resolved into API fields before sending.
_SPEC_ONLY_KEYS = {"firingTriggerName", "blockingTriggerName"}

# Built-in (reserved) trigger ids; tags may also name them in firingTriggerName. These match the range the
# gtm-container-linter treats as built-in (>= 2147479000).
BUILTIN_TRIGGER_IDS = {
    "All Pages": "2147479553",
    "Initialization - All Pages": "2147479573",
    "Consent Initialization - All Pages": "2147479572",
}

MAX_SPEC_BYTES = 500_000
MAX_ENTITIES_PER_KIND = 200

UPSTREAM_HOST = "tagmanager.googleapis.com"


def _env_number(name: str, default: Any, cast: Callable[[str], Any]) -> Any:
    """Reads a numeric env var; a missing/garbled value falls back to the default (never raises)."""
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# GTM API v2 has a very low default per-user rate quota (historically ~25 queries / 100 s).
NUM_RETRIES = _env_number("GTM_API_NUM_RETRIES", 4, int)            # googleapiclient backoff on 429/5xx/403 rateLimit
MIN_INTERVAL_S = _env_number("GTM_API_MIN_INTERVAL_S", 0.0, float)  # process-wide gap between API calls (0 = off)

_PACE_LOCK = threading.Lock()
_last_call_at: Optional[float] = None


def _pace() -> None:
    """Sleeps only the remaining gap since the previous GTM API call (thread-safe; no-op when MIN_INTERVAL_S <= 0)."""
    global _last_call_at
    interval = MIN_INTERVAL_S
    if interval <= 0:
        return
    with _PACE_LOCK:
        now = time.monotonic()
        if _last_call_at is not None:
            gap = interval - (now - _last_call_at)
            if gap > 0:
                time.sleep(gap)
                now = time.monotonic()
        _last_call_at = now


def _execute(request, num_retries: Optional[int] = None):
    """The single choke point for every Tag Manager call: client-side pacing + googleapiclient retry/backoff.

    Pass num_retries=0 for non-idempotent calls (create_version, publish) so a version is never double-created."""
    _pace()
    return request.execute(num_retries=NUM_RETRIES if num_retries is None else num_retries)


# ---------------------------------------------------------------------------------------------------------
# T1 - error classification
# ---------------------------------------------------------------------------------------------------------
def _client_invite_action() -> Dict[str, str]:
    return {
        "client_action_ar": (
            "يرجى فتح Google Tag Manager ثم الإدارة (Admin) ثم إدارة المستخدمين على مستوى الحاوية "
            f"(Container > User Management)، وإضافة البريد {SERVICE_ACCOUNT_EMAIL} بصلاحية «نشر» (Publish)، "
            "ثم الضغط على «دعوة» أو «حفظ» (Invite / Save). نشكر لكم تعاونكم."
        ),
        "client_action_en": (
            "Please open Google Tag Manager > Admin > Container > User Management, add "
            f"{SERVICE_ACCOUNT_EMAIL} with container permission \"Publish\", then press Invite / Save."
        ),
    }


def _client_not_found_action() -> Dict[str, str]:
    return {
        "client_action_ar": (
            "لم يتم العثور على الحاوية. يرجى التأكد من صحة معرّف الحاوية (GTM-XXXXXXX) ومن حفظ دعوة المستخدم "
            f"{SERVICE_ACCOUNT_EMAIL} بصلاحية «نشر» (Publish) في Admin > Container > User Management، ثم إعلامنا."
        ),
        "client_action_en": (
            "The container could not be found. Please double-check the container ID (GTM-XXXXXXX) and make sure "
            f"the invitation for {SERVICE_ACCOUNT_EMAIL} with \"Publish\" permission was saved in "
            "Admin > Container > User Management, then let us know."
        ),
    }


def _client_workspace_limit_action() -> Dict[str, str]:
    return {
        "client_action_ar": (
            "حاوية Google Tag Manager وصلت للحد الأقصى من مساحات العمل (Workspaces) وفيها تعديلات غير منشورة. "
            "يرجى فتح قائمة Workspaces (أعلى يسار الصفحة) ثم نشر أو حذف مساحة عمل واحدة غير مستخدمة "
            "(لا تحتاجون لصفحة Admin)، ثم إعلامنا لنتابع."
        ),
        "client_action_en": (
            "Your Google Tag Manager container reached its workspace limit and the default workspace has unpublished "
            "changes. Please open the Workspaces list (top-left of the GTM page) and publish or delete one unused "
            "workspace (Admin access is not needed), then let us know and we will continue."
        ),
    }


def _client_invalid_id_action() -> Dict[str, str]:
    return {
        "client_action_ar": "يرجى إرسال معرّف الحاوية بالصيغة الصحيحة، مثل GTM-ABC1234 (تجدونه أعلى صفحة Google Tag Manager).",
        "client_action_en": "Please send the container ID in the form GTM-ABC1234 (shown at the top of the Google Tag Manager page).",
    }


def error_response(reason_code: str, audience: str, detail: str, retryable: bool = False, **extra: Any) -> Dict[str, Any]:
    """Builds the standard error envelope. Client-audience codes carry bilingual client_action_* text."""
    out: Dict[str, Any] = {
        "status": "error",
        "reason_code": reason_code,
        "audience": audience,
        "retryable": retryable,
        "detail": detail,
    }
    if audience == "client":
        if reason_code == "NOT_INVITED" or reason_code == "PERMISSION_MISSING":
            out.update(_client_invite_action())
        elif reason_code == "CONTAINER_NOT_FOUND":
            out.update(_client_not_found_action())
        elif reason_code == "WORKSPACE_LIMIT":
            out.update(_client_workspace_limit_action())
        elif reason_code == "INVALID_ID":
            out.update(_client_invalid_id_action())
    out.update(extra)
    return out


def _http_status_and_body(exc: BaseException) -> Tuple[Optional[int], str]:
    """Duck-types googleapiclient.errors.HttpError (so this works whether or not the library is installed)."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        return None, ""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return None, ""
    content = getattr(exc, "content", b"")
    if isinstance(content, (bytes, bytearray)):
        content = bytes(content).decode("utf-8", errors="replace")
    body = content if isinstance(content, str) and content.strip() else str(exc)
    return status, body


def _google_message(body: str, status: int) -> str:
    """Short, human-readable message from a Google JSON error body (never the raw payload)."""
    try:
        data = json.loads(body)
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
        if isinstance(message, str) and message.strip():
            return f"HTTP {status}: {message.strip()[:300]}"
    except (ValueError, AttributeError):
        pass
    return f"HTTP {status}"


def _retry_after_s(body: str) -> Optional[float]:
    """Seconds from a Google RetryInfo detail (`retryDelay` like "30s" / "1.5s"), else None."""
    try:
        data = json.loads(body)
        details = data.get("error", {}).get("details", []) if isinstance(data, dict) else []
        for item in details if isinstance(details, list) else []:
            if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("RetryInfo"):
                continue
            m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(item.get("retryDelay", "")))
            if m:
                value = float(m.group(1))
                return int(value) if value.is_integer() else value
    except (ValueError, AttributeError):
        pass
    return None


def _quota_response(detail: str, body: str) -> Dict[str, Any]:
    """RETRYABLE (the agent prompt branches on it) plus fields that make a Tag Manager quota hit unmistakable
    versus an LLM-provider 429."""
    extra: Dict[str, Any] = {"upstream": UPSTREAM_HOST, "quota_exhausted": True}
    retry_after = _retry_after_s(body)
    if retry_after is not None:
        extra["retry_after_s"] = retry_after
    return error_response(
        "RETRYABLE", "operator",
        f"{detail} - Google Tag Manager API ({UPSTREAM_HOST}) quota exhausted; this is not an LLM-provider limit.",
        retryable=True, **extra)


def classify_gtm_error(exc: BaseException) -> Dict[str, Any]:
    """Maps an exception from the Tag Manager API (or anything else) to a structured, safe error dict."""
    status, body = _http_status_and_body(exc)
    if status is None:
        return error_response("UNKNOWN", "operator", f"{type(exc).__name__}: {str(exc)[:200]}")

    low = body.lower()
    detail = _google_message(body, status)
    if status == 403:
        if "insufficient authentication scopes" in low or "access_token_scope_insufficient" in low:
            return error_response(
                "SCOPE_INSUFFICIENT", "operator",
                f"{detail} - the OAuth scopes requested by this tool are not sufficient for the call.")
        if "accessnotconfigured" in low or "service_disabled" in low or "has not been used in project" in low:
            return error_response(
                "API_DISABLED", "operator",
                f"{detail} - enable the Tag Manager API on GCP project {GCP_PROJECT}.")
        if "ratelimitexceeded" in low or "quotaexceeded" in low or "resource_exhausted" in low:
            return _quota_response(detail, body)
        return error_response("PERMISSION_MISSING", "client", detail)
    if status == 429 or "resource_exhausted" in low:
        return _quota_response(detail, body)
    if status == 404:
        return error_response("CONTAINER_NOT_FOUND", "client", detail)
    if 500 <= status <= 599:
        return error_response("RETRYABLE", "operator", detail, retryable=True)
    return error_response("UNKNOWN", "operator", detail)


# ---------------------------------------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------------------------------------
def build_service(key_path: str, scopes: List[str]):
    """Real Tag Manager v2 client for the service account at `key_path` (Google libs imported lazily)."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(key_path, scopes=scopes)
    return build("tagmanager", "v2", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------------------------------------
# T2 - resolve a public GTM-XXXXXXX id to numeric ids
# ---------------------------------------------------------------------------------------------------------
def _paginate(call: Callable[..., Any], key: str, **kwargs: Any) -> List[Dict[str, Any]]:
    """Collects every page of a Tag Manager list call. `call(**kwargs, pageToken=...)` returns a request."""
    items: List[Dict[str, Any]] = []
    token: Optional[str] = None
    for _ in range(1000):  # hard stop against a misbehaving nextPageToken loop
        page_kwargs = dict(kwargs)
        if token:
            page_kwargs["pageToken"] = token
        res = _execute(call(**page_kwargs)) or {}
        items.extend(res.get(key, []) or [])
        token = res.get("nextPageToken")
        if not token:
            break
    return items


def validate_public_id(public_container_id: Any) -> Optional[Dict[str, Any]]:
    """Returns an INVALID_ID error dict if the id is not GTM-XXXXXXX, else None."""
    if not isinstance(public_container_id, str) or not PUBLIC_ID_RE.match(public_container_id):
        return error_response("INVALID_ID", "client", "Container ID must match GTM-XXXXXXX (uppercase letters/digits).")
    return None


def pick_default_workspace(workspaces: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """'Default Workspace' if present, else the lowest numeric workspaceId."""
    if not workspaces:
        return None
    for ws in workspaces:
        if ws.get("name") == "Default Workspace":
            return ws

    def sort_key(ws: Dict[str, Any]):
        wid = str(ws.get("workspaceId", ""))
        return (0, int(wid), wid) if wid.isdigit() else (1, 0, wid)

    return sorted(workspaces, key=sort_key)[0]


def tariq_workspace_name(public_id: str) -> str:
    """Dedicated workspace used for our writes, so a merchant's unpublished edits are never released with ours."""
    return f"Tariq - {public_id}"


def resolve_container(service, public_id: str) -> Optional[Dict[str, Any]]:
    """Finds the container with this publicId among the accounts the service account can see.

    Returns None when not found (not invited). Raises on API errors (caller classifies)."""
    accounts_api = service.accounts()
    for account in _paginate(accounts_api.list, "account"):
        account_id = str(account.get("accountId", ""))
        if not account_id:
            continue
        containers = _paginate(accounts_api.containers().list, "container", parent=f"accounts/{account_id}")
        for container in containers:
            if container.get("publicId") != public_id:
                continue
            container_id = str(container.get("containerId", ""))
            workspaces = _paginate(
                accounts_api.containers().workspaces().list, "workspace",
                parent=f"accounts/{account_id}/containers/{container_id}")
            default_ws = pick_default_workspace(workspaces)
            tariq_ws = next((w for w in workspaces if w.get("name") == tariq_workspace_name(public_id)), None)
            return {
                "account_id": account_id,
                "container_id": container_id,
                "container_name": container.get("name", ""),
                "usage_context": container.get("usageContext", []),
                "default_workspace_id": str(default_ws.get("workspaceId")) if default_ws else None,
                "tariq_workspace_id": str(tariq_ws.get("workspaceId")) if tariq_ws else None,
            }
    return None


def not_invited_response() -> Dict[str, Any]:
    return error_response(
        "NOT_INVITED", "client",
        f"The service account cannot see this container; {SERVICE_ACCOUNT_EMAIL} has not been added to it.")


# Stale-if-error cache for the READ-ONLY resolve only (verify_access). Never used by apply/publish paths and never
# holds credentials: just the public id -> account/container/workspace mapping. Every cache IO error is swallowed.
def _cache_dir() -> Path:
    env = os.environ.get("GTM_CACHE_DIR", "").strip()
    return Path(env) if env else Path.home() / ".hermes" / "cache" / "gtm-resolve"


def _cache_path(public_id: str) -> Path:
    return _cache_dir() / f"{public_id}.json"      # public_id already matched PUBLIC_ID_RE (no path separators)


def _cache_read(public_id: str) -> Optional[Tuple[float, Dict[str, Any]]]:
    """Returns (age_seconds, result) of a cached CONNECTED response, or None."""
    try:
        data = json.loads(_cache_path(public_id).read_text(encoding="utf-8"))
        ts, result = float(data["ts"]), data["result"]
        if not isinstance(result, dict) or result.get("status") != "CONNECTED":
            return None
        age = time.time() - ts
        if age < -60:       # timestamp from the future: clock skew or corruption
            return None
        return max(age, 0.0), result
    except Exception:  # noqa: BLE001 - a broken cache is just a miss
        return None


def _cache_write(public_id: str, result: Dict[str, Any]) -> None:
    tmp: Optional[Path] = None
    try:
        path = _cache_path(public_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "result": result}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:  # noqa: BLE001
                pass


def _cache_invalidate(public_id: str) -> None:
    try:
        _cache_path(public_id).unlink()
    except Exception:  # noqa: BLE001
        pass


def verify_access(service, public_container_id: str) -> Dict[str, Any]:
    bad = validate_public_id(public_container_id)
    if bad:
        return bad
    fresh_s = _env_number("GTM_RESOLVE_FRESH_S", 600.0, float)
    stale_s = _env_number("GTM_RESOLVE_STALE_S", 86400.0, float)

    if fresh_s > 0:
        hit = _cache_read(public_container_id)
        if hit and hit[0] < fresh_s:
            return {**hit[1], "cache": "fresh", "cache_age_s": int(hit[0])}

    try:
        found = resolve_container(service, public_container_id)
    except Exception as exc:  # noqa: BLE001 - classified, never raw
        err = classify_gtm_error(exc)
        if err.get("reason_code") == "RETRYABLE" and stale_s > 0:
            hit = _cache_read(public_container_id)
            if hit and hit[0] < stale_s:
                return {**hit[1], "cache": "stale", "cache_age_s": int(hit[0]), "stale_reason": err["reason_code"]}
        elif err.get("audience") == "client":
            _cache_invalidate(public_container_id)      # access revoked / id wrong: never keep serving the old mapping
        return err
    if found is None:
        _cache_invalidate(public_container_id)
        return not_invited_response()
    result = {
        "status": "CONNECTED",
        "public_id": public_container_id,
        **found,
        "note": "Publish permission is confirmed at release time; a 403 then maps to PERMISSION_MISSING.",
    }
    if fresh_s > 0 or stale_s > 0:
        _cache_write(public_container_id, result)
    return result


# ---------------------------------------------------------------------------------------------------------
# T3 - apply a container spec to the draft workspace
# ---------------------------------------------------------------------------------------------------------
_LINTER_ENV = "HERMES_GTM_LINTER_PATH"
_LINTER_REL = Path("skills") / "gtm-container-linter" / "scripts" / "lint_container.py"
_LINTER_MODULE_NAME = "hermes_gtm_container_linter"


def _linter_candidates() -> List[Path]:
    out: List[Path] = []
    env = os.environ.get(_LINTER_ENV, "").strip()
    if env:
        out.append(Path(env))
    here = Path(__file__).resolve().parent
    out.append(here / "home" / _LINTER_REL)                       # repo layout: ops/hermes/home/skills/...
    out.append(Path.home() / ".hermes" / _LINTER_REL)             # deployed layout: ~/.hermes/skills/...
    out.append(Path("/home/deploy/.hermes") / _LINTER_REL)
    return out


def _load_linter():
    """Imports gtm-container-linter's lint_container module, or returns None (callers fail closed)."""
    cached = sys.modules.get(_LINTER_MODULE_NAME)
    if cached is not None:
        return cached
    for path in _linter_candidates():
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location(_LINTER_MODULE_NAME, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[_LINTER_MODULE_NAME] = module   # dataclasses need the module registered while executing
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(_LINTER_MODULE_NAME, None)
                raise
            if hasattr(module, "Linter"):
                return module
        except Exception:  # noqa: BLE001 - unusable linter == unavailable
            continue
    return None


def _parse_spec(container_spec: Any) -> Tuple[Optional[Dict[str, List[Dict[str, Any]]]], Optional[str]]:
    """Returns (spec, None) or (None, error message)."""
    if not isinstance(container_spec, str) or not container_spec.strip():
        return None, "container_spec must be a non-empty JSON string."
    if len(container_spec.encode("utf-8")) > MAX_SPEC_BYTES:
        return None, f"container_spec exceeds {MAX_SPEC_BYTES} bytes."
    try:
        spec = json.loads(container_spec)
    except ValueError as exc:
        return None, f"container_spec is not valid JSON: {exc}"
    if not isinstance(spec, dict):
        return None, "container_spec must be a JSON object with variables/triggers/tags arrays."
    unknown = set(spec) - {"variables", "triggers", "tags"}
    if unknown:
        return None, f"Unknown top-level keys: {sorted(unknown)}."
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key in ("variables", "triggers", "tags"):
        items = spec.get(key, [])
        if not isinstance(items, list):
            return None, f"'{key}' must be an array."
        if len(items) > MAX_ENTITIES_PER_KIND:
            return None, f"'{key}' has more than {MAX_ENTITIES_PER_KIND} entries."
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                return None, f"Every entry of '{key}' must be an object."
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                return None, f"Every entry of '{key}' needs a non-empty string 'name'."
            if not isinstance(item.get("type"), str) or not item["type"].strip():
                return None, f"'{key}' entry '{name}' needs a 'type'."
            if name in seen:
                return None, f"Duplicate name '{name}' in '{key}'."
            seen.add(name)
        out[key] = items
    if not any(out.values()):
        return None, "container_spec is empty (no variables, triggers or tags)."
    return out, None


def _strip(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in entity.items() if k not in _READONLY_KEYS and k not in _SPEC_ONLY_KEYS}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    return [str(v) for v in value]


def _resolve_tag_triggers(tag: Dict[str, Any], id_by_name: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    """Returns (body with firingTriggerId/blockingTriggerId filled in, unresolved trigger names)."""
    body = _strip(tag)
    missing: List[str] = []
    for id_key, name_key in (("firingTriggerId", "firingTriggerName"), ("blockingTriggerId", "blockingTriggerName")):
        ids = _as_list(tag.get(id_key))
        for trigger_name in _as_list(tag.get(name_key)):
            if trigger_name in id_by_name:
                ids.append(id_by_name[trigger_name])
            else:
                missing.append(trigger_name)
        deduped = list(dict.fromkeys(ids))
        if deduped:
            body[id_key] = deduped
        else:
            body.pop(id_key, None)
    return body, missing


def _slim_finding(f: Dict[str, Any]) -> Dict[str, Any]:
    return {k: f.get(k) for k in ("rule", "severity", "entity_type", "entity_name", "message", "fix")}


def _lint_merged(linter, existing: Dict[str, List[Dict[str, Any]]], builtins: List[Dict[str, Any]],
                 spec: Dict[str, List[Dict[str, Any]]], id_by_name: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Lints the workspace as it would look after the apply (existing entities overlaid by the spec).

    Returns (blocking, other). Blocking = critical/high findings on spec entities or on the container as a
    whole; defects in untouched pre-existing entities are reported as `other` but do not block."""
    spec_names = {e["name"] for key in ("variables", "triggers", "tags") for e in spec[key]}
    cv: Dict[str, Any] = {"builtInVariable": builtins}
    for key, _method, _list_key, id_field in _KINDS:
        replaced = {e["name"] for e in spec[key]}
        merged = [dict(e) for e in existing[key] if e.get("name") not in replaced]
        for entity in spec[key]:
            body = _resolve_tag_triggers(entity, id_by_name)[0] if key == "tags" else _strip(entity)
            body["name"] = entity["name"]
            body[id_field] = next((str(e[id_field]) for e in existing[key] if e.get("name") == entity["name"] and e.get(id_field)),
                                  f"new:{key}:{entity['name']}")
            merged.append(body)
        cv[{"variables": "variable", "triggers": "trigger", "tags": "tag"}[key]] = merged

    findings = [dataclasses.asdict(f) for f in linter.Linter(cv).run()]
    blocking: List[Dict[str, Any]] = []
    other: List[Dict[str, Any]] = []
    for f in findings:
        names = {n.strip() for n in str(f.get("entity_name", "")).split(", ")}
        on_spec = bool(names & spec_names) or f.get("entity_type") == "container"
        if f.get("severity") in ("critical", "high") and on_spec:
            blocking.append(_slim_finding(f))
        else:
            other.append(_slim_finding(f))
    return blocking, other


def _list_workspace(service, ws_path: str) -> Dict[str, List[Dict[str, Any]]]:
    ws_api = service.accounts().containers().workspaces()
    return {
        key: _paginate(getattr(ws_api, method)().list, list_key, parent=ws_path)
        for key, method, list_key, _id in _KINDS
    }


_LIMIT_STATUSES = (400, 403, 409)


def _is_workspace_limit_error(exc: BaseException) -> bool:
    """Liberal match: an HTTP 400/403/409 whose body mentions both 'workspace' and a limit/maximum."""
    status, body = _http_status_and_body(exc)
    if status not in _LIMIT_STATUSES:
        return False
    low = body.lower()
    return "workspace" in low and any(w in low for w in ("limit", "maximum", "max ", "too many"))


def _choose_workspace(service, public_id: str, found: Dict[str, Any]) -> Dict[str, Any]:
    """Returns {"id","name","mode"} or an error envelope. Raises for unexpected API errors (caller classifies)."""
    name = tariq_workspace_name(public_id)
    if found.get("tariq_workspace_id"):
        return {"id": found["tariq_workspace_id"], "name": name, "mode": "dedicated"}

    account_id, container_id = found["account_id"], found["container_id"]
    ws_api = service.accounts().containers().workspaces()
    try:
        created = _execute(ws_api.create(
            parent=f"accounts/{account_id}/containers/{container_id}",
            body={"name": name, "description": "Created by Tariq (Hermes). Isolated from merchant drafts; released only with human approval."},
        )) or {}
        if not created.get("workspaceId"):
            raise ValueError("workspace create returned no workspaceId")
        return {"id": str(created["workspaceId"]), "name": name, "mode": "dedicated"}
    except Exception as exc:  # noqa: BLE001
        if not _is_workspace_limit_error(exc):
            raise

    # Workspace limit reached: the default workspace may be used ONLY if it has no unpublished changes.
    default_id = found.get("default_workspace_id")
    if default_id:
        default_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{default_id}"
        status = _execute(ws_api.getStatus(path=default_path)) or {}
        if not status.get("workspaceChange"):
            return {"id": default_id, "name": "Default Workspace", "mode": "default_fallback"}
    return error_response(
        "WORKSPACE_LIMIT", "client",
        "Workspace limit reached and the default workspace has unpublished changes (or no default exists); nothing was written.")


def apply_container_spec(service, public_container_id: str, container_spec: str,
                         linter_loader: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """Idempotent upsert (by name) of variables -> triggers -> tags into a dedicated "Tariq - GTM-XXXX" workspace.

    Falls back to the default workspace only when the container is at its workspace limit AND that workspace has
    no unpublished changes. Writes only to a draft workspace; nothing goes live (publishing is a separate, human-approved step)."""
    bad = validate_public_id(public_container_id)
    if bad:
        return bad
    spec, problem = _parse_spec(container_spec)
    if problem:
        return error_response("INVALID_SPEC", "operator", problem)

    # Fail closed: no linter, no writes. Checked before any API call.
    linter = (linter_loader or _load_linter)()
    if linter is None:
        return error_response("LINTER_UNAVAILABLE", "operator",
                              "gtm-container-linter could not be imported; refusing to write unlinted configuration.")

    written: Dict[str, List[Dict[str, str]]] = {"variables": [], "triggers": [], "tags": []}
    try:
        found = resolve_container(service, public_container_id)
        if found is None:
            return not_invited_response()
        account_id, container_id = found["account_id"], found["container_id"]
        chosen = _choose_workspace(service, public_container_id, found)
        if "status" in chosen:      # error envelope (WORKSPACE_LIMIT)
            return chosen
        workspace_id, workspace_name, workspace_mode = chosen["id"], chosen["name"], chosen["mode"]
        ws_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"

        existing = _list_workspace(service, ws_path)
        builtins = _paginate(service.accounts().containers().workspaces().built_in_variables().list,
                             "builtInVariable", parent=ws_path)

        # Plan: trigger name -> id (existing ids, placeholders for triggers the spec will create).
        id_by_name: Dict[str, str] = dict(BUILTIN_TRIGGER_IDS)
        id_by_name.update({t["name"]: str(t["triggerId"]) for t in existing["triggers"]
                           if t.get("name") and t.get("triggerId")})
        for trig in spec["triggers"]:
            if trig["name"] not in id_by_name or trig["name"] in BUILTIN_TRIGGER_IDS:
                id_by_name[trig["name"]] = f"new:triggers:{trig['name']}"
        unresolved = sorted({m for tag in spec["tags"] for m in _resolve_tag_triggers(tag, id_by_name)[1]})
        if unresolved:
            return error_response("INVALID_SPEC", "operator",
                                  f"Tags reference triggers that exist neither in the spec nor in the workspace: {unresolved}.")

        blocking, other = _lint_merged(linter, existing, builtins, spec, id_by_name)
        if blocking:
            return error_response("LINT_FAILED", "operator",
                                  f"{len(blocking)} blocking finding(s) from gtm-container-linter; nothing was written.",
                                  findings=blocking[:25])

        created = {"variables": 0, "triggers": 0, "tags": 0}
        updated = {"variables": 0, "triggers": 0, "tags": 0}
        unchanged = {"variables": 0, "triggers": 0, "tags": 0}
        ws_api = service.accounts().containers().workspaces()
        # Real ids of triggers as they exist (or get created) during this run.
        live_trigger_ids: Dict[str, str] = dict(BUILTIN_TRIGGER_IDS)
        live_trigger_ids.update({t["name"]: str(t["triggerId"]) for t in existing["triggers"]
                                 if t.get("name") and t.get("triggerId")})

        for key, method, _list_key, id_field in _KINDS:
            collection = getattr(ws_api, method)()
            by_name = {e["name"]: e for e in existing[key] if e.get("name")}
            for entity in spec[key]:
                name = entity["name"]
                if key == "tags":
                    body, missing = _resolve_tag_triggers(entity, live_trigger_ids)
                    if missing:  # cannot happen after the plan check; stay safe anyway
                        raise ValueError(f"unresolved trigger names: {missing}")
                else:
                    body = _strip(entity)
                body["name"] = name
                current = by_name.get(name)
                try:
                    if current is None:
                        result = _execute(collection.create(parent=ws_path, body=body)) or {}
                        created[key] += 1
                        action = "created"
                    elif all(current.get(k) == v for k, v in body.items()):
                        result = current
                        unchanged[key] += 1
                        action = "unchanged"
                    else:
                        path = current.get("path") or f"{ws_path}/{method}/{current.get(id_field)}"
                        result = _execute(collection.update(path=path, body=body, fingerprint=current.get("fingerprint"))) or {}
                        updated[key] += 1
                        action = "updated"
                except Exception as exc:  # noqa: BLE001
                    err = classify_gtm_error(exc)
                    err.update({"failed_entity": {"type": key, "name": name}, "already_written": written,
                                "account_id": account_id, "container_id": container_id, "workspace_id": workspace_id})
                    return err
                written[key].append({"name": name, "action": action})
                if key == "triggers":
                    trig_id = result.get(id_field) or (current or {}).get(id_field)
                    if trig_id:
                        live_trigger_ids[name] = str(trig_id)

        _cache_invalidate(public_container_id)      # drop (never write) the resolve cache: tariq_workspace_id may have just changed
        return {
            "status": "applied",
            "account_id": account_id,
            "container_id": container_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "workspace_mode": workspace_mode,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "preexisting_findings_not_blocking": len(other),
            "next_step": "Call hermes_gtm_cloud_publish with a D-003 approval token bound to this container_id and workspace_id.",
        }
    except Exception as exc:  # noqa: BLE001
        err = classify_gtm_error(exc)
        if any(written.values()):
            err["already_written"] = written
        return err


# ---------------------------------------------------------------------------------------------------------
# T4 - create a version and publish it
# ---------------------------------------------------------------------------------------------------------
def create_and_publish_version(service, account_id: str, container_id: str, workspace_id: str,
                               version_name: str) -> Dict[str, Any]:
    """create_version -> validate the response -> publish. Never publishes a version with a compiler/merge error."""
    parent_ws = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    base = {"mode": "cloud_api_v2", "account_id": account_id, "container_id": container_id}
    try:
        # num_retries=0 on create_version/publish: a 5xx after the server acted is ambiguous; never double-create a version.
        version_res = _execute(service.accounts().containers().workspaces().create_version(
            path=parent_ws,
            body={"name": version_name, "notes": "Released via Hermes GTM cloud API v2 after human (D-003) approval."},
        ), num_retries=0) or {}
    except Exception as exc:  # noqa: BLE001
        return {**classify_gtm_error(exc), **base}

    sync = version_res.get("syncStatus") or {}
    if sync.get("mergeConflict") or sync.get("syncError"):
        return {**error_response("MERGE_CONFLICT", "operator",
                                 "Workspace is out of sync with the latest container version (merge conflict / sync error); nothing was published."),
                **base}
    if version_res.get("compilerError") or not version_res.get("containerVersion"):
        return {**error_response("COMPILER_ERROR", "operator",
                                 "GTM could not compile the workspace into a version (compilerError or no version returned); nothing was published."),
                **base}

    v_path = version_res["containerVersion"].get("path")
    if not v_path:
        return {**error_response("COMPILER_ERROR", "operator", "Created version has no path; nothing was published."), **base}

    try:
        live = _execute(service.accounts().containers().versions().publish(path=v_path), num_retries=0) or {}
    except Exception as exc:  # noqa: BLE001
        return {**classify_gtm_error(exc), **base, "version_created_not_published": v_path}

    if live.get("compilerError") or not live.get("containerVersion"):
        return {**error_response("COMPILER_ERROR", "operator", "Publish call did not return a live version."),
                **base, "version_created_not_published": v_path}
    cv = live["containerVersion"]
    return {
        "status": "success",
        **base,
        "live_version_id": cv.get("containerVersionId"),
        "version_name": cv.get("name"),
        "operator_url": f"https://tagmanager.google.com/#/versions/{v_path}",
    }
