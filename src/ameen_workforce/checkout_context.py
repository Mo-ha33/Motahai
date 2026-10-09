"""
checkout_context.py — Encrypted checkout IP / user agent, kept only until the conversion is sent (D-006, S2-3).

* Capture: the first orders/* webhook that carries client_details wins (capture_checkout_context). Values were already
  validated by the parsers (ipaddress check, UA control chars removed and capped at 512).
* At rest: Fernet ciphertext only (credentials.encrypt_value, same MOTAHAI_FERNET_KEY machinery, fail closed). The raw
  IP/UA never appear in a column, a log line or a result. A missing/invalid key never breaks order processing: the
  context is simply not stored (the exception TYPE is logged) and the conversion is sent without IP/UA.
* Use: load_checkout_context decrypts at send time. Expired rows are ignored.
* Purge: after a SUCCESSFUL live send (purge_checkout_context), and by purge_expired_checkout_context once
  expires_at (captured_at + 14 days) has passed. Shadow sends never dispatch, so they do not purge.
* Only checkout-captured values are ever sent: the webhook server's own connection IP is never used.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .credentials import CredentialError, decrypt_value, encrypt_value
from .db import CheckoutContext, utcnow

logger = logging.getLogger("ameen_workforce.checkout_context")

CHECKOUT_CONTEXT_TTL = timedelta(days=14)


def capture_checkout_context(session: Session, order_id: int, client_ip: Optional[str],
                             user_agent: Optional[str], now: Optional[datetime] = None) -> bool:
    """
    Stores the encrypted IP/UA for `order_id` unless a row already exists (first capture wins) or there is nothing to
    store. Does NOT commit (the caller commits with the order upsert). Returns True if a row was added.
    Never raises for key problems: a missing/invalid Fernet key is logged by type and the context is skipped.
    """
    if not client_ip and not user_agent:
        return False
    if session.scalar(select(CheckoutContext.id).where(CheckoutContext.order_id == order_id)) is not None:
        return False
    now = now or utcnow()
    try:
        row = CheckoutContext(
            order_id=order_id,
            ip_ciphertext=encrypt_value(client_ip) if client_ip else None,
            ua_ciphertext=encrypt_value(user_agent) if user_agent else None,
            captured_at=now,
            expires_at=now + CHECKOUT_CONTEXT_TTL,
        )
    except CredentialError as exc:
        logger.warning("Checkout context not stored for order %s: %s", order_id, type(exc).__name__)
        return False
    session.add(row)
    return True


def load_checkout_context(session: Session, order_id: int,
                          now: Optional[datetime] = None) -> Tuple[Optional[str], Optional[str]]:
    """(client_ip, user_agent) decrypted, or (None, None) when absent, expired or undecryptable (type logged only)."""
    row = session.scalar(select(CheckoutContext).where(CheckoutContext.order_id == order_id))
    if row is None or row.expires_at <= (now or utcnow()):
        return None, None
    try:
        ip = decrypt_value(row.ip_ciphertext) if row.ip_ciphertext else None
        ua = decrypt_value(row.ua_ciphertext) if row.ua_ciphertext else None
    except CredentialError as exc:
        logger.warning("Checkout context unreadable for order %s: %s", order_id, type(exc).__name__)
        return None, None
    return ip, ua


def purge_checkout_context(session: Session, order_id: int) -> int:
    """Deletes the order's context row (no commit). Returns rows deleted."""
    return session.execute(delete(CheckoutContext).where(CheckoutContext.order_id == order_id)).rowcount or 0


def purge_expired_checkout_context(session: Session, now: Optional[datetime] = None) -> int:
    """Deletes every context row past expires_at and commits. Returns rows deleted. The S1-5 scheduler calls this."""
    deleted = session.execute(
        delete(CheckoutContext).where(CheckoutContext.expires_at <= (now or utcnow()))).rowcount or 0
    session.commit()
    return deleted
