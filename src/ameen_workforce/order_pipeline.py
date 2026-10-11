"""
order_pipeline.py — Persistent, idempotent webhook -> Rule D-005 -> Meta CAPI pipeline (S1-1, S2-1/3/4).

process_webhook() flow:
  1. Record a webhook_deliveries row. An identical redelivery (same delivery id, or same platform+topic+payload
     hash) that was already processed OK returns DUPLICATE_DELIVERY and is not reprocessed.
  2. Parse with the existing parsers, upsert the order (HASHES ONLY: no raw email/phone/name/address is stored) and
     append an order_status_events row when the status changed. fulfillments/update joins to the stored order for
     value/currency/customer hashes; an unknown order returns NEEDS_ORDER_CONTEXT (the S1-5 sweep backfills).
     The first orders/* webhook carrying client_details also stores the checkout IP/UA ENCRYPTED (D-006).
  3. Ask Rule D-005 for a decision. READY_TO_EMIT does NOT send at once: it records `orders.delivered_at` (first time
     we saw delivered/paid) and claims the idempotency key by INSERTing the capi_events row with status `scheduled`
     and due_at = min(delivered_at + settlement, placed_at + LATE_DELIVERY_CUTOFF - 1h) (UNIQUE tenant+order+event_name;
     a violation means ALREADY_EMITTED). Settlement is tenants.settlement_hours, default 12h. If due_at has already
     passed the event is dispatched IN-LINE in the same call (so a 0h tenant behaves as "send immediately" and an
     order close to the cutoff is not delayed until the next timer tick); otherwise the S1-5 scheduler's call to
     send_due_events() sends it.
  4. send_due_events() sends `scheduled` rows once due. At send time it re-evaluates the CURRENT order (cancelled or
     fully refunded since -> `stale`/no_longer_eligible; past the cutoff -> `late_delivery`), rebuilds the payload with
     the CURRENT net value and the decrypted checkout IP/UA, then honors shadow/live and credentials like before.
  5. Mark the delivery processed_ok / error_type.

event_time is the order's PLACED time: orders.created_at_platform, else (documented fallback) the order row's
first-seen timestamp orders.created_at. Meta's 7-day upload limit is protected by LATE_DELIVERY_CUTOFF (6.5 days after
placement): later conversions are never sent (`late_delivery`, terminal, holds the idempotency key). The older
`stale` guard (explicit event_time older than 7 days) stays as a second line of defense.

Transactions: this module COMMITS on the session it is given (delivery row, order upsert, idempotency claim and
send result are separate commits so a failed claim never rolls back the order). Pass a dedicated session.

STALE: a capi_events row with status `stale` is written. It is terminal and auditable ("N conversions were too old for
Meta"), and it holds the idempotency key so it is never retried. A failed send is retryable via retry_failed_events().
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .capi_service import (
    CONFIRMED_EVENT, CONFIRMED_EVENT_NAME, DELIVERED_EVENT, DELIVERED_EVENT_NAME,
    EVENT_TYPES, LATE_DELIVERY_CUTOFF, MATCH_KEYS, NON_REVENUE_STATUSES, EventType, capi_sender, hash_email, hash_phone,
    quality_flags_for
)
from .checkout_context import (  # noqa: F401  (purge_expired_checkout_context is re-exported for the scheduler)
    capture_checkout_context, load_checkout_context, purge_checkout_context, purge_expired_checkout_context
)
from .confirmation import (
    SOURCE_IMPLICIT_SHIPPED, SOURCE_MANUAL, effective_confirmation_rules
)
from .credentials import CredentialError, get_credential
from .db import CapiEvent, Order, OrderStatusEvent, Tenant, WebhookDelivery, utcnow
from .webhook_listener import OrderWebhookProcessor, fulfillment_context_decision, match_hashes_from_parsed

logger = logging.getLogger("ameen_workforce.pipeline")

META_CAPI_TOKEN_KIND = "meta_capi_token"
DEFAULT_MAX_ATTEMPTS = 5
# A `pending` claim older than this is an orphan (process died between claim and send); the retry sweep picks it up.
PENDING_ORPHAN_SECONDS = 600
# S2-1: hours between an order becoming delivered/paid and the send (override per tenant: tenants.settlement_hours).
DEFAULT_SETTLEMENT_HOURS = 12.0
# due_at never goes past placed_at + LATE_DELIVERY_CUTOFF - this margin, so a due send still lands before the cutoff.
DUE_SAFETY_MARGIN = timedelta(hours=1)

_REVENUE_STATUSES = frozenset({"delivered", "paid"})
# Late/out-of-order webhooks must not walk a delivered/paid order back to an earlier state.
_REGRESSIVE_STATUSES = frozenset({"pending", "shipped", "paid_unfulfilled"})
# capi_events statuses after which checkout IP/UA must not be (re)captured for the order.
_FINAL_EVENT_STATUSES = ("sent", "shadow", "stale", "late_delivery")


def payload_sha256(payload: Dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the payload (identical redeliveries hash equal). The payload is not stored."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse(platform: str, topic: str, payload: Dict[str, Any],
           rules: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if platform == "shopify" and topic.startswith("fulfillments/"):
        return OrderWebhookProcessor.parse_shopify_fulfillment(payload)
    if platform == "shopify":
        return OrderWebhookProcessor.parse_shopify_order(payload, rules)
    if platform == "salla":
        return OrderWebhookProcessor.parse_salla_order(payload, rules)
    if platform in ("bosta", "courier_bosta"):
        return OrderWebhookProcessor.parse_bosta_delivery(payload)
    if platform in ("oto", "courier_oto"):
        return OrderWebhookProcessor.parse_oto_order_status(payload)
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
            decision: Optional[Dict[str, Any]] = None, due_at: Optional[datetime] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "processed", "action": action, "order_id": order_id, "capi_status": capi_status}
    if due_at is not None:
        out["due_at"] = due_at.isoformat()
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


# --- S2-1 timing -------------------------------------------------------------------------------------------

def order_placed_at(order: Order) -> datetime:
    """
    When the customer placed the order = the CAPI event_time. orders.created_at_platform (the platform's own
    timestamp); if the platform did not give a parseable one, the order row's first-seen timestamp (orders.created_at).
    """
    return order.created_at_platform or order.created_at


def settlement_hours_for(tenant: Tenant) -> float:
    hours = getattr(tenant, "settlement_hours", None)
    return DEFAULT_SETTLEMENT_HOURS if hours is None else float(hours)


def compute_due_at(delivered_at: datetime, placed_at: datetime, settlement_hours: float) -> datetime:
    """min(delivered_at + settlement, placed_at + LATE_DELIVERY_CUTOFF - 1h): never later than the safe send deadline."""
    return min(delivered_at + timedelta(hours=settlement_hours),
               placed_at + LATE_DELIVERY_CUTOFF - DUE_SAFETY_MARGIN)


def event_source_url_for(tenant: Tenant) -> Optional[str]:
    """tenants.storefront_url; else https://<shop_domain>/ for Shopify; for Salla only an explicit storefront_url."""
    if tenant.storefront_url:
        return tenant.storefront_url
    if tenant.platform == "shopify" and tenant.shop_domain:
        return f"https://{tenant.shop_domain}/"
    return None


