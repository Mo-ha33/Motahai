"""
tenant_me_routes.py — the signed-in tenant's own profile, for the portal sign-in (M4-2 #49).

  GET /v1/tenant/me   -> {tenant_id, name, currency, country, mode, platform}

Authenticated ONLY by a tenant API key with the `stats:read` scope (the operator key is 401: the operator has no tenant).
Every failure is the uniform tenant-auth 401. Returns no secrets and nothing about the key itself.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .db import Tenant
from .tenant_auth import require_tenant_key

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])


@router.get("/me")
def tenant_me(tenant: Tenant = Depends(require_tenant_key("stats:read"))) -> Dict[str, Any]:
    return {"tenant_id": tenant.id, "name": tenant.name, "currency": tenant.currency, "country": tenant.country,
            "mode": tenant.mode, "platform": tenant.platform}
