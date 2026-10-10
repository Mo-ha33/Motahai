"""
tenant_api.py — Read-only Core API the Tariq agent (via Hermes) calls to see one store's COD-engine state (S3-1).

  GET /v1/tenants/{tenant_key}/status
  GET /v1/tenants/{tenant_key}/signal-health?window_days=7      (1..28)

tenant_key = tenants.shop_domain (Shopify *.myshopify.com domain / Salla merchant id), the same convention as
capture_routes.py; it is looked up with db.get_tenant_by_shop_domain (which normalizes case/whitespace).

Auth: Bearer HERMES_TENANT_API_KEY (env, read per request), compared with hmac.compare_digest. This is a credential for
the Hermes agent, deliberately separate from OPERATOR_API_KEY (the human approval key): if both are set to the same
value, or the tenant key is unset, nobody is authenticated (fail closed, 401). Auth runs as a dependency, so it answers
before path/query validation (a bad window_days with no token is 401, not 422). An unknown tenant is a generic 404.

Read-only and PII-free: counts, flags and timestamps only. No emails, phones, names, IPs, user agents, order ids,
addresses, credentials/ciphertext or webhook payloads are ever selected or returned; logs carry only the numeric
tenant id. No CAPI calls, no writes.

--- /status fields -----------------------------------------------------------------------------------------------
  tenant_key, platform, mode ("shadow"|"live"), active, created_at,
  has_meta_credentials  a `meta_capi_token` row exists in `credentials` for the tenant (existence only, never the value)
  last_webhook_at       latest webhook_deliveries.received_at with signature_ok = TRUE for the tenant (None if none)
  webhook_failures_7d   webhook_deliveries rows of the tenant received in the last 7 days that are real failures:
                        non-duplicate, error_type != 'topic_not_handled' (a deliberately ignored topic), and either
                        signature_ok = FALSE or processed_ok = FALSE
  last_capi_event_at    latest coalesce(capi_events.sent_at, created_at) over the tenant's sent / shadow / failed rows
  webhooks_verified     at least one webhook_deliveries row for this tenant has signature_ok = TRUE, i.e. a webhook
                        whose signature was cryptographically verified was received (regardless of whether processing
                        later succeeded). Unauthenticated or unknown-tenant deliveries are never attributed to the
                        tenant (signature_ok FALSE / tenant_id NULL), so they cannot make this true.
  onboarded             active AND has_meta_credentials AND meta_dataset_id is set (the store can actually send)

--- /signal-health fields ----------------------------------------------------------------------------------------
Window = [now - window_days, now) UTC. Every number comes from the digest.py q_* queries (orders placed in the window,
CapiEvent time = coalesce(sent_at, created_at)); only `failed` / `stale` CAPI counts, which digest.q_signal_health does
not expose, use a small query here with the same event-time convention and the same event names. "delivered" counts
orders placed in the window that reached delivered (as of now); `partially_refunded` is not reported because orders do
not store that status (a partial refund shows as a lower net value, not a separate status).
"""

import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .db import CapiEvent, Credential, Tenant, WebhookDelivery, get_session_factory, get_tenant_by_shop_domain
from .digest import (
    SIGNAL_EVENT_NAMES, q_cohort_delivery, q_incidents, q_refused_cod, q_signal_health, q_week_orders
)

logger = logging.getLogger("ameen_workforce.tenant_api")

TENANT_API_KEY_ENV = "HERMES_TENANT_API_KEY"
OPERATOR_KEY_ENV = "OPERATOR_API_KEY"
META_CAPI_TOKEN_KIND = "meta_capi_token"
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 28
WEBHOOK_FAILURE_DAYS = 7
IGNORED_TOPIC_ERROR = "topic_not_handled"
MAX_INCIDENTS = 20

router = APIRouter(prefix="/v1/tenants/{tenant_key}")


def require_tenant_api_key(authorization: Optional[str] = Header(None)) -> None:
    """
    Authenticates the Hermes agent for the read-only tenant API via Bearer HERMES_TENANT_API_KEY.
    Fails closed: unset key, or a key equal to OPERATOR_API_KEY (the human-approval credential must never double as an
    agent credential), authenticates nobody.
    """
    key = os.environ.get(TENANT_API_KEY_ENV, "")
    if not key:
        logger.error("%s is not configured; refusing all tenant API requests", TENANT_API_KEY_ENV)
        raise HTTPException(status_code=401, detail="Authentication required")
    operator_key = os.environ.get(OPERATOR_KEY_ENV, "")
    if operator_key and hmac.compare_digest(key.encode(), operator_key.encode()):
        logger.error("%s must differ from %s; refusing all tenant API requests", TENANT_API_KEY_ENV, OPERATOR_KEY_ENV)
        raise HTTPException(status_code=401, detail="Authentication required")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented.encode(), key.encode()):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})


def get_tenant_api_session_factory() -> sessionmaker:
    """FastAPI dependency (overridable in tests) returning the sessionmaker used for tenant API reads."""
    return get_session_factory()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _get_tenant_or_404(session: Session, tenant_key: str) -> Tenant:
    tenant = get_tenant_by_shop_domain(session, tenant_key)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    return tenant


