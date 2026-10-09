"""
webhook_routes.py — Public webhook endpoints for Shopify and Salla (S1-2).

  POST /webhooks/shopify/{topic}   e.g. /webhooks/shopify/orders/updated
  POST /webhooks/salla             topic is the payload's `event` (e.g. order.status.updated)
  POST /webhooks/bosta/{shop_domain}   courier delivery updates (Bosta); Authorization header = per-tenant secret
  POST /webhooks/oto/{shop_domain}     courier order status (OTO); HMAC over orderId:status:timestamp, per-tenant secret

Courier routes (S2-5 / FX-1): the tenant comes ONLY from the URL (each merchant configures their own URL in their
courier dashboard); there are no un-scoped routes and no payload/query/header fallback. Order numbers are per-merchant
and collide across stores, so the order is always looked up by (tenant_id, platform_order_id). The secret is the
tenant's own credential of kind `bosta_webhook_secret` / `oto_webhook_secret` (credentials.store_credential, e.g. via
scripts/onboard_store.py); a tenant without one gets 401. There is no shared env secret. Checks run: tenant (404) ->
auth (401, audit row) -> JSON (400) -> background processing; nothing touches orders before auth succeeds.

Request flow (each step fails closed):
  1. Body > 1 MB -> 413 (read in a bounded stream, nothing is stored).
  2. Verify the signature over the RAW bytes (webhook_signatures.py). The secret comes from env
     SHOPIFY_APP_SECRET (app-level client secret) / SALLA_WEBHOOK_SECRET. Missing secret, missing header or
     mismatch -> 401 and a webhook_deliveries row with signature_ok=False (payload hash only, never the payload).
  3. Invalid JSON -> 400. Unknown tenant -> 404 (+ recorded row). Unhandled topic -> 200 "ignored" (+ recorded row).
  4. Otherwise answer 200 IMMEDIATELY and run order_pipeline.process_webhook in a FastAPI background task with its
     OWN session: platforms expect an answer within ~5 s and the Meta call can take longer.

DELIVERY GUARANTEE: because we answer 200 before processing, a background task that is lost (process killed between
the 200 and the commit) is NOT redelivered by the platform. The S1-5 reconciliation sweep (polling recent orders
through the platform API) is the safety net. An exception inside the task is recorded on the delivery row by
process_webhook and as an `incidents` row (kind webhook_processing_error) here.

Tenant lookup: Shopify by header X-Shopify-Shop-Domain -> tenants.shop_domain. Salla by the payload's top-level
`merchant` (INFERRED from Salla's payload format, verify against a real delivery) -> tenants.shop_domain, which holds
the merchant id as a string for Salla tenants (see db.get_tenant_by_shop_domain).

Privacy: never log bodies, signature headers, secrets, emails or phones. Logs carry platform, allowlisted topic,
tenant id and exception TYPE names only.
"""

import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .credentials import CredentialError, get_credential
from .db import Incident, Order, Tenant, WebhookDelivery, get_session_factory, get_tenant_by_shop_domain
from .order_pipeline import process_webhook
from .webhook_signatures import (
    BOSTA_AUTH_HEADER, OTO_SIGNATURE_HEADER, SALLA_SIGNATURE_HEADER, SHOPIFY_HMAC_HEADER,
    verify_bosta_auth, verify_oto_signature, verify_salla_signature, verify_shopify_hmac
)

logger = logging.getLogger("ameen_workforce.webhooks")

SHOPIFY_SECRET_ENV = "SHOPIFY_APP_SECRET"
SALLA_SECRET_ENV = "SALLA_WEBHOOK_SECRET"
# Courier secrets are PER TENANT (credentials table); there is deliberately no env/global fallback.
BOSTA_SECRET_KIND = "bosta_webhook_secret"
OTO_SECRET_KIND = "oto_webhook_secret"
MAX_BODY_BYTES = 1_000_000  # 1 MB
INCIDENT_KIND = "webhook_processing_error"