def _decide(session: Session, sender, tenant: Tenant, order: Order, now: datetime,
            event_type: EventType = DELIVERED_EVENT, event_time: Optional[int] = None,
            with_context: bool = False, is_confirmed: bool = False) -> Dict[str, Any]:
    """
    D-005 decision for a stored order, using stored hashes and stored fbp/fbc (raw PII is not available and not
    needed). event_time defaults to the order's placed time for DeliveredPurchase, or confirmation time for ConfirmedOrder.
    with_context=True (send time only) decrypts the stored checkout IP/UA (D-006) into the payload; scheduling decisions
    never touch the ciphertext.
    """
    placed = int(order_placed_at(order).timestamp())
    ip, ua = load_checkout_context(session, order.id, now) if with_context else (None, None)

    if event_type == CONFIRMED_EVENT:
        order_val = float(getattr(order, "order_total", None) or order.value)
        if event_time is None:
            if getattr(order, "confirmed_at", None):
                event_time = int(order.confirmed_at.timestamp())
            else:
                event_time = int(now.timestamp())
        placed_cutoff = None
        is_conf = is_confirmed or bool(getattr(order, "confirmed_at", None))
    else:
        order_val = float(order.value)
        if event_time is None:
            event_time = placed
        placed_cutoff = placed
        is_conf = False

    return sender.process_cod_order_event(
        order_id=order.platform_order_id,
        status=order.current_status,
        value=order_val,
        currency=order.currency,
        is_cod=order.is_cod,
        email_hash=order.email_hash,
        phone_hash=order.phone_hash,
        fbp=order.fbp,
        fbc=order.fbc,
        client_ip=ip,
        user_agent=ua,
        match_hashes={key: getattr(order, f"{key}_hash") for key in MATCH_KEYS},
        event_source_url=event_source_url_for(tenant),
        test_event_code=getattr(tenant, "meta_test_event_code", None),
        event_time=event_time,
        placed_at=placed_cutoff,
        now=now.timestamp(),
        event_type=event_type,
        is_confirmed=is_conf
    )