def _q_unhealthy_capi(session: Session, tenant_id: int, window) -> Dict[str, int]:
    """failed / stale signal CapiEvent counts in the window (digest.q_signal_health does not expose these two)."""
    event_time = func.coalesce(CapiEvent.sent_at, CapiEvent.created_at)
    rows = session.execute(
        select(CapiEvent.status, func.count(CapiEvent.id)).where(
            CapiEvent.tenant_id == tenant_id,
            CapiEvent.event_name.in_(SIGNAL_EVENT_NAMES),
            CapiEvent.status.in_(("failed", "stale")),
            event_time >= window[0], event_time < window[1],
        ).group_by(CapiEvent.status)
    ).all()
    out = {"failed": 0, "stale": 0}
    for status, count in rows:
        out[status] = int(count or 0)
    return out


def _build_status(session: Session, tenant: Tenant, now: datetime) -> Dict[str, Any]:
    has_credentials = session.scalar(
        select(func.count(Credential.id)).where(Credential.tenant_id == tenant.id,
                                                Credential.kind == META_CAPI_TOKEN_KIND)) or 0
    verified = and_(WebhookDelivery.tenant_id == tenant.id, WebhookDelivery.signature_ok.is_(True))
    last_webhook_at = session.scalar(select(func.max(WebhookDelivery.received_at)).where(verified))
    webhooks_verified = session.scalar(select(WebhookDelivery.id).where(verified).limit(1)) is not None
    failures = session.scalar(
        select(func.count(WebhookDelivery.id)).where(
            WebhookDelivery.tenant_id == tenant.id,
            WebhookDelivery.received_at >= now - timedelta(days=WEBHOOK_FAILURE_DAYS),
            WebhookDelivery.is_duplicate.is_(False),
            or_(WebhookDelivery.error_type.is_(None), WebhookDelivery.error_type != IGNORED_TOPIC_ERROR),
            or_(WebhookDelivery.signature_ok.is_(False), WebhookDelivery.processed_ok.is_(False)),
        )) or 0
    last_capi_event_at = session.scalar(
        select(func.max(func.coalesce(CapiEvent.sent_at, CapiEvent.created_at))).where(
            CapiEvent.tenant_id == tenant.id, CapiEvent.status.in_(("sent", "shadow", "failed"))))
    has_meta_credentials = bool(has_credentials)
    return {
        "tenant_key": tenant.shop_domain,
        "platform": tenant.platform,
        "mode": tenant.mode,
        "active": bool(tenant.active),
        "created_at": _iso(tenant.created_at),
        "has_meta_credentials": has_meta_credentials,
        "last_webhook_at": _iso(last_webhook_at),
        "webhook_failures_7d": int(failures),
        "last_capi_event_at": _iso(last_capi_event_at),
        "webhooks_verified": webhooks_verified,
        "onboarded": bool(tenant.active) and has_meta_credentials and bool(tenant.meta_dataset_id),
    }


def _build_signal_health(session: Session, tenant: Tenant, window_days: int, now: datetime) -> Dict[str, Any]:
    window = (now - timedelta(days=window_days), now)
    week = q_week_orders(session, tenant.id, window)
    cohort = q_cohort_delivery(session, tenant.id, window)
    refused = q_refused_cod(session, tenant.id, window)
    signal = q_signal_health(session, tenant.id, window)
    unhealthy = _q_unhealthy_capi(session, tenant.id, window)
    shadow = signal["confirmed_shadow"] + signal["delivered_shadow"]
    open_incidents = [i for i in q_incidents(session, tenant.id, window) if i["unresolved"]]
    return {
        "tenant_key": tenant.shop_domain,
        "window": {"start": _iso(window[0]), "end": _iso(window[1]), "days": window_days},
        "orders": {
            "placed": week["placed"], "cod": week["cod"], "confirmed": week["confirmed"],
            "delivered": int(cohort["delivered"]), "refused": int(refused["count"]),
        },
        "capi_events": {
            "sent": signal["confirmed_sent"] + signal["delivered_sent"],
            "shadow": shadow,
            "failed": unhealthy["failed"],
            "stale": unhealthy["stale"],
            "late_delivery": signal["late_delivery"],
        },
        "would_have_sent": shadow,
        "open_incidents": [{"type": i["kind"], "opened_at": _iso(i["detected_at"])}
                           for i in open_incidents[:MAX_INCIDENTS]],
    }


@router.get("/status")
def tenant_status(tenant_key: str, _auth: None = Depends(require_tenant_api_key),
                  factory: sessionmaker = Depends(get_tenant_api_session_factory)) -> Dict[str, Any]:
    """Engine state for one store: mode, credentials present, webhook health, last CAPI activity. See module docs."""
    with factory() as session:
        tenant = _get_tenant_or_404(session, tenant_key)
        logger.info("tenant status read for tenant %s", tenant.id)
        return _build_status(session, tenant, _now())


@router.get("/signal-health")
def tenant_signal_health(
    tenant_key: str, _auth: None = Depends(require_tenant_api_key),
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS),
    factory: sessionmaker = Depends(get_tenant_api_session_factory)
) -> Dict[str, Any]:
    """Order and CAPI signal counts over [now - window_days, now), from the digest q_* queries. See module docs."""
    with factory() as session:
        tenant = _get_tenant_or_404(session, tenant_key)
        logger.info("tenant signal-health read for tenant %s (%dd)", tenant.id, window_days)
        return _build_signal_health(session, tenant, window_days, _now())
