"""
onboarding.py — shared onboarding logic (CLI scripts/onboard_store.py and the operator onboarding API).
Secrets are Fernet-encrypted via credentials.py and never logged here.
"""

import logging
import os
import secrets
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .config import settings
from .credentials import FERNET_KEY_ENV, encrypt_value, generate_key, store_credential
from .db import (
    PLATFORMS, TENANT_MODES, create_tenant, get_tenant_by_shop_domain, normalize_test_event_code,
    set_meta_test_event_code
)

logger = logging.getLogger("onboarding")

# Courier webhook secret kinds; the courier webhook routes read exactly these credential kinds.
COURIER_SECRET_KINDS = {"bosta": "bosta_webhook_secret", "oto": "oto_webhook_secret"}


def store_courier_secrets(
    session: Session,
    tenant_id: int,
    shop_domain: str,
    bosta_secret: Optional[str] = None,
    oto_secret: Optional[str] = None,
    generate: bool = False,
) -> Dict[str, Any]:
    """
    Stores courier webhook secrets (Fernet-encrypted) for one tenant. With generate=True, strong random secrets are
    created (secrets.token_urlsafe(32)) and returned in "generated" so the caller can print them ONCE. Secrets are
    never logged here.
    """
    if generate:
        bosta_secret = secrets.token_urlsafe(32)
        oto_secret = secrets.token_urlsafe(32)
    base_url = settings.EMPLOYEES_PORTAL.rstrip("/")
    status: Dict[str, str] = {}
    generated: Dict[str, Dict[str, str]] = {}
    for courier, value in (("bosta", bosta_secret), ("oto", oto_secret)):
        if value is None:
            continue
        kind = COURIER_SECRET_KINDS[courier]
        store_credential(session, tenant_id, kind, value.strip())
        status[kind] = "configured (Fernet-encrypted)"
        if generate:
            generated[courier] = {"url": f"{base_url}/webhooks/{courier}/{shop_domain}", "secret": value}
    return {"credentials": status, "generated": generated}


def ensure_fernet_key() -> str:
    """Ensures MOTAHAI_FERNET_KEY is set; auto-provisions one in env if missing."""
    key = os.environ.get(FERNET_KEY_ENV)
    if not key:
        key = generate_key()
        os.environ[FERNET_KEY_ENV] = key
        logger.info("Provisioned temporary %s for this session", FERNET_KEY_ENV)
    return key


