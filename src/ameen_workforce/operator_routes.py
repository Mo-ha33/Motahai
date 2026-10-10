"""
operator_routes.py — Human-operator endpoints (OPERATOR_API_KEY bearer, see require_operator).

  POST /operator/orders/{shop_domain}/{order_id}/confirm       manual ConfirmedOrder (M2-2)
  POST /operator/tenants/{shop_domain}/audiences/export        hashed Meta audience CSV export (M3-4)

The tenant comes ONLY from the URL; orders are looked up by (tenant_id, platform_order_id) because order numbers
collide across stores. Responses never contain raw PII (audience files hold SHA-256 hashes; the API returns paths
and row counts only).
"""

import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .audiences import export_tenant_audiences
from .capi_service import capi_sender
from .config import settings
from .db import Order, get_session_factory, get_tenant_by_shop_domain
from .order_pipeline import emit_confirmed_order

logger = logging.getLogger("ameen_workforce.service")

AUDIENCE_EXPORT_DIR_ENV = "AUDIENCE_EXPORT_DIR"
DEFAULT_AUDIENCE_EXPORT_DIR = "out/audiences"

router = APIRouter()


def require_operator(authorization: Optional[str] = Header(None)) -> None:
    """
    Authenticates a HUMAN operator for Rule D-003 approvals via env OPERATOR_API_KEY.
    This key is deliberately separate from HERMES_API_KEY / any agent or webhook credential:
    an agent that can call the tools must never be able to mint its own approval.
    Used as a dependency so authentication runs before request-body validation.
    Fails closed: if OPERATOR_API_KEY is unset (or equals HERMES_API_KEY) nobody is authenticated.
    """
    operator_key = os.environ.get("OPERATOR_API_KEY", "")
    if not operator_key:
        logger.error("OPERATOR_API_KEY is not configured; refusing all approval requests")
        raise HTTPException(status_code=401, detail="Operator authentication required")
    if settings.HERMES_API_KEY and hmac.compare_digest(operator_key.encode(), settings.HERMES_API_KEY.encode()):
        logger.error("OPERATOR_API_KEY must differ from HERMES_API_KEY; refusing all approval requests")
        raise HTTPException(status_code=401, detail="Operator authentication required")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented.encode(), operator_key.encode()):
        raise HTTPException(status_code=401, detail="Operator authentication required", headers={"WWW-Authenticate": "Bearer"})


def get_session_factory_dep() -> sessionmaker:
    """Overridable in tests."""
    return get_session_factory()


def get_capi_sender_dep():
    """Overridable in tests."""
    return capi_sender


def audience_export_dir() -> str:
    return os.environ.get(AUDIENCE_EXPORT_DIR_ENV) or DEFAULT_AUDIENCE_EXPORT_DIR


@router.post("/operator/orders/{shop_domain}/{order_id}/confirm")
async def confirm_order(shop_domain: str, order_id: str, _operator: None = Depends(require_operator),
                        factory: sessionmaker = Depends(get_session_factory_dep),
                        sender=Depends(get_capi_sender_dep)):
    """Manual ConfirmedOrder (call centre / WhatsApp). Idempotent: a repeat returns 200 `already_confirmed`."""
    with factory() as session:
        tenant = get_tenant_by_shop_domain(session, shop_domain)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        order = session.scalar(select(Order.id).where(
            Order.tenant_id == tenant.id, Order.platform_order_id == str(order_id)))
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        tenant_id = tenant.id
        result = await emit_confirmed_order(session, tenant, str(order_id), sender=sender)
    action = result.get("action")
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail="Order not found")
    if action == "ALREADY_EMITTED":
        state = "already_confirmed"
    elif action in ("SUPPRESSED", "DEFERRED", "STALE"):
        state = "not_emitted"
    else:
        state = "confirmed"
    logger.info("Operator confirm tenant=%s action=%s", tenant_id, action)
    return {"status": state, "action": action, "order_id": str(order_id), "capi_status": result.get("capi_status")}


@router.post("/operator/tenants/{shop_domain}/audiences/export")
async def export_audiences(shop_domain: str, _operator: None = Depends(require_operator),
                           factory: sessionmaker = Depends(get_session_factory_dep)):
    """Writes the tenant's hashed exclude/seed CSVs; returns paths and row counts only (never rows or PII)."""
    with factory() as session:
        tenant = get_tenant_by_shop_domain(session, shop_domain)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        tenant_id = tenant.id
        result = export_tenant_audiences(session, tenant_id, audience_export_dir())
    logger.info("Operator audience export tenant=%s", tenant_id)
    return result