async def _send(session: Session, tenant: Tenant, event: CapiEvent, payload: Dict[str, Any], sender) -> str:
    """
    Sends a claimed event according to the tenant mode and records the outcome. Returns the new status.
    A SUCCESSFUL live send purges the order's encrypted checkout IP/UA (D-006) on final delivery; shadow never dispatches, so it keeps it.
    Records capi_events.quality_flags (missing client_user_agent / event_source_url) for every send attempt.
    """
    event.quality_flags = quality_flags_for(payload)
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
        if event.event_name == DELIVERED_EVENT_NAME:
            purge_checkout_context(session, event.order_id)
    else:
        event.status = "failed"
        event.http_code = result.get("http_code")
        event.error_type = result.get("error") or "meta_api_error"  # exception type name, or Meta rejected it
    session.commit()
    return event.status


async def _resolve(session: Session, tenant: Tenant, event: CapiEvent, decision: Dict[str, Any], sender) -> str:
    """Applies a send-time decision to a claimed event: send it, or end it as late_delivery / stale. Returns the status."""
    action = decision["action"]
    if action == "READY_TO_EMIT":
        return await _send(session, tenant, event, decision["payload"], sender)
    if action == "LATE_DELIVERY":
        event.status, event.error_type = "late_delivery", "past_cutoff"
    elif action == "STALE":
        event.status, event.error_type = "stale", "event_time_out_of_window"
    else:  # SUPPRESSED / DEFERRED: cancelled, refunded or zero-value since the event was scheduled
        event.status, event.error_type = "stale", "no_longer_eligible"
    session.commit()
    return event.status


async def _dispatch_scheduled(session: Session, tenant: Tenant, event: CapiEvent, order: Order, sender,
                              now: datetime) -> Optional[str]:
    """
    Claims a `scheduled` row (atomic scheduled -> pending, so two sweeps never send the same event), re-evaluates the
    order as it is NOW and sends/ends it. Returns the resulting status, or None if another worker claimed the row.
    """
    claimed = session.execute(
        update(CapiEvent).where(CapiEvent.id == event.id, CapiEvent.status == "scheduled")
        .values(status="pending", claimed_at=now))
    session.commit()
    if claimed.rowcount != 1:
        return None
    session.refresh(event)
    decision = _decide(session, sender, tenant, order, now, EVENT_TYPES[event.event_name],
                       event_time=event.event_time, with_context=True)
    return await _resolve(session, tenant, event, decision, sender)