def onboard_store(
    session: Session,
    name: str,
    platform: str,
    shop_domain: str,
    meta_dataset_id: str,
    meta_capi_token: str,
    webhook_secret: Optional[str] = None,
    country: Optional[str] = None,
    currency: Optional[str] = None,
    timezone: Optional[str] = None,
    mode: str = "shadow",
    settlement_hours: Optional[float] = 12.0,
    storefront_url: Optional[str] = None,
    dry_run: bool = False,
    pipeline_test_event_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core onboarding engine: validates input, registers/updates the tenant,
    and stores Fernet-encrypted credentials.
    pipeline_test_event_code: None leaves the tenant's Meta test_event_code unchanged; "" clears it; any other value
    routes every pipeline CAPI send for this tenant to Events Manager's Test Events (db.set_meta_test_event_code).
    """
    platform_norm = platform.strip().lower()
    if platform_norm not in PLATFORMS:
        raise ValueError(f"Invalid platform: {platform}. Must be one of {PLATFORMS}")

    mode_norm = mode.strip().lower()
    if mode_norm not in TENANT_MODES:
        raise ValueError(f"Invalid mode: {mode}. Must be one of {TENANT_MODES}")

    domain_norm = shop_domain.strip().lower()
    if not domain_norm:
        raise ValueError("shop_domain cannot be empty")

    meta_dataset_id_norm = meta_dataset_id.strip()
    if not meta_dataset_id_norm:
        raise ValueError("meta_dataset_id cannot be empty")

    meta_capi_token_norm = meta_capi_token.strip()
    if not meta_capi_token_norm:
        raise ValueError("meta_capi_token cannot be empty")

    if settlement_hours is not None and settlement_hours < 0:
        raise ValueError("settlement_hours must be >= 0")

    storefront_url_clean = storefront_url.strip() if storefront_url else None
    if storefront_url_clean and not storefront_url_clean.lower().startswith(("http://", "https://")):
        raise ValueError("storefront_url must start with http:// or https://")

    if pipeline_test_event_code is not None:
        normalize_test_event_code(pipeline_test_event_code)  # validate before any write (also in dry runs)

    # Platform-specific sensible defaults
    if platform_norm == "salla":
        resolved_country = (country or "SA").strip().upper()
        resolved_currency = (currency or "SAR").strip().upper()
        resolved_timezone = timezone.strip() if timezone else "Asia/Riyadh"
    else:
        resolved_country = (country or "EG").strip().upper()
        resolved_currency = (currency or "EGP").strip().upper()
        resolved_timezone = timezone.strip() if timezone else "Africa/Cairo"

    # Verify Fernet encryption works
    ensure_fernet_key()

    # Check if tenant already exists
    existing = get_tenant_by_shop_domain(session, domain_norm)
    action = "updated" if existing else "created"

    if dry_run:
        return {
            "status": "dry_run",
            "action": action,
            "tenant_id": existing.id if existing else 0,
            "name": name.strip(),
            "platform": platform_norm,
            "shop_domain": domain_norm,
            "meta_dataset_id": meta_dataset_id_norm,
            "country": resolved_country,
            "currency": resolved_currency,
            "timezone": resolved_timezone,
            "mode": mode_norm,
            "settlement_hours": settlement_hours,
            "storefront_url": storefront_url_clean,
            "credentials": {
                "meta_capi_token": "valid (encryption verified)",
                "webhook_secret": "valid (encryption verified)" if webhook_secret else "not provided"
            }
        }

    if existing:
        tenant = existing
        tenant.name = name.strip()
        tenant.platform = platform_norm
        tenant.meta_dataset_id = meta_dataset_id_norm
        tenant.country = resolved_country
        tenant.currency = resolved_currency
        tenant.timezone = resolved_timezone
        tenant.mode = mode_norm
        tenant.settlement_hours = settlement_hours
        tenant.storefront_url = storefront_url_clean
        session.commit()
    else:
        tenant = create_tenant(
            session=session,
            name=name.strip(),
            platform=platform_norm,
            shop_domain=domain_norm,
            meta_dataset_id=meta_dataset_id_norm,
            country=resolved_country,
            currency=resolved_currency,
            timezone=resolved_timezone,
            mode=mode_norm,
            settlement_hours=settlement_hours,
            storefront_url=storefront_url_clean
        )

    # Store encrypted credentials
    store_credential(session, tenant.id, "meta_capi_token", meta_capi_token_norm)
    if webhook_secret and webhook_secret.strip():
        store_credential(session, tenant.id, "webhook_secret", webhook_secret.strip())
    if pipeline_test_event_code is not None:
        set_meta_test_event_code(session, tenant, pipeline_test_event_code)

    return {
        "status": "success",
        "action": action,
        "tenant_id": tenant.id,
        "name": tenant.name,
        "platform": tenant.platform,
        "shop_domain": tenant.shop_domain,
        "meta_dataset_id": tenant.meta_dataset_id,
        "country": tenant.country,
        "currency": tenant.currency,
        "timezone": tenant.timezone,
        "mode": tenant.mode,
        "settlement_hours": tenant.settlement_hours,
        "storefront_url": tenant.storefront_url,
        "pipeline_test_event_code": tenant.meta_test_event_code,
        "credentials": {
            "meta_capi_token": "configured (Fernet-encrypted)",
            "webhook_secret": "configured (Fernet-encrypted)" if webhook_secret else "not provided"
        }
    }
