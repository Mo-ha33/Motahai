"""
webhook_signatures.py — Platform webhook signature verification (S1-2).

Both verifiers take the RAW request bytes (never re-serialized JSON: a different key order or whitespace changes
the digest), compare in constant time, and FAIL CLOSED: a missing/empty secret or header returns False.
Stdlib only. Nothing here logs; callers must not log secrets or bodies either.
"""

import base64
import hashlib
import hmac
from typing import Any, Optional


SHOPIFY_HMAC_HEADER = "X-Shopify-Hmac-Sha256"
SALLA_SIGNATURE_HEADER = "X-Salla-Signature"
OTO_SIGNATURE_HEADER = "X-OTO-Signature"
BOSTA_AUTH_HEADER = "Authorization"


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


def verify_bosta_auth(auth_header: Optional[str], secret: str) -> bool:
    """
    Bosta webhook authentication:
    Matches Authorization header (raw API key or Bearer/Basic token) against configured secret in constant time.
    """
    if not secret or not auth_header:
        return False
    val = auth_header.strip()
    for prefix in ("Bearer ", "Basic "):
        if val.startswith(prefix):
            val = val[len(prefix):].strip()
            break
    return hmac.compare_digest(val.encode("utf-8"), secret.strip().encode("utf-8"))