async def process_webhook(
    session: Session,
    tenant: Tenant,
    platform: str,
    topic: str,
    payload: Dict[str, Any],
    delivery_id: Optional[str] = None,
    sender=capi_sender,
    event_time: Optional[int] = None,
    signature_ok: Optional[bool] = None,
    now: Optional[datetime] = None,
    event_type: Optional[EventType] = None
) -> Dict[str, Any]:
    """
    Ingests one webhook for `tenant`. platform: "shopify" | "salla"; topic e.g. "orders/updated",
    "fulfillments/update". delivery_id: platform delivery id (Shopify X-Shopify-Webhook-Id) if available.
    event_time (unix seconds): explicit override of the conversion time (default: the order's placed time; mainly for
    tests). now: clock override (default utcnow) for delivered_at/due_at. Returns {"status", "action", ...}
    where action is one of DUPLICATE_DELIVERY, TENANT_INACTIVE, UNSUPPORTED, NEEDS_ORDER_CONTEXT, DEFERRED,
    SUPPRESSED, ALREADY_EMITTED, STALE, LATE_DELIVERY, SCHEDULED, SHADOW, SENT, FAILED. SCHEDULED means the conversion
    waits for its settlement window (result carries due_at); send_due_events() sends it. Unexpected errors are
    recorded on the delivery (error_type, processed_ok False) and re-raised so the caller can answer 5xx and the
    platform retries.
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
        parsed = _parse(platform, topic, payload, effective_confirmation_rules(tenant))
        if parsed is None:
            _finish(session, delivery, False, "unsupported_platform")
            return _result("UNSUPPORTED")
        if not parsed["order_id"]:
            _finish(session, delivery, False, "missing_order_id")
            return _result("UNSUPPORTED")
        return await _process_parsed(session, tenant, delivery, parsed, platform, topic, payload, digest, sender,
                                     event_time, now or utcnow(), event_type=event_type)
    except Exception as e:
        session.rollback()
        logger.error("Webhook processing failed (%s/%s): %s", platform, topic, type(e).__name__)
        delivery.processed_ok, delivery.error_type = False, type(e).__name__
        session.commit()
        raise


async def emit_confirmed_order(
    session: Session,
    tenant: Tenant,
    platform_order_id: str,
    sender=capi_sender,
    event_time: Optional[int] = None,
    now: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Direct trigger for ConfirmedOrder (Step 2 on the 3-step signal ladder).
    Fired when customer confirms via call, WhatsApp bot, or manual merchant action.
    Guarantees idempotency via CapiEvent UNIQUE(tenant, order, event_name).
    """
    now = now or utcnow()
    order = session.scalar(
        select(Order).where(Order.tenant_id == tenant.id, Order.platform_order_id == str(platform_order_id))
    )
    if order is None:
        return {"status": "error", "message": "order_not_found", "order_id": str(platform_order_id)}

    if order.confirmed_at is None and order.current_status not in NON_REVENUE_STATUSES:
        order.confirmed_at = now  # a cancelled/refunded/voided order is never marked confirmed
        order.confirmation_source = SOURCE_MANUAL
        session.commit()

    decision = _decide(session, sender, tenant, order, now, CONFIRMED_EVENT, event_time=event_time, is_confirmed=True)
    action = decision["action"]
    if action not in ("READY_TO_EMIT", "STALE"):
        return _result(action, order.platform_order_id, None, decision)

    event_id = CONFIRMED_EVENT.event_id(order.platform_order_id)
    if action == "STALE":
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=CONFIRMED_EVENT.name, event_id=event_id,
                          event_time=decision["payload"]["data"][0]["event_time"] if "payload" in decision else int(now.timestamp()),
                          status="stale", error_type="event_time_out_of_window")
    else:
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=CONFIRMED_EVENT.name,
                          event_id=decision["event_id"], event_time=decision["payload"]["data"][0]["event_time"],
                          status="scheduled", due_at=now)
    session.add(claim)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return _result("ALREADY_EMITTED", order.platform_order_id, None, decision)

    if action == "STALE":
        return _result(action, order.platform_order_id, claim.status, decision)

    capi_status = await _dispatch_scheduled(session, tenant, claim, order, sender, now)
    return _result(
        {"sent": "SENT", "shadow": "SHADOW", "late_delivery": "LATE_DELIVERY", "stale": "STALE"}.get(capi_status, "FAILED"),
        order.platform_order_id, capi_status, decision
    )


def _has_final_event(session: Session, order_id: int) -> bool:
    return session.scalar(select(CapiEvent.id).where(
        CapiEvent.order_id == order_id,
        CapiEvent.event_name == DELIVERED_EVENT_NAME,
        CapiEvent.status.in_(_FINAL_EVENT_STATUSES)).limit(1)) is not None


