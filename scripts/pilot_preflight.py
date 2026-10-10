#!/usr/bin/env python3
"""
scripts/pilot_preflight.py — Read-only pre-flight checklist for a pilot tenant (docs/pilot/S2-7_validation_plan.md, section 1).

Usage:
    python scripts/pilot_preflight.py --tenant pilot1.myshopify.com [--db-url sqlite:///motahai.db]

Output: one line per item, `PASS|FAIL|WARN|MANUAL  <item>  <reason>`, then a SUMMARY line.
  FAIL   blocking. Any FAIL makes the exit code 1.
  WARN   non-blocking; worth reading before the pilot starts.
  PASS   checked and fine.
  MANUAL cannot be checked from the database or config (Events Manager, Meta UI, courier or Shopify screens).

Read-only: the script only SELECTs. For a SQLite file it opens the file with mode=ro (it never creates one) and it
makes no external API calls. Secrets (token, webhook secrets) are checked for presence and decryptability, never printed.
For production, point --db-url at a read-only database role as well.

Exit codes: 0 no blocking FAIL, 1 a blocking FAIL (including unknown tenant or unreadable database), 2 bad arguments.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import func, select
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src.ameen_workforce.capi_service import EVENT_TYPES, hash_phone, hash_sha256
from src.ameen_workforce.credentials import CredentialError, decrypt_value
from src.ameen_workforce.db import (
    DEFAULT_DATABASE_URL, DATABASE_URL_ENV, CapiEvent, Credential, Order, Tenant, WebhookDelivery,
    get_tenant_by_shop_domain, make_engine, utcnow
)
from src.ameen_workforce.order_pipeline import META_CAPI_TOKEN_KIND
from src.ameen_workforce.webhook_routes import BOSTA_SECRET_KIND, OTO_SECRET_KIND, SALLA_SECRET_ENV, SHOPIFY_SECRET_ENV

PASS, FAIL, WARN, MANUAL = "PASS", "FAIL", "WARN", "MANUAL"
WEBHOOK_WINDOW = timedelta(hours=72)
# Order placed + 6.5 days is the send cutoff (capi_service.LATE_DELIVERY_CUTOFF); past it a pending event is stale.
LATE_CUTOFF = timedelta(days=6.5)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
META_SAMPLE_PHONE = "15559876543"  # Meta's documented hashing sample
META_SAMPLE_HASH = "1ef970831d7963307784fa8688e8fce101a15685d62aa765fed23f3a2c576a4e"

# Items that need Events Manager, the Meta UI, or a real account. Printed as MANUAL on every run.
MANUAL_ITEMS: Tuple[Tuple[str, str], ...] = (
    ("1.1 user agent accepted", "Events Manager diagnostics: do events with missing_user_agent get accepted or discarded? (Q5)"),
    ("1.2 test events sent", "send ConfirmedOrder and DeliveredPurchase with test_event_code; confirm name, value, currency, matches"),
    ("1.2 real test account", "Test Events discards events that match no Meta account; use a real phone/email for one event per type"),
    ("1.2 old-event rejection", "sandbox request with one event older than 7 days; confirm the whole request is rejected"),
    ("1.3 match quality", "record Dataset Quality / Events Manager match view (EMQ is web-only); all test events matched"),
    ("1.3 attribution 3-6 days", "events with event_time 3 to 6 days back are accepted and attributed to a click in 7 days (Q5c)"),
    ("1.4 custom conversions", "create ConfirmedOrder and DeliveredPurchase (rule: event name equals), status Active, record category"),
    ("1.5 optimization options", "paused Sales/Website draft: both events selectable; 'Maximize value' and ROAS goal screenshots"),
    ("1.6 pixel parity", "only if a pixel also fires: same external_id, event_name and event_id on browser and server"),
)


@dataclass(frozen=True)
class Check:
    status: str
    item: str
    reason: str

    def line(self) -> str:
        return f"{self.status}  {self.item}  {self.reason}"


def _env_has(env, key: str) -> bool:
    return bool((env.get(key) or "").strip())


def _tenant_checks(session: Session, shop_domain: str) -> Tuple[Optional[Tenant], List[Check]]:
    tenant = get_tenant_by_shop_domain(session, shop_domain)
    if tenant is None:
        return None, [Check(FAIL, "1.0 tenant exists", f"no tenant with shop_domain {shop_domain.strip().lower()!r}")]
    checks = [Check(PASS, "1.0 tenant exists", f"id={tenant.id} platform={tenant.platform}")]
    if tenant.active:
        checks.append(Check(PASS, "1.0 tenant active", "active=true"))
    else:
        checks.append(Check(FAIL, "1.0 tenant active", "tenant is inactive; activate it before the pilot"))
    if tenant.mode == "shadow":
        checks.append(Check(PASS, "1.0 tenant in shadow mode", "mode=shadow (no Meta sends yet)"))
    else:
        checks.append(Check(FAIL, "1.0 tenant in shadow mode", f"mode={tenant.mode}; pre-flight expects shadow"))
    return tenant, checks


def _meta_checks(session: Session, tenant: Tenant, now: datetime) -> List[Check]:
    checks = []
    dataset = (tenant.meta_dataset_id or "").strip()
    if not dataset:
        checks.append(Check(FAIL, "1.1 meta dataset id", "meta_dataset_id is not set"))
    elif not dataset.isdigit():
        checks.append(Check(FAIL, "1.1 meta dataset id", f"{dataset!r} is not numeric"))
    else:
        checks.append(Check(PASS, "1.1 meta dataset id", dataset))

    row = session.scalar(select(Credential).where(Credential.tenant_id == tenant.id, Credential.kind == META_CAPI_TOKEN_KIND))
    if row is None:
        checks.append(Check(FAIL, "1.1 meta CAPI token", f"no {META_CAPI_TOKEN_KIND} credential stored"))
    elif row.expires_at is not None and row.expires_at <= now:
        checks.append(Check(FAIL, "1.1 meta CAPI token", "stored token is expired"))
    else:
        try:
            token = decrypt_value(row.ciphertext)
        except CredentialError as exc:  # message carries no secret material
            checks.append(Check(FAIL, "1.1 meta CAPI token", f"stored but not readable: {exc}"))
        else:
            if token.strip():
                checks.append(Check(PASS, "1.1 meta CAPI token", "stored and decryptable (value not shown)"))
            else:
                checks.append(Check(FAIL, "1.1 meta CAPI token", "stored token decrypts to an empty value"))

    code = tenant.meta_test_event_code
    if code:
        checks.append(Check(PASS, "1.2 test_event_code", f"set to {code} (clear before live)"))
    else:
        checks.append(Check(FAIL, "1.2 test_event_code", "not set; pilot test sends need the Events Manager test code"))
    return checks


def _secret_checks(session: Session, tenant: Tenant, env) -> List[Check]:
    checks = []
    if tenant.platform == "shopify":
        ok = _env_has(env, SHOPIFY_SECRET_ENV)
        checks.append(Check(PASS if ok else FAIL, "1.1 platform webhook secret (shopify)",
                            f"env {SHOPIFY_SECRET_ENV} {'set' if ok else 'missing'}"))
    elif tenant.platform == "salla":
        ok = _env_has(env, SALLA_SECRET_ENV)
        checks.append(Check(PASS if ok else FAIL, "1.1 platform webhook secret (salla)",
                            f"env {SALLA_SECRET_ENV} {'set' if ok else 'missing'}"))
    else:
        checks.append(Check(WARN, "1.1 platform webhook secret", f"no webhook route for platform {tenant.platform}"))
    for kind, label in ((BOSTA_SECRET_KIND, "bosta"), (OTO_SECRET_KIND, "oto")):
        present = session.scalar(select(Credential.id).where(Credential.tenant_id == tenant.id, Credential.kind == kind))
        if present is not None:
            checks.append(Check(PASS, f"1.1 courier webhook secret ({label})", "stored"))
        else:
            checks.append(Check(WARN, f"1.1 courier webhook secret ({label})",
                                f"not stored; needed only if the pilot courier is {label}"))
    return checks


def _webhook_checks(session: Session, tenant: Tenant, now: datetime) -> List[Check]:
    rows = session.scalars(select(WebhookDelivery).where(
        WebhookDelivery.tenant_id == tenant.id, WebhookDelivery.received_at >= now - WEBHOOK_WINDOW)).all()
    verified = [r for r in rows if r.signature_ok]
    rejected = [r for r in rows if r.signature_ok is False]
    checks = []
    if verified:
        latest = max(verified, key=lambda r: r.received_at)
        checks.append(Check(PASS, "1.1 recent webhook", f"{len(verified)} signed in last 72h, latest topic {latest.topic}"))
    else:
        checks.append(Check(WARN, "1.1 recent webhook", "no signed webhook in last 72h (expected once orders arrive)"))
    if rejected:
        checks.append(Check(WARN, "1.1 webhook signatures", f"{len(rejected)} rejected (bad signature) in last 72h"))
    return checks


def _hashing_check(tenant: Tenant) -> Check:
    want = hash_sha256("201001234567")
    variants = ["+20 100 123 4567", "0020 100 123 4567", "201001234567", "01001234567"]
    bad = [v for v in variants if hash_phone(v, country=tenant.country, currency=tenant.currency) != want]
    if bad:
        return Check(FAIL, "1.1 phone normalization", f"{len(bad)} of {len(variants)} EG variants do not hash to 201001234567")
    if hash_sha256(META_SAMPLE_PHONE) != META_SAMPLE_HASH:
        return Check(FAIL, "1.1 phone normalization", "SHA-256 of Meta's sample phone does not match the documented hash")
    return Check(PASS, "1.1 phone normalization", "4 EG variants hash to 201001234567; Meta sample hash matches")


def _order_checks(session: Session, tenant: Tenant, now: datetime) -> List[Check]:
    total = session.scalar(select(func.count(Order.id)).where(Order.tenant_id == tenant.id)) or 0
    if total == 0:
        return [Check(WARN, "1.1 order phone hashes", "no orders yet; hashes unverified")]
    phones = session.scalars(select(Order.phone_hash).where(Order.tenant_id == tenant.id)).all()
    stored = [p for p in phones if p is not None]
    invalid = [p for p in stored if not _SHA256_HEX.match(p)]
    checks = []
    if invalid:
        checks.append(Check(FAIL, "1.1 order phone hashes", f"{len(invalid)} of {len(stored)} are not 64-char lowercase hex"))
    elif stored:
        checks.append(Check(PASS, "1.1 order phone hashes", f"{len(stored)} orders, all 64-char hex"))
    else:
        checks.append(Check(WARN, "1.1 order phone hashes", "no order has a phone hash; ph match key unavailable"))
    missing = total - len(stored)
    if stored and missing:
        checks.append(Check(WARN, "1.1 phone coverage", f"{missing} of {total} orders have no phone hash"))
    return checks


def _event_checks(session: Session, tenant: Tenant, now: datetime) -> List[Check]:
    events = session.scalars(select(CapiEvent).where(CapiEvent.tenant_id == tenant.id)).all()
    if not events:
        return [Check(WARN, "1.1 CAPI events", "no CAPI events yet; payload checks not run")]
    checks = []
    names = {e.event_name for e in events}
    unexpected = sorted(names - set(EVENT_TYPES))
    counts = ", ".join(f"{n}={sum(1 for e in events if e.event_name == n)}" for n in sorted(names & set(EVENT_TYPES)))
    if unexpected:
        checks.append(Check(FAIL, "1.1 event_name", f"unexpected names: {', '.join(unexpected)}"))
    else:
        checks.append(Check(PASS, "1.1 event_name", f"exact ConfirmedOrder/DeliveredPurchase only ({counts})"))

    bad_ids = [e for e in events if e.event_name in EVENT_TYPES
               and e.event_id != EVENT_TYPES[e.event_name].event_id(e.order_id)]
    if bad_ids:
        checks.append(Check(FAIL, "1.1 event_id", f"{len(bad_ids)} events have an event_id that is not order-based"))
    else:
        checks.append(Check(PASS, "1.1 event_id", "every event_id is stable per order + event"))

    # Quality flags are written for both sent and shadow rows (order_pipeline._send).
    payloads = [e for e in events if e.status in ("sent", "shadow")]
    if not payloads:
        checks.append(Check(WARN, "1.1 payload quality flags", "no sent or shadow events yet; flags unverified"))
    else:
        flags = [set((e.quality_flags or "").split(",")) - {""} for e in payloads]
        no_url = sum("missing_event_source_url" in f for f in flags)
        no_ua = sum("missing_user_agent" in f for f in flags)
        tested = sum("test_event" in f for f in flags)
        if no_url:
            checks.append(Check(FAIL, "1.1 event_source_url", f"{no_url} of {len(payloads)} events lack event_source_url"))
        else:
            checks.append(Check(PASS, "1.1 event_source_url", f"present on all {len(payloads)} sent/shadow events"))
        if no_ua:
            checks.append(Check(WARN, "1.1 client_user_agent",
                                f"{no_ua} of {len(payloads)} events lack a user agent (see MANUAL 1.1)"))
        else:
            checks.append(Check(PASS, "1.1 client_user_agent", f"present on all {len(payloads)} sent/shadow events"))
        if tested:
            checks.append(Check(WARN, "1.2 test sends", f"{tested} events were sent with test_event_code; they used their idempotency claim"))

    cutoff = now - LATE_CUTOFF
    stale = session.scalars(
        select(CapiEvent).join(Order, Order.id == CapiEvent.order_id).where(
            CapiEvent.tenant_id == tenant.id, CapiEvent.status.in_(("pending", "scheduled")),
            Order.created_at_platform.isnot(None), Order.created_at_platform < cutoff)).all()
    if stale:
        checks.append(Check(WARN, "1.1 send cutoff", f"{len(stale)} pending/scheduled events are past placed + 6.5 days"))
    else:
        checks.append(Check(PASS, "1.1 send cutoff", "no pending/scheduled event past placed + 6.5 days"))
    return checks


def run_checks(session: Session, shop_domain: str, env=None, now: Optional[datetime] = None) -> List[Check]:
    """All automated checks for one tenant (plus the MANUAL list). Read-only."""
    env = os.environ if env is None else env
    now = now or utcnow()
    tenant, checks = _tenant_checks(session, shop_domain)
    if tenant is None:
        return checks + _manual_checks()
    checks += _meta_checks(session, tenant, now)
    checks += _secret_checks(session, tenant, env)
    checks += _webhook_checks(session, tenant, now)
    checks.append(_hashing_check(tenant))
    checks += _order_checks(session, tenant, now)
    checks += _event_checks(session, tenant, now)
    return checks + _manual_checks()


def _manual_checks() -> List[Check]:
    return [Check(MANUAL, item, reason) for item, reason in MANUAL_ITEMS]


def _readonly_url(url: str) -> str:
    """A file-backed SQLite URL is opened read-only (mode=ro), so a typo can never create an empty database."""
    if url.startswith("sqlite:///") and url not in ("sqlite://", "sqlite:///:memory:") and "mode=" not in url:
        path = url[len("sqlite:///"):]
        return f"sqlite:///file:{path}?mode=ro&uri=true"
    return url


def _redacted(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001  (a malformed URL must not echo itself)
        return "<database url>"


def _summary(shop_domain: str, checks: Sequence[Check]) -> Tuple[str, int]:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, FAIL, WARN, MANUAL)}
    result = "BLOCKED" if counts[FAIL] else "NO_BLOCKERS"
    line = (f"SUMMARY tenant={shop_domain.strip().lower()} pass={counts[PASS]} fail={counts[FAIL]} "
            f"warn={counts[WARN]} manual={counts[MANUAL]} result={result}")
    return line, (1 if counts[FAIL] else 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only pilot pre-flight checks (S2-7 section 1).")
    parser.add_argument("--tenant", required=True, help="tenant shop_domain (Shopify *.myshopify.com, or Salla merchant id)")
    parser.add_argument("--db-url", default=None,
                        help=f"SQLAlchemy URL (default: env {DATABASE_URL_ENV}, else {DEFAULT_DATABASE_URL})")
    args = parser.parse_args(argv)
    if not args.tenant.strip():
        parser.error("--tenant must not be blank")

    url = args.db_url or os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL
    try:
        engine = make_engine(_readonly_url(url))
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            checks = run_checks(session, args.tenant)
        engine.dispose()
    except SQLAlchemyError as exc:
        checks = [Check(FAIL, "1.0 database", f"cannot read {_redacted(url)}: {type(exc).__name__}")] + _manual_checks()

    for check in checks:
        print(check.line())
    summary, code = _summary(args.tenant, checks)
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
