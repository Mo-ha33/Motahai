"""
credentials.py — Fernet-encrypted per-tenant secret storage (Meta CAPI token, Shopify access token, webhook secret).

* Key: env MOTAHAI_FERNET_KEY (a Fernet key; generate with generate_key()). Missing/invalid => fail closed
  (CredentialConfigError). There is no plaintext fallback.
* encrypt_value / decrypt_value expose the same Fernet machinery for other encrypted-at-rest data
  (e.g. checkout_context IP/UA, D-006).
* Plaintext is returned only by get_credential(); it is never logged, never put in exception messages, and
  never appears in a model repr. No HTTP route may return it.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Credential, utcnow

logger = logging.getLogger("ameen_workforce.credentials")

FERNET_KEY_ENV = "MOTAHAI_FERNET_KEY"


class CredentialError(RuntimeError):
    """Credential could not be stored or read. Messages never contain secret material."""


class CredentialConfigError(CredentialError):
    """MOTAHAI_FERNET_KEY is missing or invalid."""


def generate_key() -> str:
    """A new Fernet key, for provisioning MOTAHAI_FERNET_KEY."""
    return Fernet.generate_key().decode("ascii")


def _fernet() -> Fernet:
    key = os.environ.get(FERNET_KEY_ENV, "")
    if not key:
        raise CredentialConfigError(f"{FERNET_KEY_ENV} is not set; refusing to handle credentials")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        raise CredentialConfigError(f"{FERNET_KEY_ENV} is not a valid Fernet key") from None


def encrypt_value(plaintext: str) -> str:
    """Fernet-encrypts `plaintext` with MOTAHAI_FERNET_KEY. Fails closed (CredentialConfigError) without a valid key."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_value(ciphertext: str) -> str:
    """Inverse of encrypt_value. Raises CredentialError (no secret material in the message) if it cannot be decrypted."""
    fernet = _fernet()
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise CredentialError("value cannot be decrypted (wrong key?)") from None


def store_credential(
    session: Session,
    tenant_id: int,
    kind: str,
    plaintext: str,
    expires_at: Optional[datetime] = None
) -> int:
    """Encrypts and upserts the (tenant, kind) credential and commits. Returns the credential id (never the secret)."""
    if not plaintext:
        raise CredentialError("empty credential")
    ciphertext = encrypt_value(plaintext)
    row = session.scalar(select(Credential).where(Credential.tenant_id == tenant_id, Credential.kind == kind))
    if row is None:
        row = Credential(tenant_id=tenant_id, kind=kind, ciphertext=ciphertext, expires_at=expires_at)
        session.add(row)
    else:
        row.ciphertext = ciphertext
        row.expires_at = expires_at
        row.rotated_at = utcnow()
    session.commit()
    return row.id


def get_credential(session: Session, tenant_id: int, kind: str) -> Optional[str]:
    """Decrypted credential, or None if absent or expired. Raises CredentialError if it cannot be decrypted."""
    fernet = _fernet()  # fail closed on a missing key even when the row is absent
    row = session.scalar(select(Credential).where(Credential.tenant_id == tenant_id, Credential.kind == kind))
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at <= utcnow():
        logger.warning("Credential %s for tenant %s is expired", kind, tenant_id)
        return None
    try:
        return fernet.decrypt(row.ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise CredentialError(f"credential {kind} for tenant {tenant_id} cannot be decrypted (wrong key?)") from None