def _confirmation_source(parsed: Dict[str, Any], new_status: str, tenant: Tenant, from_fulfillment: bool) -> Optional[str]:
    """
    Why THIS webhook confirms the order, or None. Order webhooks carry their own verdict (parsed["confirmation_source"],
    from the tenant's rules). Fulfillment/courier records carry no tags or status text, so they confirm only
    implicitly: the order is shipped/delivered (and the tenant's implicit_on_ship is on). Cancelled/refunded/voided never.
    """
    if new_status in NON_REVENUE_STATUSES:
        return None
    if not from_fulfillment:
        return parsed.get("confirmation_source")
    if new_status in ("shipped", "delivered") and effective_confirmation_rules(tenant)["implicit_on_ship"]:
        return SOURCE_IMPLICIT_SHIPPED
    return None


async def _process_parsed(session, tenant, delivery, parsed, platform, topic, payload, digest, sender, event_time, now,
                          event_type: Optional[EventType] = None):
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
        order.value = parsed["total_price"]  # NET collected value (total minus successful refund transactions), S2-4
        if hasattr(order, "order_total") and parsed.get("order_total") is not None:
            order.order_total = parsed["order_total"]
        order.currency = parsed["currency"]
        order.created_at_platform = order.created_at_platform or _platform_created_at(platform, payload)
        # Platforms can redact customer fields on later updates: never overwrite a hash with nothing.
        order.email_hash = hash_email(parsed["email"]) or order.email_hash
        order.phone_hash = hash_phone(parsed["phone"], parsed["currency"], tenant.country,
                                      ship_country=(parsed.get("address") or {}).get("country")) or order.phone_hash
        for key, hashed in match_hashes_from_parsed(parsed).items():  # external_id + address keys, hashes only
            if hashed:
                setattr(order, f"{key}_hash", hashed)
        _merge_attribution(order, parsed.get("attribution"))
    else:
        if parsed.get("cod_amount") is not None and order.value <= 0.0:
            order.value = float(parsed["cod_amount"])
        if parsed.get("is_cod") is True and not order.is_cod:
            order.is_cod = True

    confirmation_source = _confirmation_source(parsed, new_status, tenant, from_fulfillment)
    is_confirmed = confirmation_source is not None
    if is_confirmed and order.confirmed_at is None:
        platform_time = _parse_platform_time(parsed.get("updated_at")) if parsed.get("updated_at") else None
        order.confirmed_at = platform_time or now
        order.confirmation_source = confirmation_source

    if new_status in DELIVERED_EVENT.converting_statuses and order.delivered_at is None:
        order.delivered_at = now  # first time we saw delivered/paid: starts the settlement window
    session.flush()
    if not from_fulfillment and (parsed.get("client_ip") or parsed.get("user_agent")) \
            and not _has_final_event(session, order.id):
        capture_checkout_context(session, order.id, parsed.get("client_ip"), parsed.get("user_agent"), now)  # D-006
    if new_status != previous_status:
        session.add(OrderStatusEvent(order_id=order.id, status=new_status, source="webhook", topic=topic,
                                     payload_sha256=digest))
    session.commit()

    target_event = event_type
    if target_event is None:
        if is_confirmed and new_status not in _REVENUE_STATUSES:
            target_event = CONFIRMED_EVENT
        else:
            target_event = DELIVERED_EVENT

    # Funnel invariant (Confirmed contains Delivered): an order that is confirmed (explicit OR implicit shipped/paid, also
    # when first observed already delivered/paid) gets its ConfirmedOrder before the DeliveredPurchase if it has none yet.
    # For a prepaid order payment IS the confirmation. Cancelled/refunded orders and cancellation-tagged ones never confirm.
    if (is_confirmed or order.confirmed_at is not None) and target_event == DELIVERED_EVENT \
            and new_status not in NON_REVENUE_STATUSES:
        has_confirmed = session.scalar(select(CapiEvent.id).where(
            CapiEvent.tenant_id == tenant.id,
            CapiEvent.order_id == order.id,
            CapiEvent.event_name == CONFIRMED_EVENT.name
        ).limit(1)) is not None
        if not has_confirmed:
            conf_time = event_time
            if conf_time is None:  # first observation of the confirmation (confirmed_at), not the delivery time
                conf_time = int((order.confirmed_at or now).timestamp())
            conf_decision = _decide(session, sender, tenant, order, now, CONFIRMED_EVENT, event_time=conf_time, is_confirmed=True)
            if conf_decision["action"] == "READY_TO_EMIT":
                conf_claim = CapiEvent(
                    tenant_id=tenant.id, order_id=order.id, event_name=CONFIRMED_EVENT.name,
                    event_id=CONFIRMED_EVENT.event_id(order.platform_order_id),
                    event_time=conf_decision["payload"]["data"][0]["event_time"],
                    status="scheduled", due_at=now
                )
                session.add(conf_claim)
                try:
                    session.commit()
                    await _dispatch_scheduled(session, tenant, conf_claim, order, sender, now)
                except IntegrityError:
                    session.rollback()

    if target_event == CONFIRMED_EVENT:
        conf_time = event_time
        if conf_time is None:
            if parsed.get("updated_at"):
                pt = _parse_platform_time(parsed["updated_at"])
                conf_time = int(pt.timestamp()) if pt else int(now.timestamp())
            elif order.confirmed_at:
                conf_time = int(order.confirmed_at.timestamp())
            else:
                conf_time = int(now.timestamp())

        decision = _decide(session, sender, tenant, order, now, CONFIRMED_EVENT, event_time=conf_time, is_confirmed=is_confirmed)
        action = decision["action"]
        if action not in ("READY_TO_EMIT", "STALE"):
            _finish(session, delivery, True)
            return _result(action, order.platform_order_id, None, decision)

        event_id = CONFIRMED_EVENT.event_id(order.platform_order_id)
        if action == "STALE":
            claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=CONFIRMED_EVENT.name, event_id=event_id,
                              event_time=conf_time, status="stale", error_type="event_time_out_of_window")
        else:
            claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=CONFIRMED_EVENT.name,
                              event_id=decision["event_id"], event_time=decision["payload"]["data"][0]["event_time"],
                              status="scheduled", due_at=now)
        session.add(claim)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            _finish(session, delivery, True)
            return _result("ALREADY_EMITTED", order.platform_order_id, None, decision)

        if action == "STALE":
            _finish(session, delivery, True)
            return _result(action, order.platform_order_id, claim.status, decision)

        capi_status = await _dispatch_scheduled(session, tenant, claim, order, sender, now)
        _finish(session, delivery, True)
        return _result({"sent": "SENT", "shadow": "SHADOW", "late_delivery": "LATE_DELIVERY", "stale": "STALE"}
                       .get(capi_status, "FAILED"), order.platform_order_id, capi_status, decision)

    decision = _decide(session, sender, tenant, order, now, DELIVERED_EVENT, event_time=event_time)
    action = decision["action"]
    if action not in ("READY_TO_EMIT", "STALE", "LATE_DELIVERY"):
        _finish(session, delivery, True)
        return _result(action, order.platform_order_id, None, decision)

    placed_epoch = int(order_placed_at(order).timestamp())
    event_id = DELIVERED_EVENT.event_id(order.platform_order_id)
    due_at = None
    if action == "STALE":
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=DELIVERED_EVENT.name, event_id=event_id,
                          event_time=placed_epoch if event_time is None else event_time, status="stale",
                          error_type="event_time_out_of_window")
    elif action == "LATE_DELIVERY":
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=DELIVERED_EVENT.name, event_id=event_id,
                          event_time=placed_epoch, status="late_delivery", error_type="past_cutoff")
    else:
        due_at = compute_due_at(order.delivered_at or now, order_placed_at(order), settlement_hours_for(tenant))
        claim = CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=decision["event_name"],
                          event_id=decision["event_id"], event_time=decision["payload"]["data"][0]["event_time"],
                          status="scheduled", due_at=due_at)
    session.add(claim)
    try:
        session.commit()  # the UNIQUE(tenant, order, event_name) constraint is the idempotency claim
    except IntegrityError:
        session.rollback()
        _finish(session, delivery, True)
        return _result("ALREADY_EMITTED", order.platform_order_id, None, decision)

    if action in ("STALE", "LATE_DELIVERY"):
        _finish(session, delivery, True)
        return _result(action, order.platform_order_id, claim.status, decision)

    if due_at > now:
        _finish(session, delivery, True)
        return _result("SCHEDULED", order.platform_order_id, "scheduled", decision, due_at)

    # Already due (settlement 0h, or close to the cutoff): dispatch in-line through the same path as send_due_events.
    capi_status = await _dispatch_scheduled(session, tenant, claim, order, sender, now)
    _finish(session, delivery, True)
    return _result({"sent": "SENT", "shadow": "SHADOW", "late_delivery": "LATE_DELIVERY", "stale": "STALE"}
                   .get(capi_status, "FAILED"), order.platform_order_id, capi_status, decision)


