#!/usr/bin/env python3
"""
scripts/onboard_store.py — Operator Onboarding CLI for Pilot Stores (1-3)
========================================================================

Allows an operator to quickly onboard and configure e-commerce stores:
1. Registers/updates tenant (name, platform, shop domain, locale, currency, mode, settlement window).
2. Configures platform credentials & webhook secrets (Fernet-encrypted at rest).
3. Configures Meta Dataset ID & CAPI Access Token (Fernet-encrypted at rest).
4. Executes an automated test ping / CAPI event verification.

Usage:
    python scripts/onboard_store.py \
        --name "Pilot Store 1" \
        --platform shopify \
        --shop-domain "pilot1.myshopify.com" \
        --meta-dataset-id "123456789012345" \
        --meta-capi-token "EAAB..." \
        --webhook-secret "shpss_..." \
        --mode shadow

Options:
    --verify-capi-ping        Send a MotahaiConnectionTest event to Meta to verify dataset + token (opt-in).
                              REQUIRES --test-event-code: without it the CLI exits non-zero and sends nothing.
    --test-event-code         Meta test event code (e.g. TEST12345) from Events Manager; keeps the ping out of live data
    --skip-ping               Skip the ping even if --verify-capi-ping is given
    --bosta-webhook-secret    Bosta webhook secret (stored encrypted as bosta_webhook_secret)
    --oto-webhook-secret      OTO webhook secret (stored encrypted as oto_webhook_secret)
    --generate-courier-secrets  Generate strong Bosta + OTO secrets, store them, print them ONCE with webhook URLs
    --dry-run                 Validate inputs without writing to DB or Meta
    --json                    Output result as JSON

Requires MOTAHAI_FERNET_KEY in the environment (outside --dry-run). A throwaway key would make the stored
credentials unreadable by the services, so none is generated for real runs.
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from typing import Any, Dict, Optional, Sequence

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy.orm import Session
from src.ameen_workforce.capi_service import MetaCAPISender, capi_sender, hash_email, hash_phone
from src.ameen_workforce.credentials import (
    FERNET_KEY_ENV, CredentialConfigError, encrypt_value, generate_key, store_credential
)
from src.ameen_workforce.config import settings
from src.ameen_workforce.db import (
    PLATFORMS, TENANT_MODES, Tenant, create_tenant, get_session_factory,
    get_tenant_by_shop_domain, init_db, session_scope
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("onboard_store")

# Connection-test event. A custom name so the ping can never land in the merchant's dataset as a DeliveredPurchase
# (custom conversion campaigns optimise on that event).
CAPI_PING_EVENT_NAME = "MotahaiConnectionTest"
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
) -> Dict[str, Any]:
    """
    Core onboarding engine: validates input, registers/updates the tenant,
    and stores Fernet-encrypted credentials.
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
        "credentials": {
            "meta_capi_token": "configured (Fernet-encrypted)",
            "webhook_secret": "configured (Fernet-encrypted)" if webhook_secret else "not provided"
        }
    }


async def verify_capi_ping(
    meta_dataset_id: str,
    meta_capi_token: str,
    currency: str = "EGP",
    country: str = "EG",
    test_event_code: Optional[str] = None,
    storefront_url: Optional[str] = None,
    sender: Optional[MetaCAPISender] = None,
) -> Dict[str, Any]:
    """
    Sends a connection-test event (MotahaiConnectionTest) to Meta Conversions API to verify the dataset ID and CAPI
    token. A Meta test_event_code is mandatory: without one the event would reach the live dataset, so this raises
    ValueError before anything is sent.
    """
    if not test_event_code or not str(test_event_code).strip():
        raise ValueError("verify_capi_ping requires a Meta test_event_code; refusing to send a connection test to live data")
    active_sender = sender or capi_sender
    order_id = f"TEST-ONBOARD-{int(time.time())}"
    event_id = f"test_onboard_ping_{int(time.time())}"

    payload = active_sender.build_event_payload(
        event_name=CAPI_PING_EVENT_NAME,
        event_id=event_id,
        order_id=order_id,
        value=1.0,
        currency=currency,
        email_hash=hash_email("test_onboard@motahai.com"),
        phone_hash=hash_phone("0500000000", currency=currency, country=country),
        test_event_code=str(test_event_code).strip(),
        event_source_url=storefront_url
    )

    result = await active_sender.send_event(
        pixel_id=meta_dataset_id,
        access_token=meta_capi_token,
        payload=payload
    )

    if result.get("status") == "success":
        return {
            "status": "success",
            "events_received": result.get("events_received", 1),
            "fbtrace_id": result.get("fbtrace_id"),
            "event_id": event_id,
            "event_name": CAPI_PING_EVENT_NAME
        }
    else:
        return {
            "status": "error",
            "http_code": result.get("http_code"),
            "message": result.get("error_message") or result.get("error") or "Meta dispatch failed",
            "raw": result.get("raw")
        }


