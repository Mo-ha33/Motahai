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

Known risk: HERMES_TENANT_API_KEY can read ANY tenant's counts. The caller (the Hermes tool, S3-2) must bind
tenant_key to the onboarded store of the current conversation before calling; this module does not enforce that.

Read-only and PII-free: counts, flags and timestamps only. No emails, phones, names, IPs, user agents, order ids,
addresses, credentials/ciphertext or webhook payloads are ever selected or returned; logs carry only the numeric
tenant id. No CAPI calls, no writes.

--- /status fields -----------------------------------------------------------------------------------------------
  tenant_key, platform, mode ("shadow"|"live"), active, created_at,
  has_meta_credentials  the checklist step `meta_capi_token` is done (a credentials row exists; never the value)
  last_webhook_at       latest webhook_deliveries.received_at with signature_ok = TRUE for the tenant (None if none)
  webhook_failures_7d   webhook_deliveries rows of the tenant received in the last 7 days that are real failures:
                        non-duplicate, error_type != 'topic_not_handled' (a deliberately ignored topic), and either
                        signature_ok = FALSE or processed_ok = FALSE
  last_capi_event_at    latest coalesce(capi_events.sent_at, created_at) over the tenant's sent / shadow / failed rows
  webhooks_verified     at least one webhook_deliveries row for this tenant has signature_ok = TRUE, i.e. a webhook
                        whose signature was cryptographically verified was received (regardless of whether processing
                        later succeeded). Unauthenticated or unknown-tenant deliveries are never attributed to the
                        tenant (signature_ok FALSE / tenant_id NULL), so they cannot make this true.
  checklist_ready       onboarding_routes.compute_checklist(...)["ready_for_live"], the SAME function the operator
                        GET /v1/operator/onboarding/tenants/{id}/checklist uses (all required steps done)
  onboarded             = checklist_ready (defined from the shared checklist so operator and Tariq cannot disagree)
  missing_required_steps  ids of required checklist steps not done (ids only, e.g. "courier_secret", "api_key")
  test_event_code_set   the tenant has a Meta test_event_code (the bool only, never the code)

--- /signal-health fields ----------------------------------------------------------------------------------------
Same window conventions as stats_routes (hence the Sunday digest and the portal): `window` is the `window_days` full
UTC days ending at today's UTC midnight (stats_routes.default_window); `cohort_window` is that window shifted back 7
days. Week-based numbers use `window`, matured-cohort numbers use `cohort_window`. All of them come from
stats_routes.compute_section (-> digest q_* queries); only the `failed` / `stale` CAPI counts, which
digest.q_signal_health does not expose, use a small query here (same event-time convention and event names).
  orders.{placed,cod,confirmed}      q_week_orders over `window`
  cohort.{orders,delivered,refused}  q_cohort_delivery (all orders) and q_refused_cod over `cohort_window`
                                     (refused = COD orders cancelled/refunded after dispatch)
  capi_events, would_have_sent       q_signal_health over `window`; would_have_sent = the shadow count
`partially_refunded` is not reported: orders do not store that status (a partial refund is a lower net value).
"""

import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .db import CapiEvent, Tenant, WebhookDelivery, get_session_factory, get_tenant_by_shop_domain
from .digest import SIGNAL_EVENT_NAMES, q_incidents
from .onboarding_routes import compute_checklist
from .stats_routes import cohort_window, compute_section, default_window, window_iso

logger = logging.getLogger("ameen_workforce.tenant_api")

TENANT_API_KEY_ENV = "HERMES_TENANT_API_KEY"
OPERATOR_KEY_ENV = "OPERATOR_API_KEY"
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
    checklist = compute_checklist(session, tenant)
    steps = checklist["steps"]
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
    return {
        "tenant_key": tenant.shop_domain,
        "platform": tenant.platform,
        "mode": tenant.mode,
        "active": bool(tenant.active),
        "created_at": _iso(tenant.created_at),
        "has_meta_credentials": any(s["key"] == "meta_capi_token" and s["status"] == "done" for s in steps),
        "last_webhook_at": _iso(last_webhook_at),
        "webhook_failures_7d": int(failures),
        "last_capi_event_at": _iso(last_capi_event_at),
        "webhooks_verified": webhooks_verified,
        "checklist_ready": checklist["ready_for_live"],
        "onboarded": checklist["ready_for_live"],
        "missing_required_steps": [s["key"] for s in steps if s["required"] and s["status"] != "done"],
        "test_event_code_set": tenant.meta_test_event_code is not None,
    }


def _build_signal_health(session: Session, tenant: Tenant, window_days: int, now: datetime) -> Dict[str, Any]:
    window = default_window(window_days, now)
    cohort = cohort_window(window)
    week = compute_section(session, tenant.id, "week-orders", window)
    cohort_delivery = compute_section(session, tenant.id, "cohort-delivery", window)["all"]
    refused = compute_section(session, tenant.id, "refused-cod", window)
    signal = compute_section(session, tenant.id, "signal-health", window)
    unhealthy = _q_unhealthy_capi(session, tenant.id, window)
    shadow = signal["confirmed_shadow"] + signal["delivered_shadow"]
    open_incidents = [i for i in q_incidents(session, tenant.id, window) if i["unresolved"]]
    return {
        "tenant_key": tenant.shop_domain,
        "window": {**window_iso(window), "days": window_days},
        "cohort_window": window_iso(cohort),
        "orders": {"placed": week["placed"], "cod": week["cod"], "confirmed": week["confirmed"]},
        "cohort": {"orders": int(cohort_delivery["orders"]), "delivered": int(cohort_delivery["delivered"]),
                   "refused": int(refused["count"])},
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
    """Engine state for one store: mode, onboarding checklist, webhook health, last CAPI activity. See module docs."""
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
    """Order and CAPI signal counts for the stats-aligned window and matured-cohort window. See module docs."""
    with factory() as session:
        tenant = _get_tenant_or_404(session, tenant_key)
        logger.info("tenant signal-health read for tenant %s (%dd)", tenant.id, window_days)
        return _build_signal_health(session, tenant, window_days, _now())
