"""
webhook_signatures.py — Platform webhook signature verification (S1-2).

Both verifiers take the RAW request bytes (never re-serialized JSON: a different key order or whitespace changes
the digest), compare in constant time, and FAIL CLOSED: a missing/empty secret or header returns False.
Stdlib only. Nothing here logs; callers must not log secrets or bodies either.
"""

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any, Optional


SHOPIFY_HMAC_HEADER = "X-Shopify-Hmac-Sha256"
SALLA_SIGNATURE_HEADER = "X-Salla-Signature"
OTO_SIGNATURE_HEADER = "X-OTO-Signature"
BOSTA_AUTH_HEADER = "Authorization"
# Bosta authenticates with a static shared secret (no body signature), so the secret itself must be unguessable.
# scripts/onboard_store.py --generate-courier-secrets produces 43 characters (secrets.token_urlsafe(32)).
BOSTA_MIN_SECRET_LENGTH = 24


def verify_shopify_hmac(raw_body: bytes, header_value: Optional[str], secret: str) -> bool:
    """
    Shopify: header X-Shopify-Hmac-Sha256 = base64(HMAC-SHA256(app client secret, raw body)). CONFIRMED in
    Shopify's docs (https://shopify.dev/docs/apps/build/webhooks/subscribe/https).
    """
    if not secret or not header_value or not isinstance(raw_body, (bytes, bytearray)):
        return False
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha256).digest())
    return hmac.compare_digest(expected, header_value.strip().encode("utf-8"))


def verify_salla_signature(raw_body: bytes, header_value: Optional[str], secret: str) -> bool:
    """
    Salla: header X-Salla-Signature = hex(HMAC-SHA256(webhook secret, raw body)).

    INFERRED, NOT VERIFIED: Salla's docs (https://docs.salla.dev/421119m0) are ambiguous in prose (they quote
    conflicting digest lengths) and the algorithm here comes from their Node/PHP code samples. This MUST be
    verified against a real Salla delivery (secret + body + header from the Partners Portal) before production.
    A leading "sha256=" is tolerated in case Salla (or a proxy) prefixes the digest; hex case is ignored.
    The `token` and `none` security strategies are deliberately unsupported (they fail closed here).
    """
    if not secret or not header_value or not isinstance(raw_body, (bytes, bytearray)):
        return False
    presented = header_value.strip().lower()
    if presented.startswith("sha256="):
        presented = presented[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), presented.encode("utf-8"))


def verify_oto_signature(order_id: Any, status: Any, timestamp: Any, signature: Optional[str], secret: str) -> bool:
    """
    OTO orderStatus webhook signature:
    HMAC-SHA256 over '{orderId}:{status}:{timestamp}' using secretKey, Base64 encoded.
    CONFIRMED in OTO webhook documentation.
    """
    if not secret or not signature or order_id is None or status is None or timestamp is None:
        return False
    msg = f"{order_id}:{status}:{timestamp}".encode("utf-8")
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def oto_timestamp_is_fresh(
    timestamp: Any, max_age_seconds: int, max_future_seconds: int, now: Optional[float] = None
) -> bool:
    """
    Replay window for the OTO signed `timestamp`: True only when it parses and lies within
    [now - max_age_seconds, now + max_future_seconds]. The signature covers the timestamp, so an attacker cannot
    refresh a captured request without the secret.

    Accepted formats (OTO's exact format is not pinned in their docs, so all common ones are tolerated): Unix seconds
    or milliseconds (int or digit string; values above 1e12 are read as milliseconds) and ISO 8601 (a trailing "Z"
    is UTC; a naive value is read as UTC). Anything else is NOT fresh (fails closed).
    """
    now = time.time() if now is None else now
    if isinstance(timestamp, bool) or timestamp is None:
        return False
    epoch: Optional[float] = None
    if isinstance(timestamp, (int, float)):
        epoch = float(timestamp)
    elif isinstance(timestamp, str):
        text = timestamp.strip()
        if not text:
            return False
        if text.isdigit():
            epoch = float(text)
        else:
            try:
                parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
            except ValueError:
                return False
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            epoch = parsed.timestamp()
    if epoch is None:
        return False
    if epoch > 1e12:
        epoch /= 1000.0
    return now - max_age_seconds <= epoch <= now + max_future_seconds


def verify_bosta_auth(auth_header: Optional[str], secret: str) -> bool:
    """
    Bosta webhook authentication: Bosta sends the merchant-configured secret back in a header (raw value, or with a
    "Bearer "/"Basic " prefix). There is no body signature, so:
      * a secret shorter than BOSTA_MIN_SECRET_LENGTH is treated as not configured (fails closed);
      * both sides are SHA-256 hashed before the constant-time compare, so timing reveals neither content nor length.
    Exact redeliveries are absorbed by the payload-hash dedupe in order_pipeline, and the order state machine never
    regresses a delivered order, which bounds what a replayed request can do.
    """
    secret = (secret or "").strip()
    if len(secret) < BOSTA_MIN_SECRET_LENGTH or not auth_header:
        return False
    val = auth_header.strip()
    for prefix in ("Bearer ", "Basic "):
        if val.startswith(prefix):
            val = val[len(prefix):].strip()
            break
    if not val:
        return False
    presented = hashlib.sha256(val.encode("utf-8")).digest()
    expected = hashlib.sha256(secret.encode("utf-8")).digest()
    return hmac.compare_digest(presented, expected)