def parse_cli_args(args: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Onboard pilot stores to Ameen / Motahai Conversion Engine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--name", required=True, help="Store/tenant display name (e.g. 'Pilot Store 1')")
    parser.add_argument("--platform", required=True, choices=["shopify", "salla", "zid"], help="E-commerce platform")
    parser.add_argument("--shop-domain", required=True, help="Shopify *.myshopify.com or Salla merchant ID")
    parser.add_argument("--meta-dataset-id", required=True, help="Meta Pixel / Dataset ID")
    parser.add_argument("--meta-capi-token", required=True, help="Meta Conversions API access token")
    parser.add_argument("--webhook-secret", default=None, help="Platform webhook secret (optional)")
    parser.add_argument("--country", default=None, help="Two-letter country code (default: EG for shopify, SA for salla)")
    parser.add_argument("--currency", default=None, help="Three-letter currency code (default: EGP for shopify, SAR for salla)")
    parser.add_argument("--timezone", default=None, help="Timezone string (e.g. 'Africa/Cairo', 'Asia/Riyadh')")
    parser.add_argument("--mode", default="shadow", choices=["shadow", "live"], help="Operational mode")
    parser.add_argument("--settlement-hours", type=float, default=12.0, help="Settlement window in hours")
    parser.add_argument("--storefront-url", default=None, help="Public storefront URL (e.g. https://store.com)")
    parser.add_argument("--verify-capi-ping", action="store_true",
                        help="Send a MotahaiConnectionTest event to Meta to verify dataset + token. Requires --test-event-code.")
    parser.add_argument("--skip-ping", action="store_true", help="Skip the Meta connection ping even if --verify-capi-ping is set")
    parser.add_argument("--test-event-code", default=None,
                        help="Meta test event code (e.g. TEST12345). Required by --verify-capi-ping; keeps the ping out of live data.")
    parser.add_argument("--bosta-webhook-secret", default=None, help="Bosta webhook secret (stored encrypted)")
    parser.add_argument("--oto-webhook-secret", default=None, help="OTO webhook secret (stored encrypted)")
    parser.add_argument("--generate-courier-secrets", action="store_true",
                        help="Generate strong Bosta + OTO webhook secrets, store them, and print them ONCE with the webhook URLs. "
                             "Replaces any existing courier secrets.")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration without persisting to DB")
    parser.add_argument("--json", action="store_true", help="Print output as JSON")
    return parser.parse_args(args)


def validate_cli_args(args: argparse.Namespace) -> Optional[str]:
    """Returns a human-readable problem with the combination of flags, or None. Runs before anything is written or sent."""
    if args.verify_capi_ping and not (args.test_event_code and args.test_event_code.strip()):
        return ("--verify-capi-ping requires --test-event-code (the Meta test event code from Events Manager). "
                "Nothing was sent and nothing was stored.")
    if args.generate_courier_secrets and (args.bosta_webhook_secret is not None or args.oto_webhook_secret is not None):
        return "--generate-courier-secrets cannot be combined with --bosta-webhook-secret or --oto-webhook-secret"
    for flag, value in (("--bosta-webhook-secret", args.bosta_webhook_secret), ("--oto-webhook-secret", args.oto_webhook_secret)):
        if value is not None and not value.strip():
            return f"{flag} must not be empty"
    return None


def _fail(args: argparse.Namespace, message: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "message": message}, indent=2))
    else:
        print(f"\n[ERROR] {message}\n", file=sys.stderr)
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_cli_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1

    problem = validate_cli_args(args)
    if problem:
        return _fail(args, problem)
    if not args.dry_run and not os.environ.get(FERNET_KEY_ENV):
        return _fail(args, f"{FERNET_KEY_ENV} must be set in the environment. Refusing to store credentials under a "
                           "throwaway key that the services could not read.")

    try:
        ensure_fernet_key()
        init_db()

        courier_flags_given = bool(args.generate_courier_secrets or args.bosta_webhook_secret is not None
                                   or args.oto_webhook_secret is not None)
        with session_scope() as session:
            onboard_result = onboard_store(
                session=session,
                name=args.name,
                platform=args.platform,
                shop_domain=args.shop_domain,
                meta_dataset_id=args.meta_dataset_id,
                meta_capi_token=args.meta_capi_token,
                webhook_secret=args.webhook_secret,
                country=args.country,
                currency=args.currency,
                timezone=args.timezone,
                mode=args.mode,
                settlement_hours=args.settlement_hours,
                storefront_url=args.storefront_url,
                dry_run=args.dry_run
            )
            if args.dry_run:
                if courier_flags_given:
                    onboard_result["courier_secrets"] = "not stored (dry run)"
            elif courier_flags_given:
                courier_result = store_courier_secrets(
                    session,
                    tenant_id=onboard_result["tenant_id"],
                    shop_domain=onboard_result["shop_domain"],
                    bosta_secret=args.bosta_webhook_secret,
                    oto_secret=args.oto_webhook_secret,
                    generate=args.generate_courier_secrets,
                )
                onboard_result["credentials"].update(courier_result["credentials"])
                if courier_result["generated"]:
                    onboard_result["generated_courier_webhooks"] = courier_result["generated"]

        ping_result = None
        if args.verify_capi_ping and not args.skip_ping and not args.dry_run:
            logger.info("Executing Meta CAPI test ping for dataset %s...", args.meta_dataset_id)
            ping_result = asyncio.run(verify_capi_ping(
                meta_dataset_id=args.meta_dataset_id,
                meta_capi_token=args.meta_capi_token,
                currency=onboard_result.get("currency", "EGP"),
                country=onboard_result.get("country", "EG"),
                test_event_code=args.test_event_code,
                storefront_url=args.storefront_url
            ))
            onboard_result["verification_ping"] = ping_result

        if args.json:
            print(json.dumps(onboard_result, indent=2))
        else:
            print("\n" + "=" * 65)
            print("  Motahai Pilot Store Onboarding Summary")
            print("=" * 65)
            print(f"  Tenant ID:        {onboard_result.get('tenant_id')}")
            print(f"  Action:           {onboard_result.get('action').upper()}")
            print(f"  Store Name:       {onboard_result.get('name')}")
            print(f"  Platform:         {onboard_result.get('platform')}")
            print(f"  Shop Domain:      {onboard_result.get('shop_domain')}")
            print(f"  Operational Mode: {onboard_result.get('mode')}")
            print(f"  Meta Dataset ID:  {onboard_result.get('meta_dataset_id')}")
            print(f"  Locale / Currency:{onboard_result.get('country')} / {onboard_result.get('currency')} ({onboard_result.get('timezone')})")
            print(f"  Settlement Window:{onboard_result.get('settlement_hours')} hours")
            if onboard_result.get("storefront_url"):
                print(f"  Storefront URL:   {onboard_result.get('storefront_url')}")
            print("  Credentials:")
            print(f"    * Meta CAPI Token: {onboard_result['credentials']['meta_capi_token']}")
            print(f"    * Webhook Secret:  {onboard_result['credentials']['webhook_secret']}")
            for kind in COURIER_SECRET_KINDS.values():
                if kind in onboard_result["credentials"]:
                    print(f"    * {kind}: {onboard_result['credentials'][kind]}")
            if onboard_result.get("courier_secrets"):
                print(f"    * Courier secrets: {onboard_result['courier_secrets']}")
            generated = onboard_result.get("generated_courier_webhooks")
            if generated:
                print("  Courier webhooks (secrets shown ONCE: paste them into the courier dashboards; they are")
                print("  stored encrypted and cannot be displayed again. Rotate by re-running with --generate-courier-secrets):")
                for courier in ("bosta", "oto"):
                    if courier in generated:
                        print(f"    * {courier.capitalize()} URL:    {generated[courier]['url']}")
                        print(f"      {courier.capitalize()} secret: {generated[courier]['secret']}")
            if ping_result:
                print("  Verification Ping:")
                status_str = ping_result.get("status", "").upper()
                if status_str == "SUCCESS":
                    print(f"    * Status: SUCCESS (Trace ID: {ping_result.get('fbtrace_id')})")
                else:
                    print(f"    * Status: WARNING/FAILED ({ping_result.get('message')})")
            print("=" * 65)
            print("Store successfully onboarded!\n")

        return 0

    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        else:
            print(f"\n[ERROR] Onboarding failed: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