# Topics the pipeline can interpret. Anything else is acknowledged (so the platform stops retrying) and recorded.
SHOPIFY_TOPICS = frozenset({
    "orders/create", "orders/updated", "orders/paid", "orders/fulfilled", "orders/cancelled",
    "fulfillments/create", "fulfillments/update",
})
# order.shipment.* payloads describe a shipment, not an order, so parse_salla_order would misread them: excluded.
SALLA_TOPICS = frozenset({
    "order.created", "order.updated", "order.status.updated", "order.cancelled", "order.refunded",
})
BOSTA_TOPICS = frozenset({"delivery_update", "status_changed", "default"})
OTO_TOPICS = frozenset({"orderStatus", "order_status", "status_update", "default"})


router = APIRouter()


def get_session_factory_dep() -> sessionmaker:
    """FastAPI dependency (overridable in tests) returning the sessionmaker used for request and task sessions."""
    return get_session_factory()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _salla_merchant_id(payload: Any) -> Optional[str]:
    """Salla's top-level `merchant` (INFERRED: scalar id, tolerated as {"id": ...})."""
    if not isinstance(payload, dict):
        return None
    merchant = payload.get("merchant")
    if isinstance(merchant, dict):
        merchant = merchant.get("id")
    if isinstance(merchant, bool) or merchant in (None, ""):
        return None
    return str(merchant)


