#!/usr/bin/env python3
"""
Fetch the LIVE (published) version of a GTM container via Tag Manager API v2.

Read-only by design: requests only the `tagmanager.readonly` scope. The service
account e-mail must be added to the GTM account with "Read" permission.

Env:
  GTM_SA_KEY_FILE   path to the service-account JSON key (chmod 600, never in git)

Usage:
  fetch_live_version.py <account_id> <container_id> [-o out.json]

Requires: pip install google-auth requests
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
API = "https://tagmanager.googleapis.com/tagmanager/v2/accounts/{a}/containers/{c}/versions:live"
NUMERIC = re.compile(r"^\d{1,20}$")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch live GTM container version (read-only).")
    ap.add_argument("account_id")
    ap.add_argument("container_id", help="Numeric container ID (e.g. 100000001), not the GTM-XXXX public ID")
    ap.add_argument("-o", "--out", help="Write JSON here instead of stdout")
    args = ap.parse_args(argv)

    # IDs are interpolated into a URL — accept digits only.
    if not (NUMERIC.match(args.account_id) and NUMERIC.match(args.container_id)):
        sys.stderr.write("account_id and container_id must be numeric\n")
        return 2

    key_file = os.environ.get("GTM_SA_KEY_FILE")
    if not key_file or not Path(key_file).is_file():
        sys.stderr.write("GTM_SA_KEY_FILE is not set or does not point to a file\n")
        return 2

    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(key_file, scopes=[SCOPE])
    resp = AuthorizedSession(creds).get(API.format(a=args.account_id, c=args.container_id), timeout=30)
    if resp.status_code != 200:
        sys.stderr.write(f"GTM API error {resp.status_code}: {resp.text[:500]}\n")
        return 1

    body = json.dumps(resp.json(), indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
