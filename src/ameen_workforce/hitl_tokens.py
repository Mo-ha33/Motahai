"""
hitl_tokens.py — Signed, bound, short-lived, single-use approval tokens for Rule D-003.

Rule D-003: Hermes never publishes GTM versions; a human does (or explicitly approves it).
A bare string such as 'SUPERVISOR_APPROVED' is not an approval, because the LLM can type it.
An approval is therefore a token the LLM cannot mint:

    token = base64url(json{cid, wid, exp, nonce}) + "." + base64url(HMAC-SHA256(key, payload_b64))

* Signed:      HMAC-SHA256 with the secret in env HITL_SIGNING_KEY (held by the operator-facing
               service and the MCP server, never by an agent). Missing/short key => fail closed.
* Bound:       the token is valid for exactly one container id + one workspace id.
* Short-lived: `exp` (unix seconds), default TTL 30 minutes.
* Single-use:  `nonce` is recorded in a JSON file (env HITL_USED_NONCES_PATH) when it is consumed.

Issuance: POST /approvals/gtm-publish in service.py (operator bearer key only).
Verification: the verifier block below is duplicated INLINE in the deployed consultation.py
(a VPS snapshot that cannot import this package). tests/test_hitl_tokens.py asserts the two
copies stay byte-for-byte identical, so edit both together.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Optional, Tuple

HITL_SIGNING_KEY_ENV = "HITL_SIGNING_KEY"
HITL_USED_NONCES_PATH_ENV = "HITL_USED_NONCES_PATH"
DEFAULT_USED_NONCES_PATH = "/home/deploy/.hermes/hitl_used_nonces.json"
MIN_SIGNING_KEY_CHARS = 32
DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 86400

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --- BEGIN VERIFIER (must stay identical to the copy inlined in consultation.py) ---
_NONCE_LOCK = threading.Lock()


def _signing_key() -> Optional[bytes]:
    key = os.environ.get("HITL_SIGNING_KEY", "")
    if len(key) < 32:
        return None
    return key.encode("utf-8")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _consume_nonce(used_nonce_store, nonce: str, exp: int, now: float, record: bool = True) -> bool:
    """Atomically records `nonce` as used. Returns False if it was already used or the store is unusable.
    With record=False it only checks that the nonce is still unused (nothing is written)."""
    with _NONCE_LOCK:
        try:
            if os.path.exists(used_nonce_store):
                with open(used_nonce_store, "r", encoding="utf-8") as fh:
                    used = json.load(fh)
                if not isinstance(used, dict):
                    return False
            else:
                used = {}
            if nonce in used:
                return False
            if not record:
                return True
            used = {n: e for n, e in used.items() if isinstance(e, (int, float)) and e > now}
            used[nonce] = exp
            directory = os.path.dirname(os.path.abspath(used_nonce_store))
            os.makedirs(directory, exist_ok=True)
            tmp_path = f"{used_nonce_store}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(used, fh)
            os.replace(tmp_path, used_nonce_store)
            return True
        except (OSError, ValueError):
            return False


def verify_publish_token(token, container_id, workspace_id, used_nonce_store, now=None, consume=True):
    """Returns (ok, reason). With consume=True (default) an ok token's nonce is spent (single use);
    consume=False validates everything without spending it (dry check before a later consume)."""
    key = _signing_key()
    if key is None:
        return False, "signing_key_not_configured"
    if not isinstance(token, str) or not token.strip():
        return False, "token_missing"
    now = time.time() if now is None else now
    try:
        payload_b64, sig_b64 = token.strip().split(".")
        expected = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return False, "bad_signature"
        claims = json.loads(_b64url_decode(payload_b64))
        cid, wid, exp, nonce = claims["cid"], claims["wid"], claims["exp"], claims["nonce"]
        if not (isinstance(cid, str) and isinstance(wid, str) and isinstance(nonce, str)
                and isinstance(exp, int) and not isinstance(exp, bool) and nonce):
            return False, "token_malformed"
    except (ValueError, KeyError, TypeError):
        return False, "token_malformed"
    if now >= exp:
        return False, "token_expired"
    if cid != container_id:
        return False, "container_mismatch"
    if wid != workspace_id:
        return False, "workspace_mismatch"
    if not _consume_nonce(used_nonce_store, nonce, exp, now, record=consume):
        return False, "nonce_reused_or_store_unavailable"
    return True, "ok"
# --- END VERIFIER ---


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_publish_token(container_id: str, workspace_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """
    Mints an approval token for exactly one (container_id, workspace_id) pair.
    Must only be called on behalf of an authenticated human operator (see service.py).
    Raises ValueError for bad input and RuntimeError if HITL_SIGNING_KEY is missing/too short.
    """
    key = _signing_key()
    if key is None:
        raise RuntimeError(f"{HITL_SIGNING_KEY_ENV} is not configured (min {MIN_SIGNING_KEY_CHARS} chars); refusing to issue")
    if not isinstance(container_id, str) or not _ID_PATTERN.match(container_id):
        raise ValueError("container_id must match [A-Za-z0-9_-]{1,64}")
    if not isinstance(workspace_id, str) or not _ID_PATTERN.match(workspace_id):
        raise ValueError("workspace_id must match [A-Za-z0-9_-]{1,64}")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be an integer between 1 and {MAX_TTL_SECONDS}")

    claims = {
        "cid": container_id,
        "wid": workspace_id,
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_urlsafe(16),
    }
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(signature)}"


def used_nonces_path() -> str:
    """Where consumed nonces are recorded (env HITL_USED_NONCES_PATH, VPS default otherwise)."""
    return os.environ.get(HITL_USED_NONCES_PATH_ENV) or DEFAULT_USED_NONCES_PATH
