"""
onboarding_routes.py — operator onboarding API (M3-6). All behind require_operator.

  POST /v1/operator/onboarding/tenants                          create tenant (shadow mode)
  POST /v1/operator/onboarding/tenants/{id}/meta                store Meta dataset id + CAPI token (token never echoed)
  POST /v1/operator/onboarding/tenants/{id}/courier-secrets     generate Bosta/OTO webhook secrets (returned ONCE)
  POST /v1/operator/onboarding/tenants/{id}/api-key             issue the tenant's first API key (returned ONCE)
  GET  /v1/operator/onboarding/tenants/{id}/checklist           per-step status + ready_for_live

Switching a tenant to live is deliberately NOT here; it stays a separate operator action.
Platform webhook registration (Shopify/Salla) is MANUAL: the checklist lists the URLs the merchant must register.
"""

import secrets
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .credentials import CredentialError, get_credential, store_credential
from .db import (
    Credential, Tenant, TenantApiKey, create_tenant, get_tenant_by_shop_domain, normalize_test_event_code,
    set_meta_test_event_code
)
from .onboarding import COURIER_SECRET_KINDS, ensure_fernet_key, store_courier_secrets
from .tenant_auth import create_tenant_key
from .webhook_routes import get_session_factory_dep
from .webhook_signatures import BOSTA_MIN_SECRET_LENGTH

router = APIRouter(prefix="/v1/operator/onboarding", tags=["onboarding"])


def _operator(authorization: Optional[str] = Header(None)) -> None:
    # Late import: service.py includes this router, so it cannot be imported at module load.
    from .service import require_operator
    require_operator(authorization)


class CreateTenantRequest(BaseModel):
    shop_domain: str = Field(..., min_length=3, max_length=255)
    platform: Literal["shopify", "salla"]
    country: str = Field(..., min_length=2, max_length=2)
    currency: str = Field(..., min_length=3, max_length=3)
    name: Optional[str] = Field(None, max_length=200)


class MetaRequest(BaseModel):
    meta_dataset_id: str = Field(..., pattern=r"^\d{5,32}$")
    meta_capi_token: str = Field(..., min_length=10, max_length=1024, repr=False)
    test_event_code: Optional[str] = Field(None, max_length=64)


class CourierSecretsRequest(BaseModel):
    couriers: List[Literal["bosta", "oto"]] = Field(default_factory=lambda: ["bosta", "oto"], min_length=1)
    rotate: bool = False  # required to overwrite an existing secret


def _tenant(session: Session, tenant_id: int) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _kinds(session: Session, tenant_id: int) -> set:
    return set(session.scalars(select(Credential.kind).where(Credential.tenant_id == tenant_id)).all())


def _base() -> str:
    return settings.EMPLOYEES_PORTAL.rstrip("/")


def _active_key_exists(session: Session, tenant_id: int) -> bool:
    return session.scalar(select(TenantApiKey.id).where(
        TenantApiKey.tenant_id == tenant_id, TenantApiKey.revoked_at.is_(None)).limit(1)) is not None


@router.post("/tenants", status_code=201)
def create_tenant_endpoint(req: CreateTenantRequest, _op: None = Depends(_operator),
                           factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        if get_tenant_by_shop_domain(session, req.shop_domain.strip().lower()) is not None:
            raise HTTPException(status_code=409, detail="A tenant with this shop_domain already exists")
        try:
            tenant = create_tenant(session, name=(req.name or req.shop_domain).strip(), platform=req.platform,
                                   shop_domain=req.shop_domain, country=req.country, currency=req.currency,
                                   mode="shadow")
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=409, detail="A tenant with this shop_domain already exists")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"tenant_id": tenant.id, "shop_domain": tenant.shop_domain, "platform": tenant.platform,
                "country": tenant.country, "currency": tenant.currency, "mode": tenant.mode,
                "live_sending": False}