def _try_json(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _record_delivery(
    session: Session, tenant_id: Optional[int], platform: str, topic: str, delivery_id: Optional[str],
    raw_body: bytes, signature_ok: bool, processed_ok: bool, error_type: Optional[str]
) -> None:
    """Audit row written directly by the route (hash of the raw bytes only; the payload is never stored)."""
    digest = hashlib.sha256(raw_body).hexdigest()
    delivery_id = delivery_id[:128] if delivery_id else None
    topic = (topic or "unknown")[:64]
    dedupe_key = f"{platform}:{delivery_id}" if delivery_id else f"{platform}:{topic}:{digest}"
    session.add(WebhookDelivery(
        tenant_id=tenant_id, platform=platform, topic=topic, delivery_id=delivery_id, dedupe_key=dedupe_key[:255],
        payload_sha256=digest, signature_ok=signature_ok, processed_ok=processed_ok, error_type=error_type
    ))
    session.commit()


async def _process_in_background(
    factory: sessionmaker, tenant_id: int, platform: str, topic: str, payload: Dict[str, Any],
    delivery_id: Optional[str]
) -> None:
    """Runs after the 200 is sent, with its own session. Never raises."""
    session = factory()
    try:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise LookupError("tenant vanished")
        await process_webhook(session, tenant, platform, topic, payload, delivery_id=delivery_id, signature_ok=True)
    except Exception as exc:
        error_type = type(exc).__name__
        logger.error("Webhook background processing failed (%s/%s, tenant %s): %s", platform, topic, tenant_id,
                     error_type)
        try:
            session.rollback()
            session.add(Incident(tenant_id=tenant_id, kind=INCIDENT_KIND, severity="error",
                                 detail=f"{platform}/{topic}: {error_type}"))
            session.commit()
        except Exception as inc_exc:  # the incident write must not mask the original failure or crash the worker
            session.rollback()
            logger.error("Could not record webhook incident: %s", type(inc_exc).__name__)
    finally:
        session.close()


async def _ingest(
    request: Request, background_tasks: BackgroundTasks, factory: sessionmaker, platform: str,
    path_topic: Optional[str]
) -> Dict[str, str]:
    raw = await _read_body(request)  # raw bytes first: the signature covers exactly these
    headers = request.headers
    if platform == "shopify":
        secret = os.environ.get(SHOPIFY_SECRET_ENV, "")
        signed = verify_shopify_hmac(raw, headers.get(SHOPIFY_HMAC_HEADER), secret)
        delivery_id = headers.get("X-Shopify-Webhook-Id")
        allowed = SHOPIFY_TOPICS
    else:
        secret = os.environ.get(SALLA_SECRET_ENV, "")
        signed = verify_salla_signature(raw, headers.get(SALLA_SIGNATURE_HEADER), secret)
        delivery_id = None  # Salla supplies no delivery id header; dedupe falls back to platform+topic+payload hash
        allowed = SALLA_TOPICS

    session = factory()
    try:
        def find_tenant(payload: Optional[Dict[str, Any]]) -> Optional[Tenant]:
            if platform == "shopify":
                domain = headers.get("X-Shopify-Shop-Domain")
            else:
                domain = _salla_merchant_id(payload)
            tenant = get_tenant_by_shop_domain(session, domain) if domain else None
            return tenant if tenant is not None and tenant.platform == platform else None

        if not signed:
            logger.warning("Rejected %s webhook: %s", platform,
                           "signature secret not configured" if not secret else "signature missing or invalid")
            peek = _try_json(raw) if platform == "salla" else None  # unauthenticated: only labels the audit row
            tenant = find_tenant(peek)
            topic = path_topic if platform == "shopify" else (peek or {}).get("event") or "unknown"
            _record_delivery(session, tenant.id if tenant else None, platform, str(topic), delivery_id, raw,
                             False, False, "signature_not_configured" if not secret else "invalid_signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        payload = _try_json(raw)
        if payload is None:
            _record_delivery(session, None, platform, path_topic or "unknown", delivery_id, raw, True, False,
                             "invalid_json")
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        topic = path_topic if platform == "shopify" else payload.get("event")
        topic = topic if isinstance(topic, str) and topic else "unknown"
        tenant = find_tenant(payload)
        if tenant is None:
            logger.warning("Verified %s webhook for unknown tenant (topic %s)", platform,
                           topic if topic in allowed else "unlisted")
            _record_delivery(session, None, platform, topic, delivery_id, raw, True, False, "unknown_tenant")
            raise HTTPException(status_code=404, detail="Unknown shop")

        if topic not in allowed:
            logger.info("Ignoring unhandled %s topic for tenant %s", platform, tenant.id)
            _record_delivery(session, tenant.id, platform, topic, delivery_id, raw, True, False, "topic_not_handled")
            return {"status": "ignored"}

        tenant_id = tenant.id
    finally:
        session.close()

    background_tasks.add_task(_process_in_background, factory, tenant_id, platform, topic, payload, delivery_id)
    return {"status": "accepted"}


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@router.post("/webhooks/shopify/{topic:path}")
async def shopify_webhook(topic: str, request: Request, background_tasks: BackgroundTasks,
                          factory: sessionmaker = Depends(get_session_factory_dep)):
    return await _ingest(request, background_tasks, factory, "shopify", topic)


@router.post("/webhooks/salla")
async def salla_webhook(request: Request, background_tasks: BackgroundTasks,
                        factory: sessionmaker = Depends(get_session_factory_dep)):
    return await _ingest(request, background_tasks, factory, "salla", None)


async def _reject_courier(
    session: Session, tenant_id: Optional[int], platform: str, topic: str, raw: bytes, error_type: str,
    status_code: int, detail: str, signature_ok: bool = False
) -> None:
    """Records the audit row (hash only; never the payload, never an unauthenticated delivery id) and raises."""
    _record_delivery(session, tenant_id, platform, topic, None, raw, signature_ok, False, error_type)
    raise HTTPException(status_code=status_code, detail=detail)


def _courier_secret(session: Session, tenant: Tenant, kind: str) -> str:
    """Per-tenant courier secret. Missing, expired or undecryptable -> "" so verification fails closed (401)."""
    try:
        return get_credential(session, tenant.id, kind) or ""
    except CredentialError as exc:
        logger.error("Courier secret %s unreadable for tenant %s: %s", kind, tenant.id, type(exc).__name__)
        return ""


async def _ingest_bosta(
    request: Request, background_tasks: BackgroundTasks, factory: sessionmaker, shop_domain: str
) -> Dict[str, str]:
    """
    Order of checks (each fails closed): tenant from the URL (404) -> per-tenant secret + auth header (401) ->
    JSON (400) -> process in background. No order lookup happens before auth, and the tenant is NEVER inferred from
    the order id (order numbers are per-merchant and collide across stores).
    """
    raw = await _read_body(request)
    headers = request.headers

    session = factory()
    try:
        tenant = get_tenant_by_shop_domain(session, shop_domain) if shop_domain else None
        if tenant is None:
            logger.warning("Bosta webhook for unknown shop")
            await _reject_courier(session, None, "bosta", "delivery_update", raw, "unknown_tenant", 404, "Unknown shop",
                                  signature_ok=False)

        secret = _courier_secret(session, tenant, BOSTA_SECRET_KIND)
        auth_header = headers.get(BOSTA_AUTH_HEADER) or headers.get("X-Bosta-Signature")
        if not verify_bosta_auth(auth_header, secret):
            logger.warning("Rejected Bosta webhook for tenant %s: %s", tenant.id,
                           "secret not configured" if not secret else "invalid auth header")
            await _reject_courier(session, tenant.id, "bosta", "delivery_update", raw,
                                  "signature_not_configured" if not secret else "invalid_signature", 401,
                                  "Invalid webhook authentication")

        payload = _try_json(raw)
        if payload is None:
            await _reject_courier(session, tenant.id, "bosta", "delivery_update", raw, "invalid_json", 400,
                                  "Body must be a JSON object", signature_ok=True)

        data = payload.get("data") or payload.get("delivery") or payload
        data = data if isinstance(data, dict) else payload
        delivery_id = str(data.get("trackingNumber") or data.get("_id") or "") or None
        tenant_id = tenant.id
    finally:
        session.close()

    background_tasks.add_task(_process_in_background, factory, tenant_id, "bosta", "delivery_update", payload,
                              delivery_id)
    return {"status": "accepted"}


async def _ingest_oto(
    request: Request, background_tasks: BackgroundTasks, factory: sessionmaker, shop_domain: str
) -> Dict[str, str]:
    """Same check order as _ingest_bosta; the OTO signature covers orderId:status:timestamp (see webhook_signatures)."""
    raw = await _read_body(request)
    headers = request.headers

    session = factory()
    try:
        tenant = get_tenant_by_shop_domain(session, shop_domain) if shop_domain else None
        if tenant is None:
            logger.warning("OTO webhook for unknown shop")
            await _reject_courier(session, None, "oto", "orderStatus", raw, "unknown_tenant", 404, "Unknown shop",
                                  signature_ok=False)

        secret = _courier_secret(session, tenant, OTO_SECRET_KIND)
        payload = _try_json(raw)
        data = payload.get("data") if payload is not None else None
        data = data if isinstance(data, dict) else (payload or {})
        order_id = str(data.get("orderId") or data.get("order_id") or data.get("reference_id") or "")
        status_val = str(data.get("status") or data.get("dcStatus") or "")
        timestamp_val = str(data.get("timestamp") or "")
        sig_val = str(data.get("signature") or headers.get(OTO_SIGNATURE_HEADER) or "")

        if not verify_oto_signature(order_id, status_val, timestamp_val, sig_val, secret):
            logger.warning("Rejected OTO webhook for tenant %s: %s", tenant.id,
                           "secret not configured" if not secret else "invalid signature")
            await _reject_courier(session, tenant.id, "oto", "orderStatus", raw,
                                  "signature_not_configured" if not secret else "invalid_signature", 401,
                                  "Invalid webhook signature")

        if payload is None:
            await _reject_courier(session, tenant.id, "oto", "orderStatus", raw, "invalid_json", 400,
                                  "Body must be a JSON object", signature_ok=True)

        delivery_id = str(data.get("trackingNumber") or data.get("orderId") or "") or None
        tenant_id = tenant.id
    finally:
        session.close()

    background_tasks.add_task(_process_in_background, factory, tenant_id, "oto", "orderStatus", payload, delivery_id)
    return {"status": "accepted"}


@router.post("/webhooks/bosta/{shop_domain}")
async def bosta_webhook(shop_domain: str, request: Request, background_tasks: BackgroundTasks,
                        factory: sessionmaker = Depends(get_session_factory_dep)):
    return await _ingest_bosta(request, background_tasks, factory, shop_domain)


@router.post("/webhooks/oto/{shop_domain}")
async def oto_webhook(shop_domain: str, request: Request, background_tasks: BackgroundTasks,
                      factory: sessionmaker = Depends(get_session_factory_dep)):
    return await _ingest_oto(request, background_tasks, factory, shop_domain)
