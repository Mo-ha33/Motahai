"""
capture.py — Merges quarantined storefront captures (pending_captures) into verified orders (FX-2).

THREAT MODEL. The storefront capture endpoint (capture_routes.py) is public and unauthenticated: anyone can POST to
it. Order ids on Salla/Shopify are sequential and guessable, so an attacker could pre-register attribution (fbp, fbc,
user agent, utm_*) for the NEXT order ids; with "first non-empty value wins" that would poison the real customers'
Meta conversions. Therefore a capture NEVER touches the orders table when it arrives. It is parked in
`pending_captures` and only joined to an order here, after the signed platform webhook (the trusted source of truth)
has created that order, and only when:
  * the order belongs to the SAME tenant as the capture (cross-tenant ids never merge), and
  * the capture's order_total matches the order's stored total within max(1%, 1.0 currency unit) and the currency
    matches (the total is the SECOND FACTOR: knowing an order id is not enough, the sender must also know what the
    order cost, which the attacker cannot read for someone else's order).
Rejected rows keep a `rejected_reason` (total_mismatch | duplicate_capture) until they expire.

Merge rules: only EMPTY attribution columns on the order are filled (never overwritten, so webhook-sourced
note_attributes/Shopify values win); IP/UA go through checkout_context.capture_checkout_context (first capture wins:
an existing context, e.g. from Shopify client_details, is left alone). When several captures exist for one order the
earliest valid one is merged and the others are rejected as duplicate_capture (also when an earlier capture was already
merged). Ciphertexts are nulled once a row is merged or rejected; rows are deleted at expires_at (received + 48h).
A capture whose order has not arrived yet simply waits (the webhook can be seconds to hours late).

RESIDUAL RISK: an attacker who knows the exact total of one specific FUTURE order (e.g. a fixed-price single-product
store, or they place the order themselves) can still get a poisoned capture merged for it, and the first valid capture
wins. This narrows the attack from "any order id" to "an order whose total the attacker can predict"; the total is
not a secret, only a hard-to-guess second factor. Fixed single-price stores are the weak case.
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .checkout_context import capture_checkout_context
from .credentials import CredentialError, decrypt_value
from .db import Order, PendingCapture, utcnow

logger = logging.getLogger("ameen_workforce.capture")

ATTRIBUTION_COLUMNS = ("utm_source", "utm_medium", "utm_campaign", "utm_content",
                       "ad_id", "fbp", "fbc", "ttclid", "sccid")
TOTAL_TOLERANCE_RATIO = 0.01
TOTAL_TOLERANCE_MIN = 1.0

REJECT_TOTAL_MISMATCH = "total_mismatch"
REJECT_DUPLICATE = "duplicate_capture"


def _totals_match(capture: PendingCapture, order: Order) -> bool:
    """Currency equal and capture total within max(1%, 1.0) of the order's stored total (full total or net value)."""
    if (capture.currency or "").upper() != (order.currency or "").upper():
        return False
    candidates = [v for v in (order.order_total, order.value) if v is not None]
    for stored in candidates:
        tolerance = max(abs(stored) * TOTAL_TOLERANCE_RATIO, TOTAL_TOLERANCE_MIN)
        if abs(capture.order_total - stored) <= tolerance:
            return True
    return False


def _clear_sensitive(capture: PendingCapture) -> None:
    capture.ip_ciphertext = None
    capture.ua_ciphertext = None


def _merge_into_order(session: Session, capture: PendingCapture, order: Order, now: datetime) -> None:
    for column in ATTRIBUTION_COLUMNS:
        value = getattr(capture, column)
        if value and not getattr(order, column):
            setattr(order, column, value)
    ip = ua = None
    try:
        ip = decrypt_value(capture.ip_ciphertext) if capture.ip_ciphertext else None
        ua = decrypt_value(capture.ua_ciphertext) if capture.ua_ciphertext else None
    except CredentialError as exc:
        logger.warning("Pending capture %s IP/UA unreadable: %s", capture.id, type(exc).__name__)
    if ip or ua:
        capture_checkout_context(session, order.id, ip, ua, now=now)  # first capture wins; no-op if one exists
    capture.merged_at = now
    _clear_sensitive(capture)


def merge_pending_captures(session: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """
    Joins pending captures to their orders (see module docstring) and commits. Returns
    {"merged", "rejected", "expired_deleted", "waiting"}. Safe to run repeatedly (the scheduler runs it every 15 min).
    """
    now = now or utcnow()
    result = {"merged": 0, "rejected": 0, "expired_deleted": 0, "waiting": 0}

    result["expired_deleted"] = session.execute(
        delete(PendingCapture).where(PendingCapture.expires_at <= now)).rowcount or 0

    open_rows = session.scalars(
        select(PendingCapture)
        .where(PendingCapture.merged_at.is_(None), PendingCapture.rejected_reason.is_(None),
               PendingCapture.expires_at > now)
        .order_by(PendingCapture.received_at, PendingCapture.id)
    ).all()

    groups: Dict[Tuple[int, str], List[PendingCapture]] = defaultdict(list)
    for row in open_rows:
        groups[(row.tenant_id, row.platform_order_id)].append(row)

    for (tenant_id, platform_order_id), rows in groups.items():
        order = session.scalar(select(Order).where(
            Order.tenant_id == tenant_id, Order.platform_order_id == platform_order_id))
        if order is None:
            result["waiting"] += len(rows)
            continue

        already_merged = session.scalar(select(PendingCapture.id).where(
            PendingCapture.tenant_id == tenant_id, PendingCapture.platform_order_id == platform_order_id,
            PendingCapture.merged_at.is_not(None)).limit(1)) is not None

        winner_taken = already_merged
        for capture in rows:  # earliest first
            if not _totals_match(capture, order):
                capture.rejected_reason = REJECT_TOTAL_MISMATCH
                _clear_sensitive(capture)
                result["rejected"] += 1
            elif winner_taken:
                capture.rejected_reason = REJECT_DUPLICATE
                _clear_sensitive(capture)
                result["rejected"] += 1
            else:
                _merge_into_order(session, capture, order, now)
                winner_taken = True
                result["merged"] += 1

    session.commit()
    return result
