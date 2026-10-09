"""
audiences.py — Meta audience exports for COD merchants (S2-6).

Why audiences: Meta cannot "subtract" a refused COD order from its optimization. The legitimate levers are
(1) exclude serial refusers from targeting and (2) seed lookalikes from customers who actually paid (delivered).

Rules:
- Every list is PER MERCHANT (tenant). Every query filters on tenant_id; no list ever mixes tenants.
- Only SHA-256 hex identifiers are written (the DB stores no raw email/phone). The CSV writer refuses anything
  that is not a 64-char lowercase hex digest, so a raw identifier can never reach a file.
- Refusal = COD order that was dispatched (status history has a shipped/paid_unfulfilled event) and whose current
  status is cancelled or refunded. A cancel before dispatch is NOT a refusal.
"""

import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .capi_service import DELIVERED_EVENT_NAME
from .db import CapiEvent, Order, OrderStatusEvent, Tenant, utcnow

# Order statuses that prove the order left the merchant (a refusal needs one of these in its history).
# paid_unfulfilled is deliberately NOT here: a held, not-dispatched order cancelled by the merchant is not a refusal.
SHIPPED_STATUSES = ("shipped",)
REFUSAL_STATUSES = ("cancelled", "refunded")
DELIVERED_STATUS = "delivered"
DELIVERED_EVENT_STATUSES = ("sent", "shadow")

MIN_LIST_ROWS = 100  # below this Meta may refuse to build the audience

# Meta customer-list CSV column names. verify against Meta customer list template before first upload.
CSV_PHONE_COLUMN = "phone"
CSV_EMAIL_COLUMN = "email"
CSV_VALUE_COLUMN = "value"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _as_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cutoff(since_days: Optional[int], now: datetime) -> Optional[datetime]:
    """None means unbounded (all time)."""
    if since_days is None:
        return None
    if since_days <= 0:
        raise ValueError("since_days must be positive")
    return now - timedelta(days=since_days)


# Time windows use the platform's order time, falling back to our ingest time when the platform time is missing.
ORDER_TIME = func.coalesce(Order.created_at_platform, Order.created_at)


def _refused_orders(session: Session, tenant_id: int, since_days: Optional[int], now: datetime) -> List[Any]:
    """Refused COD orders for ONE tenant, oldest first (so the last non-null email seen is the most recent)."""
    shipped_order_ids = select(OrderStatusEvent.order_id).where(OrderStatusEvent.status.in_(SHIPPED_STATUSES))
    stmt = select(Order.id, Order.phone_hash, Order.email_hash, ORDER_TIME.label("order_time")).where(
        Order.tenant_id == tenant_id,
        Order.is_cod.is_(True),
        Order.current_status.in_(REFUSAL_STATUSES),
        Order.id.in_(shipped_order_ids),
    )
    cutoff = _cutoff(since_days, now)
    if cutoff is not None:
        stmt = stmt.where(ORDER_TIME >= cutoff)
    return list(session.execute(stmt.order_by(ORDER_TIME, Order.id)).all())


def _refusal_identifiers(session: Session, tenant_id: int, since_days: Optional[int], now: datetime
                         ) -> Tuple[Set[str], Set[str]]:
    """Every phone/email hash that appears on ANY refused order of this tenant (threshold 1)."""
    phones: Set[str] = set()
    emails: Set[str] = set()
    for row in _refused_orders(session, tenant_id, since_days, now):
        if row.phone_hash:
            phones.add(row.phone_hash)
        if row.email_hash:
            emails.add(row.email_hash)
    return phones, emails


