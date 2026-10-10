"""
audit_bosta_secrets.py — Pre-deploy check for the Bosta minimum secret length (PR #23).

The Bosta webhook route now rejects any tenant whose stored Bosta secret is shorter than BOSTA_MIN_SECRET_LENGTH
(401, error_type secret_too_weak). Run this against the production database BEFORE deploying that change:

    DATABASE_URL=... MOTAHAI_FERNET_KEY=... python scripts/audit_bosta_secrets.py           # report only
    DATABASE_URL=... MOTAHAI_FERNET_KEY=... python scripts/audit_bosta_secrets.py --rotate 7  # new secret for tenant 7

Report mode is read-only. It prints one line per tenant that has a Bosta secret (tenant id, shop domain, active flag,
secret LENGTH and verdict), never the secret itself, and exits 1 when any tenant is weak or unreadable, 0 otherwise.

--rotate TENANT_ID [...] replaces the Bosta secret of those tenants with secrets.token_urlsafe(32) and prints each new
secret ONCE with its webhook URL. The merchant's Bosta dashboard must be updated with the new value at the same time:
until then Bosta keeps sending the old secret and its webhooks are rejected. Only the Bosta secret changes.
"""

import argparse
import os
import secrets
import sys
from typing import List, Optional, Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ameen_workforce.config import settings
from src.ameen_workforce.credentials import CredentialError, get_credential, store_credential
from src.ameen_workforce.db import Credential, Tenant, get_session_factory, session_scope
from src.ameen_workforce.webhook_signatures import BOSTA_MIN_SECRET_LENGTH

BOSTA_KIND = "bosta_webhook_secret"


def audit(session: Session) -> List[dict]:
    """One dict per tenant holding a Bosta secret: tenant_id, shop_domain, active, length (None if unreadable), verdict."""
    tenant_ids = session.scalars(select(Credential.tenant_id).where(Credential.kind == BOSTA_KIND)).all()
    results = []
    for tenant_id in sorted(set(tenant_ids)):
        tenant = session.get(Tenant, tenant_id)
        try:
            value = get_credential(session, tenant_id, BOSTA_KIND)
        except CredentialError:
            value, verdict = None, "unreadable"
        else:
            if value is None:
                verdict = "expired"
            else:
                verdict = "ok" if len(value.strip()) >= BOSTA_MIN_SECRET_LENGTH else "weak"
        results.append({
            "tenant_id": tenant_id, "shop_domain": tenant.shop_domain if tenant else "?",
            "active": bool(tenant.active) if tenant else False,
            "length": len(value.strip()) if value is not None else None, "verdict": verdict,
        })
    return results


def rotate(session: Session, tenant_id: int) -> dict:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise SystemExit(f"tenant {tenant_id} not found; nothing changed")
    secret = secrets.token_urlsafe(32)
    store_credential(session, tenant_id, BOSTA_KIND, secret)
    url = f"{settings.EMPLOYEES_PORTAL.rstrip('/')}/webhooks/bosta/{tenant.shop_domain}"
    return {"tenant_id": tenant_id, "url": url, "secret": secret}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rotate", type=int, nargs="+", metavar="TENANT_ID",
                        help="replace these tenants' Bosta secrets (prints each new secret once)")
    args = parser.parse_args(argv)

    factory = get_session_factory()
    if args.rotate:
        with session_scope(factory) as session:
            rotated = [rotate(session, tid) for tid in args.rotate]
        for r in rotated:
            print(f"tenant {r['tenant_id']}: set this Authorization value in Bosta now, then discard it")
            print(f"  url:    {r['url']}")
            print(f"  secret: {r['secret']}")
        return 0

    with session_scope(factory) as session:
        results = audit(session)
    if not results:
        print("No tenant has a Bosta secret: nothing to migrate.")
        return 0
    print(f"Minimum length: {BOSTA_MIN_SECRET_LENGTH}")
    for r in results:
        length = "-" if r["length"] is None else r["length"]
        print(f"tenant {r['tenant_id']:>5}  {r['shop_domain']:<40} active={r['active']!s:<5} "
              f"length={length!s:<4} {r['verdict'].upper()}")
    blocking = [r for r in results if r["verdict"] in ("weak", "unreadable")]
    print(f"{len(blocking)} tenant(s) need a new Bosta secret before deploy." if blocking else "All Bosta secrets pass.")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
