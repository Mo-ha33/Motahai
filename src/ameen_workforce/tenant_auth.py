"""
tenant_auth.py — per-tenant scoped API keys (M3-3).

Key format: `mtk_<prefix>_<secret>`; prefix = 8 hex chars (lookup/display), secret = secrets.token_urlsafe(32)
(>= 32 bytes of entropy). Only SHA-256(full key) is stored; with a high-entropy random key a plain hash is sufficient
(no pepper/KDF needed). The plaintext key is returned once by create_tenant_key and is never stored or logged.

require_tenant_key(scope) is a FastAPI dependency returning the Tenant. Every failure (missing/malformed/unknown/wrong/
revoked/missing scope/inactive tenant) is the same 401 body, so responses are not an oracle for key existence.

require_operator_or_tenant_key(scope) accepts either credential on one route: a bearer token starting with `mtk_` is
authenticated as a tenant key (uniform 401 on failure, never falling back to the operator key); anything else goes
through auth.require_operator unchanged. It returns the Tenant, or None for the operator.
"""

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Callable, Optional, Tuple

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .auth import require_operator
from .db import Tenant, TenantApiKey, utcnow
from .webhook_routes import get_session_factory_dep

KEY_TAG = "mtk_"
PREFIX_LEN = 8
DEFAULT_SCOPES = "stats:read"


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})


def hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def create_tenant_key(session: Session, tenant_id: int, label: str = "", scopes: str = DEFAULT_SCOPES) -> Tuple[TenantApiKey, str]:
    """Create a key. Returns (row, full_key); full_key is the only copy of the plaintext."""
    while True:
        prefix = secrets.token_hex(PREFIX_LEN // 2)
        if session.scalar(select(TenantApiKey.id).where(TenantApiKey.key_prefix == prefix)) is None:
            break
    full_key = f"{KEY_TAG}{prefix}_{secrets.token_urlsafe(32)}"
    row = TenantApiKey(tenant_id=tenant_id, key_prefix=prefix, key_hash=hash_key(full_key),
                       label=label, scopes=scopes or DEFAULT_SCOPES)
    session.add(row)
    session.commit()
    return row, full_key


def revoke_tenant_key(session: Session, key_id: int, now: Optional[datetime] = None) -> Optional[TenantApiKey]:
    row = session.get(TenantApiKey, key_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = now or utcnow()
        session.commit()
    return row


def _parse_prefix(presented: str) -> Optional[str]:
    start = len(KEY_TAG)
    if not presented.startswith(KEY_TAG) or presented[start + PREFIX_LEN:start + PREFIX_LEN + 1] != "_":
        return None
    return presented[start:start + PREFIX_LEN]


def authenticate_key(session: Session, presented: str, scope: str) -> Optional[Tenant]:
    """Return the Tenant for a valid, unrevoked key holding `scope`, else None. Updates last_used_at on success."""
    prefix = _parse_prefix(presented)
    if prefix is None:
        return None
    row = session.scalar(select(TenantApiKey).where(TenantApiKey.key_prefix == prefix))
    # Always run the constant-time compare (against a dummy hash for an unknown prefix) to flatten timing.
    expected = row.key_hash if row is not None else "0" * 64
    matches = hmac.compare_digest(hash_key(presented).encode(), expected.encode())
    if row is None or not matches or row.revoked_at is not None:
        return None
    if scope not in row.scopes.replace(",", " ").split():
        return None
    tenant = session.get(Tenant, row.tenant_id)
    if tenant is None or not tenant.active:
        return None
    row.last_used_at = utcnow()
    session.commit()
    return tenant


def require_tenant_key(scope: str = DEFAULT_SCOPES) -> Callable[..., Tenant]:
    """Dependency factory: `tenant: Tenant = Depends(require_tenant_key("stats:read"))`."""
    def dependency(authorization: Optional[str] = Header(None),
                   factory: sessionmaker = Depends(get_session_factory_dep)) -> Tenant:
        presented = ""
        if authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if not presented:
            raise _unauthorized()
        with factory() as session:
            tenant = authenticate_key(session, presented, scope)
            if tenant is None:
                raise _unauthorized()
            session.expunge(tenant)
            return tenant
    return dependency


def ensure_tenant_matches(tenant: Tenant, url_tenant_id: int) -> None:
    """For routes with a tenant in the URL: a key may only act on its own tenant. Mismatch -> 404 (no existence leak)."""
    if tenant.id != url_tenant_id:
        raise HTTPException(status_code=404, detail="Not found")


def require_operator_or_tenant_key(scope: str = DEFAULT_SCOPES) -> Callable[..., Optional[Tenant]]:
    """Dependency factory: Tenant for a valid `mtk_` key holding `scope`; None for the operator key; else 401/503-as-before."""
    def dependency(authorization: Optional[str] = Header(None),
                   factory: sessionmaker = Depends(get_session_factory_dep)) -> Optional[Tenant]:
        presented = ""
        if authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if presented.startswith(KEY_TAG):
            with factory() as session:
                tenant = authenticate_key(session, presented, scope)
                if tenant is None:
                    raise _unauthorized()
                session.expunge(tenant)
                return tenant
        require_operator(authorization)
        return None
    return dependency