def refuser_hashes(session: Session, tenant_id: int, since_days: int = 180, min_refusals: int = 2,
                   now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Serial refusers for one tenant, keyed by phone_hash. Returns rows {phone_hash, email_hash, refusals}.
    email_hash is the most recent non-null email hash across that customer's refused orders (or None).
    """
    if min_refusals < 1:
        raise ValueError("min_refusals must be >= 1")
    now = _as_utc(now)
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in _refused_orders(session, tenant_id, since_days, now):
        if row.phone_hash is None:
            continue
        entry = grouped.setdefault(
            row.phone_hash, {"phone_hash": row.phone_hash, "email_hash": None, "refusals": 0}
        )
        entry["refusals"] += 1
        if row.email_hash is not None:
            entry["email_hash"] = row.email_hash  # ascending order: the last non-null value is the most recent
    return [entry for entry in grouped.values() if entry["refusals"] >= min_refusals]


def delivered_buyer_hashes(session: Session, tenant_id: int, since_days: int = 365,
                           now: Optional[datetime] = None, refusal_since_days: Optional[int] = 180
                           ) -> List[Dict[str, Any]]:
    """
    Customers of ONE tenant who paid (delivered), for a lookalike seed. Returns rows
    {phone_hash, email_hash, value, orders}. Group key is phone_hash, falling back to email_hash when the phone
    is missing. value is the sum of order values over the delivered orders (tenant currency).

    A delivered order is one whose current_status is 'delivered' OR that has a DeliveredPurchase CapiEvent with
    status sent/shadow. Any customer with at least one refusal (threshold 1, looked back `refusal_since_days`,
    None = all time) is excluded, matched by phone or email hash.
    """
    now = _as_utc(now)
    cutoff = _cutoff(since_days, now)
    emitted_order_ids = select(CapiEvent.order_id).where(
        CapiEvent.tenant_id == tenant_id,
        CapiEvent.event_name == DELIVERED_EVENT_NAME,
        CapiEvent.status.in_(DELIVERED_EVENT_STATUSES),
    )
    stmt = select(Order.id, Order.phone_hash, Order.email_hash, Order.value).where(
        Order.tenant_id == tenant_id,
        or_(Order.current_status == DELIVERED_STATUS, Order.id.in_(emitted_order_ids)),
    )
    if cutoff is not None:
        stmt = stmt.where(ORDER_TIME >= cutoff)
    orders = session.execute(stmt.order_by(ORDER_TIME, Order.id)).all()

    refused_phones, refused_emails = _refusal_identifiers(session, tenant_id, refusal_since_days, now)

    groups: Dict[str, Dict[str, Any]] = {}
    for row in orders:
        if row.phone_hash is None and row.email_hash is None:
            continue
        key = row.phone_hash or row.email_hash
        group = groups.setdefault(key, {
            "phone_hash": None, "email_hash": None, "value": 0.0, "orders": 0, "identifiers": set(),
        })
        group["orders"] += 1
        group["value"] += float(row.value or 0.0)
        if row.phone_hash:
            group["phone_hash"] = row.phone_hash
            group["identifiers"].add(row.phone_hash)
        if row.email_hash:
            group["email_hash"] = row.email_hash  # ascending order: most recent non-null wins
            group["identifiers"].add(row.email_hash)

    result: List[Dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        if group["identifiers"] & (refused_phones | refused_emails):
            continue  # any refusal disqualifies the customer from the seed
        result.append({
            "phone_hash": group["phone_hash"],
            "email_hash": group["email_hash"],
            "value": round(group["value"], 2),
            "orders": group["orders"],
        })
    return result


def _hash_or_empty(value: Optional[str]) -> str:
    if value is None:
        return ""
    if not _SHA256_HEX.match(value):
        raise ValueError("refusing to write a non-SHA-256 identifier to a customer list")
    return value


def write_customer_list_csv(rows: List[Dict[str, Any]], path: Path | str, include_value: bool = False) -> int:
    """
    Writes a Meta customer-list CSV of pre-hashed identifiers. Header: phone,email (+ value when include_value).
    Rows with neither identifier are skipped. Every row is validated before the file is opened, so a bad row
    never leaves a partial file behind. Returns the number of data rows written.
    """
    header = [CSV_PHONE_COLUMN, CSV_EMAIL_COLUMN] + ([CSV_VALUE_COLUMN] if include_value else [])
    lines: List[List[str]] = []
    for row in rows:
        phone = _hash_or_empty(row.get("phone_hash"))
        email = _hash_or_empty(row.get("email_hash"))
        if not phone and not email:
            continue
        line = [phone, email]
        if include_value:
            line.append(f"{float(row.get('value') or 0.0):.2f}")
        lines.append(line)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(lines)
    return len(lines)


def export_tenant_audiences(session: Session, tenant_id: int, out_dir: Path | str,
                            now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Writes <tenant_id>_exclude_refusers.csv and <tenant_id>_seed_delivered_buyers.csv into out_dir.
    Returns counts, file paths and a warnings list (one per list under MIN_LIST_ROWS).
    """
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"unknown tenant_id {tenant_id}")
    now = _as_utc(now)  # one reference time for both lists
    out_dir = Path(out_dir)

    refusers = refuser_hashes(session, tenant_id, since_days=180, min_refusals=2, now=now)
    buyers = delivered_buyer_hashes(session, tenant_id, since_days=365, now=now, refusal_since_days=180)

    exclude_path = out_dir / f"{tenant_id}_exclude_refusers.csv"
    seed_path = out_dir / f"{tenant_id}_seed_delivered_buyers.csv"
    exclude_count = write_customer_list_csv(refusers, exclude_path, include_value=False)
    seed_count = write_customer_list_csv(buyers, seed_path, include_value=True)

    warnings: List[str] = []
    for list_name, count in (("exclude_refusers", exclude_count), ("seed_delivered_buyers", seed_count)):
        if count < MIN_LIST_ROWS:
            warnings.append(
                f"tenant {tenant_id} {list_name}: {count} rows (< {MIN_LIST_ROWS}). "
                "Meta may not be able to use small audiences — verify minimum size."
            )

    return {
        "tenant_id": tenant_id,
        "exclude_refusers_count": exclude_count,
        "seed_delivered_buyers_count": seed_count,
        "exclude_refusers_path": str(exclude_path),
        "seed_delivered_buyers_path": str(seed_path),
        "warnings": warnings,
    }