@router.post("/tenants/{tenant_id}/meta")
def store_meta(tenant_id: int, req: MetaRequest, _op: None = Depends(_operator),
               factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        tenant = _tenant(session, tenant_id)
        if req.test_event_code is not None:
            try:
                normalize_test_event_code(req.test_event_code)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        ensure_fernet_key()
        tenant.meta_dataset_id = req.meta_dataset_id
        session.commit()
        store_credential(session, tenant.id, "meta_capi_token", req.meta_capi_token.strip())
        if req.test_event_code is not None:
            set_meta_test_event_code(session, tenant, req.test_event_code)
        return {"tenant_id": tenant.id, "meta_dataset_id": tenant.meta_dataset_id,
                "meta_capi_token": "configured (Fernet-encrypted)",
                "test_event_code_set": tenant.meta_test_event_code is not None}


@router.post("/tenants/{tenant_id}/courier-secrets", status_code=201)
def generate_courier_secrets(tenant_id: int, req: CourierSecretsRequest, _op: None = Depends(_operator),
                             factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        tenant = _tenant(session, tenant_id)
        couriers = list(dict.fromkeys(req.couriers))
        existing = _kinds(session, tenant.id)
        clash = [c for c in couriers if COURIER_SECRET_KINDS[c] in existing]
        if clash and not req.rotate:
            raise HTTPException(status_code=409,
                                detail=f"Secret already exists for {clash}; pass rotate=true to replace it")
        ensure_fernet_key()
        # token_urlsafe(32) = 43 chars, comfortably above BOSTA_MIN_SECRET_LENGTH.
        values: Dict[str, str] = {c: secrets.token_urlsafe(32) for c in couriers}
        store_courier_secrets(session, tenant.id, tenant.shop_domain,
                              bosta_secret=values.get("bosta"), oto_secret=values.get("oto"))
        out = {c: {"url": f"{_base()}/webhooks/{c}/{tenant.shop_domain}", "secret": v} for c, v in values.items()}
        return {"tenant_id": tenant.id, "secrets": out,
                "note": "Shown once; store them now and configure each in the courier dashboard."}


@router.post("/tenants/{tenant_id}/api-key", status_code=201)
def issue_first_key(tenant_id: int, _op: None = Depends(_operator),
                    factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        tenant = _tenant(session, tenant_id)
        if _active_key_exists(session, tenant.id):
            raise HTTPException(status_code=409,
                                detail="Tenant already has an active API key; use the api-keys rotate endpoint")
        row, full_key = create_tenant_key(session, tenant.id, "initial")
        return {"tenant_id": tenant.id, "key_id": row.id, "key_prefix": row.key_prefix, "scopes": row.scopes,
                "api_key": full_key}


def _step(key: str, label: str, done: bool, required: bool = True, detail: str = "", manual: bool = False) -> dict:
    status = "done" if done else ("manual" if manual else "missing")
    return {"key": key, "label": label, "required": required, "manual": manual, "status": status, "detail": detail}


def _courier_secret_ok(session: Session, tenant_id: int, courier: str, kinds: set) -> bool:
    kind = COURIER_SECRET_KINDS[courier]
    if kind not in kinds:
        return False
    if courier != "bosta":
        return True
    try:
        value = get_credential(session, tenant_id, kind)
    except CredentialError:
        return False
    return bool(value) and len(value.strip()) >= BOSTA_MIN_SECRET_LENGTH


@router.get("/tenants/{tenant_id}/checklist")
def checklist(tenant_id: int, _op: None = Depends(_operator),
              factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        tenant = _tenant(session, tenant_id)
        kinds = _kinds(session, tenant.id)
        bosta = _courier_secret_ok(session, tenant.id, "bosta", kinds)
        oto = _courier_secret_ok(session, tenant.id, "oto", kinds)
        if tenant.platform == "shopify":
            urls = [f"{_base()}/webhooks/shopify/{t}" for t in ("orders/create", "orders/updated", "orders/paid")]
        else:
            urls = [f"{_base()}/webhooks/salla"]
        steps = [
            _step("tenant_created", "Tenant created", True),
            _step("meta_dataset", "Meta dataset id stored", bool(tenant.meta_dataset_id)),
            _step("meta_capi_token", "Meta CAPI token stored", "meta_capi_token" in kinds),
            _step("courier_secret", "At least one courier webhook secret (Bosta or OTO)", bosta or oto,
                  detail=f"bosta={'ok' if bosta else 'missing'}, oto={'ok' if oto else 'missing'}"),
            _step("api_key", "Tenant API key issued", _active_key_exists(session, tenant.id)),
            _step("platform_webhooks", f"Register {tenant.platform} webhooks (MANUAL)", False, required=False,
                  detail="; ".join(urls), manual=True),
            _step("test_event_code", "Meta test_event_code set (optional)", tenant.meta_test_event_code is not None,
                  required=False),
        ]
        ready = all(s["status"] == "done" for s in steps if s["required"])
        return {"tenant_id": tenant.id, "mode": tenant.mode, "steps": steps, "platform_webhook_urls": urls,
                "ready_for_live": ready,
                "note": "Switching to live is a separate operator action; this API never changes mode."}