async def send_due_events(
    session: Session,
    now: Optional[datetime] = None,
    sender=capi_sender,
    limit: int = 500
) -> Dict[str, int]:
    """
    Sends `scheduled` capi_events whose due_at has passed (SQL-filtered on the (status, due_at) index, oldest first,
    at most `limit` per call). Each row is claimed atomically (scheduled -> pending), then the order is re-evaluated as
    it is NOW: cancelled / fully refunded / zero net value since -> `stale` (error_type no_longer_eligible); past the
    placed+6.5d cutoff -> `late_delivery`; otherwise the payload is rebuilt with the CURRENT net value, the stored
    hashes and the decrypted checkout IP/UA, and sent (live) or recorded as `shadow` (shadow tenants), exactly like a
    first send. Rows of missing/inactive tenants are left scheduled. The S1-5 scheduler calls this on a timer.
    Returns counts: due, sent, shadow, failed, stale, late_delivery, skipped (inactive tenant / lost the claim).
    """
    now = now or utcnow()
    rows = session.scalars(
        select(CapiEvent).where(CapiEvent.status == "scheduled", CapiEvent.due_at <= now)
        .order_by(CapiEvent.due_at, CapiEvent.id).limit(limit)
    ).all()
    counts = {"due": len(rows), "sent": 0, "shadow": 0, "failed": 0, "stale": 0, "late_delivery": 0, "skipped": 0}
    for event in rows:
        tenant = session.get(Tenant, event.tenant_id)
        order = session.get(Order, event.order_id)
        if tenant is None or order is None or not tenant.active:
            counts["skipped"] += 1
            continue
        status = await _dispatch_scheduled(session, tenant, event, order, sender, now)
        counts["skipped" if status is None else status if status in counts else "failed"] += 1
    return counts


