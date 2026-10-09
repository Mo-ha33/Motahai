"""
order_pipeline.py — Persistent, idempotent webhook -> Rule D-005 -> Meta CAPI pipeline (S1-1).

process_webhook() flow:
  1. Record a webhook_deliveries row. An identical redelivery (same delivery id, or same platform+topic+payload
     hash) that was already processed OK returns DUPLICATE_DELIVERY and is not reprocessed.
  2. Parse with the existing parsers, upsert the order (HASHES ONLY: no raw email/phone is stored) and append an
     order_status_events row when the status changed. fulfillments/update joins to the stored order for
     value/currency/customer hashes; an unknown order returns NEEDS_ORDER_CONTEXT (the S1-5 sweep backfills).
  3. Ask Rule D-005 for a decision. READY_TO_EMIT claims the idempotency key by INSERTing the capi_events row
     (UNIQUE tenant+order+event_name); a constraint violation means ALREADY_EMITTED and nothing is sent.
     Shadow tenants stop at status `shadow`; live tenants fetch the encrypted token and call Meta.
  4. Mark the delivery processed_ok / error_type.

Transactions: this module COMMITS on the session it is given (delivery row, order upsert, idempotency claim and
send result are separate commits so a failed claim never rolls back the order). Pass a dedicated session.

STALE decision (event_time older than 7 days): a capi_events row with status `stale` is written. It is terminal
and auditable ("N conversions were too old for Meta"), and it holds the idempotency key so it is never retried.
A failed send is retryable via retry_failed_events().
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .capi_service import DELIVERED_EVENT_NAME, capi_sender, hash_email, hash_phone
from .credentials import CredentialError, get_credential
from .db import CapiEvent, Order, OrderStatusEvent, Tenant, WebhookDelivery, utcnow
from .webhook_listener import OrderWebhookProcessor, fulfillment_context_decision

logger = logging.getLogger("ameen_workforce.pipeline")

META_CAPI_TOKEN_KIND = "meta_capi_token"
DEFAULT_MAX_ATTEMPTS = 5
# A `pending` claim older than this is an orphan (process died between claim and send); the retry sweep picks it up.
PENDING_ORPHAN_SECONDS = 600

_REVENUE_STATUSES = frozenset({"delivered", "paid"})
# Late/out-of-order webhooks must not walk a delivered/paid order back to an earlier state.
_REGRESSIVE_STATUSES = frozenset({"pending", "shipped", "paid_unfulfilled"})


def payload_sha256(payload: Dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the payload (identical redeliveries hash equal). The payload is not stored."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse(platform: str, topic: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if platform == "shopify" and topic.startswith("fulfillments/"):
        return OrderWebhookProcessor.parse_shopify_fulfillment(payload)
    if platform == "shopify":
        return OrderWebhookProcessor.parse_shopify_order(payload)
    if platform == "salla":
        return OrderWebhookProcessor.parse_salla_order(payload)
    return None


def _parse_platform_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _platform_created_at(platform: str, payload: Dict[str, Any]) -> Optional[datetime]:
    data = (payload.get("data") or payload) if platform == "salla" else payload
    return _parse_platform_time(data.get("created_at"))


def _next_status(current: Optional[str], new: str, from_fulfillment: bool) -> str:
    if current is None:
        return new
    if current in _REVENUE_STATUSES and new in _REGRESSIVE_STATUSES:
        return current
    if from_fulfillment and new == "shipped":
        return new if current == "pending" else current
    return new


def _result(action: str, order_id: str = "", capi_status: Optional[str] = None,
            decision: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "processed", "action": action, "order_id": order_id, "capi_status": capi_status}
    if decision is not None:
        out["d005_decision"] = {k: v for k, v in decision.items() if k != "payload"}  # payload carries hashes
    return out


def _finish(session: Session, delivery: WebhookDelivery, ok: bool, error_type: Optional[str] = None) -> None:
    delivery.processed_ok = ok
    delivery.error_type = error_type
    session.commit()


_ATTRIBUTION_COLUMNS = ("utm_source", "utm_medium", "utm_campaign", "utm_content",
                        "ad_id", "fbp", "fbc", "ttclid", "sccid")


def _merge_attribution(order: Order, attribution: Optional[Dict[str, Optional[str]]]) -> None:
    """
    Fills empty attribution columns from an (already sanitized) parsed["attribution"]. First non-empty value per
    field wins: a later webhook never overwrites a stored value, and an empty/missing value never erases one.
    (Deliberately simple: each field is kept independently, so fields may come from different webhooks.)
    """
    for column in _ATTRIBUTION_COLUMNS:
        value = (attribution or {}).get(column)
        if value and not getattr(order, column):
            setattr(order, column, value)


def _decide(sender, order: Order, event_time: Optional[int], client_ip: Optional[str] = None,
            user_agent: Optional[str] = None) -> Dict[str, Any]:
    """
    D-005 decision for a stored order, using stored hashes and stored fbp/fbc (raw PII is not available and not
    needed). client_ip / user_agent are NOT stored: they are only passed when the send happens while handling a
    webhook that carries them (orders/* topics); retries and fulfillments/update sends go without them.
    """
    return sender.process_cod_order_event(
        order_id=order.platform_order_id,
        status=order.current_status,
        value=float(order.value),
        currency=order.currency,
        is_cod=order.is_cod,
        email_hash=order.email_hash,
        phone_hash=order.phone_hash,
        fbp=order.fbp,
        fbc=order.fbc,
        client_ip=client_ip,
        user_agent=user_agent,
        event_time=event_time
    )


async def _send(session: Session, tenant: Tenant, event: CapiEvent, payload: Dict[str, Any], sender) -> str:
    """Sends a claimed event according to the tenant mode and records the outcome. Returns the new status."""
    if tenant.mode != "live":
        event.status = "shadow"
        session.commit()
        return "shadow"

    if not tenant.meta_dataset_id:
        event.status, event.error_type = "failed", "missing_dataset_id"
        session.commit()
        logger.error("Tenant %s is live but has no meta_dataset_id", tenant.id)
        return "failed"
    try:
        token = get_credential(session, tenant.id, META_CAPI_TOKEN_KIND)
    except CredentialError as e:
        event.status, event.error_type = "failed", type(e).__name__
        session.commit()
        logger.error("Cannot read Meta token for tenant %s: %s", tenant.id, type(e).__name__)
        return "failed"
    if not token:
        event.status, event.error_type = "failed", "missing_credential"
        session.commit()
        logger.error("Tenant %s is live but has no usable %s", tenant.id, META_CAPI_TOKEN_KIND)
        return "failed"

    result = await sender.send_event(pixel_id=tenant.meta_dataset_id, access_token=token, payload=payload)
    event.attempts += 1
    if result.get("status") == "success":
        event.status, event.error_type, event.http_code = "sent", None, 200
        event.fbtrace_id = result.get("fbtrace_id")
        event.sent_at = utcnow()
    else:
        event.status = "failed"
        event.http_code = result.get("http_code")
        event.error_type = result.get("error") or "meta_api_error"  # exception type name, or Meta rejected it
    session.commit()
    return event.status


async def process_webhook(
    session: Session,
    tenant: Tenant,
    platform: str,
    topic: str,
    payload: Dict[str, Any],
    delivery_id: Optional[str] = None,
    sender=capi_sender,
    event_time: Optional[int] = None,
    signature_ok: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Ingests one webhook for `tenant`. platform: "shopify" | "salla"; topic e.g. "orders/updated",
    "fulfillments/update". delivery_id: platform delivery id (Shopify X-Shopify-Webhook-Id) if available.
    event_time (unix seconds): when the conversion happened; default now. Returns {"status", "action", ...}
    where action is one of DUPLICATE_DELIVERY, TENANT_INACTIVE, UNSUPPORTED, NEEDS_ORDER_CONTEXT, DEFERRED,
    SUPPRESSED, ALREADY_EMITTED, STALE, SHADOW, SENT, FAILED. Unexpected errors are recorded on the delivery
    (error_type, processed_ok False) and re-raised so the caller can answer 5xx and the platform retries.
    """
    digest = payload_sha256(payload)
    dedupe_key = f"{platform}:{delivery_id}" if delivery_id else f"{platform}:{topic}:{digest}"
    delivery = WebhookDelivery(
        tenant_id=tenant.id, platform=platform, topic=topic, delivery_id=delivery_id,
        dedupe_key=dedupe_key, payload_sha256=digest, signature_ok=signature_ok
    )
    already_done = session.scalar(
        select(WebhookDelivery.id).where(
            WebhookDelivery.tenant_id == tenant.id,
            WebhookDelivery.dedupe_key == dedupe_key,
            WebhookDelivery.processed_ok.is_(True),
            WebhookDelivery.is_duplicate.is_(False)
        ).limit(1)
    )
    if already_done is not None:
        delivery.is_duplicate, delivery.processed_ok = True, True
        session.add(delivery)
        session.commit()
        return _result("DUPLICATE_DELIVERY")
    session.add(delivery)
    session.commit()

    try:
        if not tenant.active:
            _finish(session, delivery, False, "tenant_inactive")  # not "done": a redelivery after reactivation must run
            return _result("TENANT_INACTIVE")
        parsed = _parse(platform, topic, payload)
        if parsed is None:
            _finish(session, delivery, False, "unsupported_platform")
            return _result("UNSUPPORTED")
        if not parsed["order_id"]:
            _finish(session, delivery, False, "missing_order_id")
            return _result("UNSUPPORTED")
        return await _process_parsed(session, tenant, delivery, parsed, platform, topic, payload, digest, sender, event_time)
    except Exception as e:
        session.rollback()
        logger.error("Webhook processing failed (%s/%s): %s", platform, topic, type(e).__name__)
        delivery.processed_ok, delivery.error_type = False, type(e).__name__
        session.commit()
        raise


async def _process_parsed(session, tenant, delivery, parsed, platform, topic, payload, digest, sender, event_time):
    from_fulfillment = bool(parsed.get("needs_order_context"))
    order = session.scalar(select(Order).where(
        Order.tenant_id == tenant.id, Order.platform_order_id == parsed["order_id"]))

    if from_fulfillment and order is None:
        # No stored order to join: nothing can be emitted. The S1-5 sweep backfills the order.
        decision = fulfillment_context_decision(parsed)
        # A delivered fulfillment without its order is NOT done: the same redelivery must reprocess once the order exists.
        needs_context = decision["action"] == "NEEDS_ORDER_CONTEXT"
        _finish(session, delivery, not needs_context, "needs_order_context" if needs_context else None)
        return _result(decision["action"], parsed["order_id"], None, decision)

    previous_status = order.current_status if order is not None else None
    new_status = _next_status(previous_status, parsed["status"], from_fulfillment)

    if order is None:
        order = Order(tenant_id=tenant.id, platform_order_id=parsed["order_id"], current_status=new_status,
                      currency=parsed["currency"])
        session.add(order)
    order.current_status = new_status
    if not from_fulfillment:
        order.is_cod = bool(parsed["is_cod"])
        order.value = parsed["total_price"]
        order.currency = parsed["currency"]
        order.created_at_platform = order.created_at_platform or _platform_created_at(platform, payload)
        # Platforms can redact customer fields on later updates: never overwrite a hash with nothing.
        order.email_hash = hash_email(parsed["email"]) or order.email_hash
        order.phone_hash = hash_phone(parsed["phone"], parsed["currency"], tenant.country) or order.phone_hash
        _merge_attribution(order, parsed.get("attribution"))
    session.flush()
    if new_status != previous_status:
        session.add(OrderStatusEvent(order_id=order.id, status=new_status, source="webhook", topic=topic,
                                     payload_sha256=digest))
    session.commit()

    decision = _decide(sender, order, event_time, parsed.get("client_ip"), parsed.get("user_agent"))
    action = decision["action"]
    if action not in ("READY_TO_EMIT", "STALE"):
        _finish(session, delivery, True)
        return _result(action, order.platform_order_id, None, decision)

    if action == "STALE":
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=DELIVERED_EVENT_NAME,
                          event_id=f"delivered_{order.platform_order_id}", event_time=event_time, status="stale",
                          error_type="event_time_out_of_window")
        claim_payload = None
    else:
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=decision["event_name"],
                          event_id=decision["event_id"],
                          event_time=decision["payload"]["data"][0]["event_time"], status="pending")
        claim_payload = decision["payload"]
    session.add(claim)
    try:
        session.commit()  # the UNIQUE(tenant, order, event_name) constraint is the idempotency claim
    except IntegrityError:
        session.rollback()
        _finish(session, delivery, True)
        return _result("ALREADY_EMITTED", order.platform_order_id, None, decision)

    if action == "STALE":
        _finish(session, delivery, True)
        return _result("STALE", order.platform_order_id, "stale", decision)

    capi_status = await _send(session, tenant, claim, claim_payload, sender)
    _finish(session, delivery, True)
    return _result({"sent": "SENT", "shadow": "SHADOW"}.get(capi_status, "FAILED"),
                   order.platform_order_id, capi_status, decision)


async def retry_failed_events(
    session: Session,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sender=capi_sender
) -> Dict[str, int]:
    """
    Resends capi_events with status `failed` and attempts < max_attempts (plus orphaned `pending` claims older
    than PENDING_ORPHAN_SECONDS), for live tenants only. The payload is rebuilt from the stored order and the
    original event_time, so a retry past Meta's 7-day window becomes `stale`, and an order that has since been
    cancelled/refunded is not sent (`stale`, error_type no_longer_eligible). Rows failing for a missing
    credential/dataset id do not consume attempts, so they resume once the tenant is fixed.
    Returns counts: retried, sent, failed, stale, skipped.
    """
    orphan_cutoff = utcnow() - timedelta(seconds=PENDING_ORPHAN_SECONDS)
    candidates = session.scalars(
        select(CapiEvent).where(
            CapiEvent.attempts < max_attempts,
            or_(CapiEvent.status == "failed",
                and_(CapiEvent.status == "pending", CapiEvent.created_at <= orphan_cutoff))
        ).order_by(CapiEvent.id)
    ).all()
    counts = {"retried": 0, "sent": 0, "failed": 0, "stale": 0, "skipped": 0}
    for event in candidates:
        tenant = session.get(Tenant, event.tenant_id)
        order = session.get(Order, event.order_id)
        if tenant is None or order is None or tenant.mode != "live" or not tenant.active:
            counts["skipped"] += 1
            continue
        decision = _decide(sender, order, event.event_time)
        if decision["action"] != "READY_TO_EMIT":
            event.status = "stale"
            event.error_type = ("event_time_out_of_window" if decision["action"] == "STALE"
                                else "no_longer_eligible")
            session.commit()
            counts["stale"] += 1
            continue
        counts["retried"] += 1
        status = await _send(session, tenant, event, decision["payload"], sender)
        counts["sent" if status == "sent" else "failed"] += 1
    return counts
