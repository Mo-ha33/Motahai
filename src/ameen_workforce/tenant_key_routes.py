"""
tenant_key_routes.py — operator endpoints to manage per-tenant API keys (M3-3). All behind require_operator.

  POST /v1/operator/tenants/{tenant_id}/api-keys                  create (full key returned ONCE)
  GET  /v1/operator/tenants/{tenant_id}/api-keys                  list (prefix/label/scopes/timestamps; no hash, no key)
  POST /v1/operator/tenants/{tenant_id}/api-keys/{key_id}/rotate  revoke + create replacement (new key returned ONCE)
  POST /v1/operator/tenants/{tenant_id}/api-keys/{key_id}/revoke  revoke
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import Tenant, TenantApiKey
from .tenant_auth import DEFAULT_SCOPES, create_tenant_key, revoke_tenant_key
from .webhook_routes import get_session_factory_dep

router = APIRouter(prefix="/v1/operator/tenants/{tenant_id}/api-keys", tags=["tenant-api-keys"])


def _operator(authorization: Optional[str] = Header(None)) -> None:
    # Late import: service.py includes this router, so it cannot be imported at module load.
    from .service import require_operator
    require_operator(authorization)


class CreateKeyRequest(BaseModel):
    label: str = Field("", max_length=100)
    scopes: str = Field(DEFAULT_SCOPES, pattern=r"^[a-z_]+:[a-z_]+( [a-z_]+:[a-z_]+)*$", max_length=255)


class KeyInfo(BaseModel):
    id: int
    key_prefix: str
    label: str
    scopes: str
    created_at: Optional[str]
    last_used_at: Optional[str]
    revoked_at: Optional[str]


class CreatedKey(KeyInfo):
    api_key: str  # shown exactly once


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _info(row: TenantApiKey) -> dict:
    return dict(id=row.id, key_prefix=row.key_prefix, label=row.label, scopes=row.scopes,
                created_at=_iso(row.created_at), last_used_at=_iso(row.last_used_at), revoked_at=_iso(row.revoked_at))


def _require_tenant(session, tenant_id: int) -> None:
    if session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found")


def _get_key(session, tenant_id: int, key_id: int) -> TenantApiKey:
    row = session.get(TenantApiKey, key_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Key not found")
    return row


@router.post("", response_model=CreatedKey, status_code=201)
def create_key(tenant_id: int, req: CreateKeyRequest, _op: None = Depends(_operator),
               factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        _require_tenant(session, tenant_id)
        row, full_key = create_tenant_key(session, tenant_id, req.label, req.scopes)
        return dict(_info(row), api_key=full_key)


@router.get("", response_model=List[KeyInfo])
def list_keys(tenant_id: int, _op: None = Depends(_operator),
              factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        _require_tenant(session, tenant_id)
        rows = session.scalars(select(TenantApiKey).where(TenantApiKey.tenant_id == tenant_id)
                               .order_by(TenantApiKey.id)).all()
        return [_info(r) for r in rows]


@router.post("/{key_id}/rotate", response_model=CreatedKey, status_code=201)
def rotate_key(tenant_id: int, key_id: int, _op: None = Depends(_operator),
               factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        old = _get_key(session, tenant_id, key_id)
        revoke_tenant_key(session, old.id)
        row, full_key = create_tenant_key(session, tenant_id, old.label, old.scopes)
        return dict(_info(row), api_key=full_key)


@router.post("/{key_id}/revoke", response_model=KeyInfo)
def revoke_key(tenant_id: int, key_id: int, _op: None = Depends(_operator),
               factory: sessionmaker = Depends(get_session_factory_dep)):
    with factory() as session:
        row = _get_key(session, tenant_id, key_id)
        revoke_tenant_key(session, row.id)
        return _info(row)
