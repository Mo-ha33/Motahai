"""
motahai_api.py - Hermes MCP client for the Motahai Core tenant & COD signal API.

Pure functions with an injectable HTTP requester so unit tests never touch the network.
Deployed alongside consultation.py on the VPS.

Configuration (both required; read from the Hermes environment):
  * MOTAHAI_CORE_URL       - base URL of Motahai Core (http or https, with a host).
  * MOTAHAI_TENANT_API_KEY - Core bearer key.

MOTAHAI_CORE_URL has deliberately NO default. The VPS is shared: port 8000 on
127.0.0.1 belongs to another product (SmartCafe), so a silent fallback there would
send Motahai traffic to the wrong backend. A missing or blank URL or key returns
CORE_NOT_CONFIGURED without making any call. A loopback URL on port 8000 is refused
for the same reason.

Tools backed by this module:
  * hermes_motahai_tenant_status(tenant_key)
      -> GET  {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/status
  * hermes_motahai_signal_health(tenant_key, window_days=7)
      -> GET  {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/signal-health?window_days=N
  * hermes_motahai_promote_live(tenant_key, hitl_approval_token)
      -> POST {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/promote-live

KNOWN RISK (Pilot Acceptance):
The Core bearer key (MOTAHAI_TENANT_API_KEY) can read any tenant's aggregate counts.
For the pilot (stores we onboard ourselves) this is accepted; before opening W6 to
arbitrary self-serve stores, bind tenant_key to the Wesam conversation server-side.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("hermes.motahai_api")

TIMEOUT_S = 15.0
# Port 8000 on a loopback host is the SmartCafe backend on the shared VPS; never call it.
_RESERVED_LOOPBACK_PORT = 8000

# Lowercase domain (3..255 chars of a-z, 0-9, '.', '-') or numeric Salla merchant ID (1..20 digits).
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]{3,255}$")
_MERCHANT_ID_RE = re.compile(r"^[0-9]{1,20}$")


def error_response(
    reason_code: str,
    audience: str,
    detail: str,
    retryable: bool = False,
    **extra: Any,
) -> Dict[str, Any]:
    """Standard Hermes tool error envelope matching gtm_api.error_response."""
    out: Dict[str, Any] = {
        "ok": False,
        "status": "error",
        "reason_code": reason_code,
        "audience": audience,
        "retryable": retryable,
        "detail": detail,
    }
    out.update(extra)
    return out


def validate_tenant_key(tenant_key: Any) -> Optional[Dict[str, Any]]:
    """Validates tenant_key before making any HTTP call. Returns None if valid, else an error envelope."""
    if not isinstance(tenant_key, str) or not tenant_key:
        return error_response(
            "INVALID_TENANT_KEY",
            "operator",
            "tenant_key must be a non-empty lowercase domain or numeric merchant ID.",
        )
    if ".." in tenant_key or tenant_key.startswith(".") or tenant_key.endswith("."):
        return error_response(
            "INVALID_TENANT_KEY",
            "operator",
            "tenant_key contains invalid path or dot sequence.",
        )
    if tenant_key.isdigit():
        if _MERCHANT_ID_RE.fullmatch(tenant_key):
            return None
    elif _DOMAIN_RE.fullmatch(tenant_key):
        return None
    return error_response(
        "INVALID_TENANT_KEY",
        "operator",
        "tenant_key must match ^[a-z0-9.-]{3,255}$ (lowercase domain) or ^[0-9]{1,20}$ (merchant ID).",
    )


def validate_window_days(window_days: Any) -> Optional[Dict[str, Any]]:
    """Validates window_days is an integer in 1..28."""
    if isinstance(window_days, bool) or not isinstance(window_days, int) or not (1 <= window_days <= 28):
        return error_response(
            "INVALID_WINDOW_DAYS",
            "operator",
            "window_days must be an integer between 1 and 28.",
        )
    return None


def _redact_secrets(text: str, secrets: Tuple[str, ...]) -> str:
    """Strips any accidental occurrence of API keys or approval tokens from error text."""
    sanitized = text or ""
    for s in secrets:
        if s and len(s) >= 4 and s in sanitized:
            sanitized = sanitized.replace(s, "[REDACTED]")
    return sanitized


def _default_http_call(
    method: str,
    url: str,
    headers: Dict[str, str],
    body_bytes: Optional[bytes],
    timeout_s: float,
) -> Tuple[int, str]:
    """Executes an HTTP request using stdlib urllib and returns (status_code, response_text)."""
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - internal Core URL
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            return int(resp.status), text
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else ""
        return int(exc.code), text


HttpCaller = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, str]]


def _is_loopback_host(host: str) -> bool:
    """True for localhost, *.localhost, 127.0.0.0/8, ::1, and the unspecified addresses 0.0.0.0 / ::.

    The unspecified addresses are included because on Linux they reach the local host.
    Brackets are already stripped by urlsplit().hostname.
    """
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _validate_core_url(raw: str) -> Optional[str]:
    """Returns a safe detail message if raw is not an acceptable Core URL, else None.

    Never echoes the URL itself, so the detail is safe to return to callers.
    """
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
        port = parts.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return "MOTAHAI_CORE_URL is not a valid URL."
    if parts.scheme.lower() not in ("http", "https") or not host:
        return "MOTAHAI_CORE_URL must be an absolute http or https URL with a host."
    if port == _RESERVED_LOOPBACK_PORT and _is_loopback_host(host):
        return (
            "MOTAHAI_CORE_URL points at a loopback or unspecified address on port 8000, which is reserved "
            "for another product on this host (SmartCafe). Set it to the Motahai Core address."
        )
    return None


def _core_config() -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Reads MOTAHAI_CORE_URL and MOTAHAI_TENANT_API_KEY from env.

    Makes no network call. Returns CORE_NOT_CONFIGURED when either is missing or
    blank, or when the URL is malformed or targets the reserved port. The detail
    names variables only, never their values.
    """
    base_url = os.environ.get("MOTAHAI_CORE_URL", "").strip()
    api_key = os.environ.get("MOTAHAI_TENANT_API_KEY", "").strip()
    missing = [
        name
        for name, value in (("MOTAHAI_CORE_URL", base_url), ("MOTAHAI_TENANT_API_KEY", api_key))
        if not value
    ]
    if missing:
        return None, None, error_response(
            "CORE_NOT_CONFIGURED",
            "operator",
            f"{' and '.join(missing)} not configured in the Hermes environment.",
        )
    url_problem = _validate_core_url(base_url)
    if url_problem:
        return None, None, error_response("CORE_NOT_CONFIGURED", "operator", url_problem)
    return base_url.rstrip("/"), api_key, None