async def retry_failed_events(
    session: Session,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sender=capi_sender,
    now: Optional[datetime] = None
) -> Dict[str, int]:
    """
    Resends capi_events with status `failed` and attempts < max_attempts (plus orphaned `pending` claims older
    than PENDING_ORPHAN_SECONDS since they were claimed), for live tenants only. The payload is rebuilt from the
    stored order with the stored event_time (the order's placed time) and the decrypted checkout IP/UA, so a retry
    past the placed+6.5d cutoff becomes `late_delivery`, past Meta's 7-day window `stale`, and an order that has since
    been cancelled/refunded is not sent (`stale`, error_type no_longer_eligible). Rows failing for a missing
    credential/dataset id do not consume attempts, so they resume once the tenant is fixed.
    Returns counts: retried, sent, failed, stale, late_delivery, skipped.
    """
    now = now or utcnow()
    orphan_cutoff = now - timedelta(seconds=PENDING_ORPHAN_SECONDS)
    candidates = session.scalars(
        select(CapiEvent).where(
            CapiEvent.attempts < max_attempts,
            or_(CapiEvent.status == "failed",
                and_(CapiEvent.status == "pending",
                     func.coalesce(CapiEvent.claimed_at, CapiEvent.created_at) <= orphan_cutoff))
        ).order_by(CapiEvent.id)
    ).all()
    counts = {"retried": 0, "sent": 0, "failed": 0, "stale": 0, "late_delivery": 0, "skipped": 0}
    for event in candidates:
        tenant = session.get(Tenant, event.tenant_id)
        order = session.get(Order, event.order_id)
        if tenant is None or order is None or tenant.mode != "live" or not tenant.active:
            counts["skipped"] += 1
            continue
        decision = _decide(session, sender, tenant, order, now, EVENT_TYPES[event.event_name],
                           event_time=event.event_time, with_context=True)
        if decision["action"] != "READY_TO_EMIT":
            status = await _resolve(session, tenant, event, decision, sender)
            counts["late_delivery" if status == "late_delivery" else "stale"] += 1
            continue
        counts["retried"] += 1
        status = await _send(session, tenant, event, decision["payload"], sender)
        counts["sent" if status == "sent" else "failed"] += 1
    return counts
