# Hermes Custom Skills — Wesam GTM Swarm

Install path: `/home/deploy/.hermes/skills/<skill-name>/` (each has a `SKILL.md`,
optional `scripts/`, `tests/`). Design principle: **deterministic code does the
counting and parsing; the LLM does judgement on the structured result.** Every skill
returns JSON so Wesam agents can parse it without re-asking.

| # | Skill | Status | Primary consumers | Tier |
|---|---|---|---|---|
| 1 | `gtm-container-linter` | **Built + tested** (this repo) | Auditor, Orchestrator, Sentinel, Architect | read |
| 2 | `datalayer-schema-validator` | Spec below | Schema Validator, QA, Auto-Fix | read |
| 3 | `consent-mode-auditor` | Spec below | QA & Consent Inspector | read (browser) |
| 4 | `capi-dedup-debugger` | Spec below | Conversion/Server-Side Specialist | read |
| 5 | `shopify-pixel-patcher` | Spec below | Auto-Fix Engineer, Architect | generate (no publish) |

Build order: 1 → 2 → 4 → 3 → 5. Skills 2–4 produce the evidence that the SOUL's
"no verdict without evidence" rule demands; 5 only generates code and depends on 2 to
verify what it generates.

---

## 2. `datalayer-schema-validator`

**Purpose:** Validate captured dataLayer pushes / Shopify Web Pixel payloads against the
GA4 ecommerce contract (SOUL §3.2, patterns P5).

**Input:** JSON array of pushes. Captured by the caller via
`hermes_browser_evaluate` → `JSON.stringify(window.dataLayer)` after walking the funnel,
or pasted from a Shopify pixel `console.log`.

**Checks (all deterministic):**
- Event-specific required fields: `purchase`/`refund` → `transaction_id`, `currency`,
  `value`, `items[]`; `add_to_cart`/`view_item`/`begin_checkout` → `currency`, `value`, `items[]`.
- `currency` ∈ ISO 4217 list; KWD/BHD/OMR 3-decimals, others 2.
- `value`, `price`, `tax`, `shipping` are finite numbers (reject strings, `NaN`, negatives
  except refunds); detect Arabic-Indic digits / `٫` / `٬` / currency text leakage.
- `value` ≈ Σ(price × quantity) − discounts (tolerance configurable, default 1%) — flags
  subunit (×100) bugs and shipping-included mismatches.
- Each item has `item_id` or `item_name`; ≤ 200 items; `quantity` integer ≥ 1.
- `ecommerce: null` reset precedes each ecommerce push.
- Duplicate `transaction_id` within capture → double-fire.
- `event_id` present when the client ledger says CAPI is active; unique per push except
  deterministic `purchase_<order_id>`.

**Output:** `{ "events_checked": n, "pass": bool, "findings": [{event_index, event, rule, severity, path, got, expected}] }`

**Scripts:** `scripts/validate_datalayer.py` (stdlib; ISO 4217 table embedded).

---

## 3. `consent-mode-auditor`

**Purpose:** Prove (not assume) Consent Mode v2 behaviour on a live URL.

**Procedure (browser tools, read-only, client domain only):**
1. `hermes_browser_navigate` fresh context (no cookies) → URL.
2. `hermes_browser_evaluate`: read `window.dataLayer` before any interaction; find the
   first `['consent','default',…]` entry and its index vs the first tag event (`gtm.js`, `config`).
3. Record network requests to `google-analytics.com/g/collect`, `googleadservices`,
   `facebook.com/tr`, `analytics.tiktok.com` **before** choice → extract `gcs`/`gcd`.
4. Click "Reject all" (selector from client ledger) → reload → repeat capture.
5. Fresh context → "Accept all" → repeat capture.

**Assertions:**
- Default present for all 4 signals before the first tag (`ad_user_data`,
  `ad_personalization` missing ⇒ high).
- Pre-consent / rejected: no `facebook.com/tr`, no TikTok/Snap/Clarity hits; Google
  hits only as cookieless pings (`gcs=G100`-style denied state) if Advanced mode.
- After accept: `update` event present; hits carry granted state.
- No `_fbp`, `_ga`, `_gcl_*` cookies set while denied.

**Output:** `{ "url", "mode": "advanced|basic|none", "default_before_tags": bool, "states": {"pre": {...}, "rejected": {...}, "accepted": {...}}, "violations": [...] }`

**Limits:** geo-scoped defaults need a request from the right region; record the egress
country of the VPS in the report.

---

## 4. `capi-dedup-debugger`

**Purpose:** Diagnose Meta Pixel ↔ CAPI deduplication and event-match quality.

**Input:** a browser capture of `facebook.com/tr` requests (from the funnel walk) +
an sGTM/Stape log export (or CAPI request bodies, **token stripped by the caller**).

**Checks:**
- Pair browser and server events by `(event_name, event_id)`; report unpaired on each side.
- Event-name case mismatch (`Purchase` vs `purchase`).
- `event_time` drift > 1 h between pair members; server events > 7 days old.
- `event_id` regenerated per tag (same order, different IDs) ⇒ dedup broken.
- `user_data` coverage %: `em`, `ph`, `external_id`, `fbp`, `fbc`, IP, UA.
- Hash-format check: 64-char lowercase hex for hashed fields; plaintext email/phone ⇒ critical.
- Egypt phone normalisation (`20` + 10 digits, no `+`, no leading `0`).

**Output:** `{ "pairs": n, "unpaired_browser": n, "unpaired_server": n, "dedup_rate": 0.0, "match_key_coverage": {...}, "findings": [...] }`

**Never** accepts or stores an access token; rejects input containing `EAA…` strings.

---

## 5. `shopify-pixel-patcher`

**Purpose:** Generate a Shopify **Customer Events custom pixel** (and optional theme
snippet for storefront events) from the client ledger — never by guessing IDs.

**Input:** client slug → reads ledger: GTM ID, destinations, value definition,
consent permissions, which events, deterministic `event_id` policy.

**Output:** files only, in `~/.hermes/out/<client>/<verdict_id>/`:
- `custom-pixel.js` (P1 reference implementation, all mapped events)
- `README.md` (install steps in Shopify Admin, privacy setting to choose, test plan)
- `rollback.md` (disconnect pixel; previous pixel version text)

**Gates:**
- Refuses if any `<<REQUIRED>>` remains in the ledger fields it needs.
- Runs `datalayer-schema-validator` against a synthetic `checkout_completed` fixture
  before emitting files.
- Never calls Shopify Admin API write endpoints. A human installs the pixel.