def _parse_json_safe(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _map_core_response(
    status_code: int,
    text: str,
    secrets_to_redact: Tuple[str, ...],
) -> Dict[str, Any]:
    """Maps Core HTTP status codes and JSON bodies to the Hermes tool contract."""
    data = _parse_json_safe(text)

    if status_code == 200:
        out: Dict[str, Any] = {"ok": True}
        out.update(data)
        return out

    if status_code == 401:
        return error_response(
            "CORE_AUTH_FAILED",
            "operator",
            "Core API rejected MOTAHAI_TENANT_API_KEY (HTTP 401).",
        )

    if status_code == 404:
        return error_response(
            "TENANT_NOT_FOUND",
            "operator",
            "Tenant not found in Motahai Core (HTTP 404).",
        )

    if status_code == 403:
        err_code = str(data.get("error", "")).strip()
        if err_code == "token_rejected" or not err_code:
            return error_response(
                "TOKEN_REJECTED",
                "operator",
                "Approval token was rejected by Core. Request a NEW single-use D-003 promote_live token from the operator.",
            )
        return error_response(
            "TOKEN_REJECTED",
            "operator",
            _redact_secrets(f"Forbidden by Core ({err_code}). Request a new D-003 token.", secrets_to_redact),
        )

    if status_code == 409:
        err_code = str(data.get("error", "")).strip()
        if err_code == "already_live":
            return error_response(
                "ALREADY_LIVE",
                "operator",
                "Tenant is already in live mode.",
            )
        if err_code == "precondition_failed":
            failed_list = data.get("failed") if isinstance(data.get("failed"), list) else []
            return error_response(
                "PRECONDITION_FAILED",
                "operator",
                "Tenant failed one or more promote-live preconditions.",
                failed=failed_list,
            )
        return error_response(
            "PRECONDITION_FAILED",
            "operator",
            _redact_secrets(f"Conflict from Core (HTTP 409: {err_code or 'conflict'}).", secrets_to_redact),
            failed=data.get("failed") if isinstance(data.get("failed"), list) else [],
        )

    if status_code >= 500 or status_code == 408 or status_code == 429:
        return error_response(
            "CORE_UNREACHABLE",
            "operator",
            f"Motahai Core returned HTTP {status_code}.",
            retryable=True,
        )

    return error_response(
        "CORE_ERROR",
        "operator",
        _redact_secrets(f"Unexpected HTTP {status_code} from Motahai Core.", secrets_to_redact),
    )


def tenant_status(
    tenant_key: str,
    http_call: Optional[HttpCaller] = None,
) -> Dict[str, Any]:
    """GET {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/status."""
    bad = validate_tenant_key(tenant_key)
    if bad:
        return bad
    base_url, api_key, cfg_err = _core_config()
    if cfg_err:
        return cfg_err

    caller = http_call or _default_http_call
    encoded_key = urllib.parse.quote(tenant_key, safe="")
    url = f"{base_url}/v1/tenants/{encoded_key}/status"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    secrets = (api_key,)
    try:
        status_code, body_text = caller("GET", url, headers, None, TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Core tenant_status network error: %s", type(exc).__name__)
        return error_response(
            "CORE_UNREACHABLE",
            "operator",
            _redact_secrets(f"Could not reach Motahai Core ({type(exc).__name__}).", secrets),
            retryable=True,
        )
    return _map_core_response(status_code, body_text, secrets)


def signal_health(
    tenant_key: str,
    window_days: int = 7,
    http_call: Optional[HttpCaller] = None,
) -> Dict[str, Any]:
    """GET {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/signal-health?window_days=N."""
    bad_key = validate_tenant_key(tenant_key)
    if bad_key:
        return bad_key
    bad_win = validate_window_days(window_days)
    if bad_win:
        return bad_win

    base_url, api_key, cfg_err = _core_config()
    if cfg_err:
        return cfg_err

    caller = http_call or _default_http_call
    encoded_key = urllib.parse.quote(tenant_key, safe="")
    url = f"{base_url}/v1/tenants/{encoded_key}/signal-health?window_days={window_days}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    secrets = (api_key,)
    try:
        status_code, body_text = caller("GET", url, headers, None, TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Core signal_health network error: %s", type(exc).__name__)
        return error_response(
            "CORE_UNREACHABLE",
            "operator",
            _redact_secrets(f"Could not reach Motahai Core ({type(exc).__name__}).", secrets),
            retryable=True,
        )
    return _map_core_response(status_code, body_text, secrets)


def promote_live(
    tenant_key: str,
    hitl_approval_token: str,
    http_call: Optional[HttpCaller] = None,
) -> Dict[str, Any]:
    """POST {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/promote-live with D-003 approval token."""
    bad_key = validate_tenant_key(tenant_key)
    if bad_key:
        return bad_key
    if not isinstance(hitl_approval_token, str) or not hitl_approval_token.strip():
        return error_response(
            "TOKEN_REJECTED",
            "operator",
            "Missing hitl_approval_token. Request a single-use D-003 promote_live token from the operator.",
        )

    base_url, api_key, cfg_err = _core_config()
    if cfg_err:
        return cfg_err

    token_clean = hitl_approval_token.strip()
    caller = http_call or _default_http_call
    encoded_key = urllib.parse.quote(tenant_key, safe="")
    url = f"{base_url}/v1/tenants/{encoded_key}/promote-live"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload_bytes = json.dumps({"approval_token": token_clean}).encode("utf-8")
    secrets = (api_key, token_clean)
    try:
        status_code, body_text = caller("POST", url, headers, payload_bytes, TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Core promote_live network error: %s", type(exc).__name__)
        return error_response(
            "CORE_UNREACHABLE",
            "operator",
            _redact_secrets(f"Could not reach Motahai Core ({type(exc).__name__}).", secrets),
            retryable=True,
        )
    return _map_core_response(status_code, body_text, secrets)
