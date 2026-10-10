"""
webhook_staging.py — Durable staging and replay for verified webhooks (closes the lost-background-task gap).

The webhook routes answer 200 within the platforms' ~5 s budget and process in a FastAPI background task. Before this
module, a task lost between the 200 and its commit (process killed, deploy, OOM) was never redelivered by the
platform. Now every verified webhook is staged first:

  1. Route: stage_webhook() writes a staged_webhooks row (payload Fernet-encrypted) and commits BEFORE the 200.
     If staging fails (no MOTAHAI_FERNET_KEY, database down) the route answers 503 so the platform retries.
  2. Background task: process_staged() decrypts the row, runs order_pipeline.process_webhook and deletes the row.
     On an exception the row stays, with attempts + 1 and a backoff on next_attempt_at.
  3. Scheduler: replay_staged_webhooks() picks pending rows whose next_attempt_at has passed. A fresh row's first
     next_attempt_at is received_at + STAGE_GRACE, so a lost task is replayed on the next tick after that.

Replays are safe: process_webhook skips a delivery already processed (webhook_deliveries dedupe) and capi_events is
UNIQUE per (tenant, event_id), so at worst a replay re-derives a decision that is already recorded.

After MAX_ATTEMPTS failures the row becomes status "dead": its ciphertext is wiped (no PII kept) and an `incidents`
row (kind webhook_dead_letter) tells the operator. The dead row and the incident keep the non-sensitive metadata needed
to reconcile by hand: tenant, platform, topic, delivery id, platform order reference (order_ref), received time,
attempts and the last error type. Encrypted payloads therefore live at most until processed, or
about BACKOFF total (~10 h) for a webhook that keeps failing.

Privacy: payloads are only ever stored encrypted; logs carry ids, platform, topic and exception TYPE names only.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .capi_service import capi_sender
from .credentials import decrypt_value, encrypt_value
from .db import Incident, StagedWebhook, Tenant, utcnow
from .order_pipeline import process_webhook

logger = logging.getLogger("ameen_workforce.webhook_staging")

STAGE_GRACE = timedelta(minutes=5)  # how long a fresh row is left to its background task before the sweep may replay
REPLAY_LEASE = timedelta(minutes=10)  # a row being replayed is pushed out this far so a second sweep skips it
# Delay before attempt n+1 after the n-th failure; len(BACKOFF) + 1 == MAX_ATTEMPTS.
BACKOFF = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=8))
MAX_ATTEMPTS = len(BACKOFF) + 1
DEFAULT_REPLAY_LIMIT = 200
DEAD_LETTER_INCIDENT = "webhook_dead_letter"


def order_ref_from_payload(platform: str, payload: Any) -> Optional[str]:
    """
    Best-effort platform order reference for dead-letter reconciliation (never PII): Shopify order `id` (or
    `order_id` on fulfillment topics), Salla `data.id` / `data.reference_id`, Bosta `businessReference`, OTO
    `orderId`. Courier fields may sit under `data`/`delivery`. Returns None when nothing usable is present.
    """
    if not isinstance(payload, dict):
        return None
    candidates: list = []
    nested = [payload.get(k) for k in ("data", "delivery") if isinstance(payload.get(k), dict)]
    if platform == "shopify":
        candidates = [payload.get("order_id"), payload.get("id")]
    elif platform == "salla":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        candidates = [data.get("id"), data.get("reference_id")]
    elif platform == "bosta":
        for src in [payload] + nested:
            candidates += [src.get("businessReference"), src.get("trackingNumber")]
    elif platform == "oto":
        for src in [payload] + nested:
            candidates += [src.get("orderId"), src.get("order_id"), src.get("reference_id")]
    for value in candidates:
        if isinstance(value, bool) or value in (None, ""):
            continue
        if isinstance(value, (str, int)):
            return str(value)[:128]
    return None


def stage_webhook(
    session: Session, tenant_id: int, platform: str, topic: str, payload: Dict[str, Any],
    delivery_id: Optional[str], now: Optional[datetime] = None
) -> int:
    """Encrypts and commits the payload; returns the staged row id. Raises if the vault or the database fails."""
    now = now or utcnow()
    row = StagedWebhook(
        tenant_id=tenant_id, platform=platform, topic=(topic or "unknown")[:64],
        delivery_id=delivery_id[:128] if delivery_id else None, order_ref=order_ref_from_payload(platform, payload),
        payload_ciphertext=encrypt_value(json.dumps(payload, separators=(",", ":"))),
        received_at=now, next_attempt_at=now + STAGE_GRACE, attempts=0, status="pending"
    )
    session.add(row)
    session.commit()
    return row.id


def _record_failure(session: Session, row: StagedWebhook, error_type: str, now: datetime) -> None:
    row.attempts = (row.attempts or 0) + 1
    row.last_error_type = error_type[:64]
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "dead"
        row.payload_ciphertext = ""  # dead letters keep no PII
        session.add(Incident(tenant_id=row.tenant_id, kind=DEAD_LETTER_INCIDENT, severity="error",
                             detail=(f"{row.platform}/{row.topic} order_ref={row.order_ref or 'unknown'} "
                                     f"delivery_id={row.delivery_id or '-'} staged_id={row.id} "
                                     f"received_at={row.received_at.isoformat()}: {error_type} after "
                                     f"{row.attempts} attempts")))
        logger.error("Staged webhook %s dead after %s attempts (%s)", row.id, row.attempts, error_type)
    else:
        row.next_attempt_at = now + BACKOFF[row.attempts - 1]
    session.commit()


async def process_staged(
    session: Session, staged_id: int, sender=capi_sender, now: Optional[datetime] = None, processor=None
) -> Optional[Dict[str, Any]]:
    """
    Runs process_webhook (or `processor`, same signature) for one staged row and deletes the row on success. Returns
    its result, or None when the row is gone or dead (already handled). On failure records the attempt and re-raises.
    """
    processor = processor or process_webhook
    row = session.get(StagedWebhook, staged_id)
    if row is None or row.status != "pending":
        return None
    try:
        tenant = session.get(Tenant, row.tenant_id)
        if tenant is None:
            raise LookupError("tenant vanished")
        payload = json.loads(decrypt_value(row.payload_ciphertext))
        result = await processor(session, tenant, row.platform, row.topic, payload,
                                 delivery_id=row.delivery_id, signature_ok=True, sender=sender)
    except Exception as exc:
        session.rollback()
        row = session.get(StagedWebhook, staged_id)
        if row is not None and row.status == "pending":
            _record_failure(session, row, type(exc).__name__, now or utcnow())
        raise
    row = session.get(StagedWebhook, staged_id)
    if row is not None:
        session.delete(row)
        session.commit()
    return result


async def replay_staged_webhooks(
    session: Session, now: Optional[datetime] = None, sender=capi_sender, limit: int = DEFAULT_REPLAY_LIMIT
) -> Dict[str, int]:
    """Scheduler job: replays pending rows whose next_attempt_at has passed (lost tasks and failed attempts)."""
    now = now or utcnow()
    due_ids = session.scalars(
        select(StagedWebhook.id)
        .where(StagedWebhook.status == "pending", StagedWebhook.next_attempt_at <= now)
        .order_by(StagedWebhook.next_attempt_at)
        .limit(limit)
    ).all()
    counts = {"due": len(due_ids), "processed": 0, "failed": 0, "dead": 0}
    for staged_id in due_ids:
        row = session.get(StagedWebhook, staged_id)
        if row is None or row.status != "pending":
            continue
        row.next_attempt_at = now + REPLAY_LEASE
        session.commit()
        try:
            await process_staged(session, staged_id, sender=sender, now=now)
            counts["processed"] += 1
        except Exception as exc:
            counts["failed"] += 1
            logger.warning("Replay of staged webhook %s failed: %s", staged_id, type(exc).__name__)
            row = session.get(StagedWebhook, staged_id)
            if row is not None and row.status == "dead":
                counts["dead"] += 1
    return counts
