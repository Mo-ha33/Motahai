"""
test_salla_signature.py — The documented Salla signature test case (see tests/fixtures/salla/README.md).

verify_salla_signature implements hex(HMAC-SHA256(secret, raw body)), which is INFERRED from Salla's code samples.
  * test_salla_known_answer_vector pins that algorithm to a digest computed independently with OpenSSL, so a refactor
    cannot silently change it.
  * test_salla_real_captured_delivery is the production gate: it checks a delivery captured from a real Salla store
    and is skipped until one is supplied (the capture and the secret are never committed).
"""

import base64
import json
import os
from pathlib import Path

import pytest

from src.ameen_workforce.webhook_signatures import verify_salla_signature

KNOWN_SECRET = "salla-known-answer-secret"
KNOWN_BODY = b'{"event":"order.status.updated","merchant":1234567,"data":{"id":77}}'
# printf '%s' "$KNOWN_BODY" | openssl dgst -sha256 -hmac 'salla-known-answer-secret'
KNOWN_DIGEST = "01ff39c61f736ef8af000462fa884e34c3dc99464870ce38eb7f1b18beb3855b"

CAPTURE_ENV = "SALLA_CAPTURED_DELIVERY"  # path to a capture JSON (format in tests/fixtures/salla/README.md)
SECRET_ENV = "SALLA_WEBHOOK_SECRET"


def test_salla_known_answer_vector():
    assert verify_salla_signature(KNOWN_BODY, KNOWN_DIGEST, KNOWN_SECRET)
    assert verify_salla_signature(KNOWN_BODY, KNOWN_DIGEST.upper(), KNOWN_SECRET)  # hex case ignored
    assert verify_salla_signature(KNOWN_BODY, "sha256=" + KNOWN_DIGEST, KNOWN_SECRET)  # tolerated prefix
    assert not verify_salla_signature(KNOWN_BODY + b" ", KNOWN_DIGEST, KNOWN_SECRET)  # raw bytes, not re-serialized
    assert not verify_salla_signature(KNOWN_BODY, KNOWN_DIGEST, KNOWN_SECRET + "x")


def test_salla_real_captured_delivery():
    path = os.environ.get(CAPTURE_ENV, "")
    secret = os.environ.get(SECRET_ENV, "")
    if not path or not secret:
        pytest.skip(f"set {CAPTURE_ENV} and {SECRET_ENV} to verify a real Salla delivery (tests/fixtures/salla/README.md)")
    capture = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = base64.b64decode(capture["body_base64"])
    header = capture["headers"]["X-Salla-Signature"]
    assert verify_salla_signature(raw, header, secret), (
        "The real Salla signature does not match hex(HMAC-SHA256(secret, body)). Check the security strategy in the "
        "Salla Partners Portal and the header name before going live."
    )
    merchant = json.loads(raw).get("merchant")
    assert merchant not in (None, ""), "webhook_routes._salla_merchant_id expects a top-level `merchant`"
