---
name: gtm-container-linter
description: >
  Deterministic static audit of a Google Tag Manager web container (export JSON or
  live version via API). Finds dead/paused/duplicate tags, orphan triggers, unused or
  undefined variables, duplicate Google tags, Meta Pixel calls without eventID,
  GA4 purchase tags without transaction_id/currency, Consent Mode v2 gaps, leaked
  secrets in Custom HTML, and size bloat. Returns a JSON report with a 0-100 health
  score and a config fingerprint for drift detection. Use BEFORE any GTM verdict,
  fix, or "container is clean" claim.
version: 1.0.0
author: Wesam GTM Swarm / Hermes
tags: [gtm, audit, ga4, meta, consent, drift]
---

# gtm-container-linter

## Why this skill exists

LLMs are unreliable at counting and cross-referencing hundreds of IDs inside a GTM
export. This skill does that part **deterministically** (pure Python, stdlib only,
no network, no LLM). Hermes and the Wesam agents then reason over the *report*,
not over the raw JSON. Rule: **no audit verdict without a linter report attached.**

## Inputs

One of:

| Mode | How | When |
|---|---|---|
| Live via API (preferred) | `scripts/fetch_live_version.py <account_id> <container_id> -o /tmp/c.json` | Hermes holds a read-only GTM service account |
| File | Path to a GTM UI export (`Admin → Export Container`) | Client sent an export / workspace not yet published |
| stdin | `-` | Piping from another tool |

Container `100000001` is a numeric **container ID** — the API also needs the numeric
**account ID**. `GTM-XXXXXXX` is the public ID and cannot be used with the API.

## Run

```bash
cd ~/.hermes/skills/gtm-container-linter
# 1) fetch (read-only scope)
GTM_SA_KEY_FILE=~/.hermes/secrets/gtm-readonly.json \
  python3 scripts/fetch_live_version.py <ACCOUNT_ID> 100000001 -o /tmp/gtm-100000001.json
# 2) lint
python3 scripts/lint_container.py /tmp/gtm-100000001.json --format json --max-findings 80
#    --format md       human-readable table
#    --fail-on high    exit 1 if any finding >= high (default); 'never' to always exit 0
```

Exit codes: `0` clean at threshold · `1` findings at/above `--fail-on` · `2` bad input.

## Output contract (JSON)

```json
{
  "linter": "gtm-container-linter", "linter_version": "1.0.0",
  "container": {"container_id": "100000001", "public_id": "GTM-XXXX", "version_id": "42", "gtm_fingerprint": "..."},
  "config_fingerprint": "<sha256 of functional config; ignores names/ids/notes/order>",
  "inventory": {"tags": 0, "triggers": 0, "variables": 0, "custom_templates": 0},
  "health_score": 0,
  "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
  "findings_total": 0, "findings_truncated": false,
  "findings": [{
    "rule": "meta.pixel_no_event_id", "severity": "high",
    "entity_type": "tag", "entity_id": "4", "entity_name": "Meta Pixel - Purchase",
    "message": "...", "fix": "...", "evidence": {"events": ["Purchase"]}
  }]
}
```

Secrets found in Custom HTML are **redacted** in `evidence` — never echo them.

## Rule catalogue

| Rule | Sev | Meaning |
|---|---|---|
| `security.meta_token_in_html` | critical | Meta `EAA…` token shipped to browsers → revoke now |
| `security.secret_in_html` | critical | `access_token`/`api_secret`/`client_secret` literal in Custom HTML |
| `tag.exact_duplicate` | high | Identical config + triggers → every hit sent N× |
| `google.duplicate_google_tag` | high | Same `G-`/`AW-` ID configured by >1 Google tag/config tag |
| `ga4.purchase_no_transaction_id` | high | GA4 purchase/refund without ecommerce data or transaction_id |
| `meta.pixel_no_event_id` | high | `fbq('track','Purchase'…)` without `eventID` → CAPI dedup impossible |
| `tag.missing_trigger` | high | Firing/blocking trigger ID not in the version |
| `variable.undefined_reference` | high | `{{X}}` not defined/enabled (case-sensitive) |
| `html.insecure_src` | high | `http://` resource → mixed-content blocked |
| `consent.no_default_detected` | high | No Consent-Init tag / CMP detected (heuristic) |
| `consent.non_google_tag_unchecked` | medium | Custom HTML/Image/template tag with consent "Not set" |
| `ga4.purchase_no_currency` | medium | Value without currency → revenue dropped |
| `meta.duplicate_pixel_init` | medium | Same pixel `fbq('init')` in several tags |
| `tag.no_firing_trigger` | medium | Can never fire (sequencing-aware) |
| `tag.universal_analytics` | medium | UA tag — dead since July 2024 |
| `html.document_write` / `html.eval` | medium | Fragile / CSP-hostile Custom HTML |
| `container.size` | medium/high | Export > 150 KB (proxy for the 200 KB limit) |
| `tag.paused`, `trigger.unused`, `trigger.duplicate`, `variable.unused` | low | Bloat |

Health score = `100 − Σ penalty` (critical 25, high 10, medium 4, low 1), floored at 0.

## Known limits (state these in any verdict that depends on them)

- **Static only.** It cannot see whether the dataLayer actually pushes the values. Pair
  with `datalayer-schema-validator` (live capture) before declaring tracking "correct".
- `consent.no_default_detected` is a heuristic (looks for Consent-Init trigger / CMP-like
  names). A consent default set in the site's `<head>` before GTM is valid and will be a
  false positive — confirm with a browser snapshot.
- Custom templates (`cvt_*`) are treated as opaque; Meta/TikTok template tags are not
  checked for `event_id` mapping.
- Variables read only from inside custom-template sandboxed code may be reported unused.

## Drift detection (Sentinel)

Store `config_fingerprint` + `container.version_id` in the client ledger
(`memories/client-ledger.md`). On each scan: same fingerprint → no drift; different
fingerprint with same version_id → impossible (re-fetch); new version_id → re-lint and
diff `counts` against the previous run; any new `critical`/`high` → open incident.

## Tests

```bash
python3 -m pytest tests -q
```
