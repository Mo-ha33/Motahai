# Motahai Master Plan (2026-10-10)

The execution roadmap for Motahai Core, the Tariq/Hermes agent layer and the tenant portal. It builds on
`docs/MOTAHAI_CORE_FOUNDATION.md` (the audit of what exists) and turns its gaps into ordered phases. Every task below is
a GitHub issue on [Project #3](https://github.com/users/Mo-ha33/projects/3) and a row in `PROJECT_BOARD.md`.

**Ordering rule:** protect the money path first, make the pilot's numbers trustworthy second, then turn the engine into
a product people log into, and only then widen the platform and market surface.

**Board states:** Todo → In Progress → Review (PR open) → Done (merged and verified).

**Roles:** Opus plans, writes specs and board/plan documents, reviews diffs and runs tests. Production code, tests and
scripts are written by Sonnet (complex backend, integrations) and Haiku (utilities, tests, boilerplate, frontend
components) workers, and reviewed before a PR is opened.

---

## Phase 1: Money-path integrity (P0)

Goal: no delivered order, refusal or webhook is silently lost, replayed or mis-keyed.

| ID | Task | Depends on | Owner |
|---|---|---|---|
| M1-1 | S1-5 sweep + Bosta/OTO replay protection (#26, PR #23) | — | Repo audit thread |
| M1-2 | Poll Shopify and Salla order APIs for orders whose webhook never arrived | M1-1 | Workforce |
| M1-3 | Poll Bosta and OTO shipment APIs for missed courier updates | M1-1 | Workforce |
| M1-4 | Churn early-warning checks: Meta token expiry / error 190, webhook failure rate, 48 h silence → `incidents` | M1-1 | Workforce |
| M1-5 | Alembic baseline migration and a CI check that models and migrations agree | M1-1 | Workforce |
| M1-6 | SQLite WAL + busy_timeout; Postgres pool settings documented | M1-5 | Workforce |
| M1-7 | Re-hash stored phone hashes after the E.164 fix so refuser keys merge | — | Workforce |
| M1-8 | Verify Salla signature and `merchant` field against a real delivery | — | Needs Mo (real store) |

## Phase 2: Pilot readiness (P1)

Goal: the S2-7 pilot can run on real stores in all four markets with numbers a media buyer can trust.

| ID | Task | Depends on | Owner |
|---|---|---|---|
| M2-1 | S2-8: Salla thank-you capture of UA/IP so Salla events stop carrying `missing_user_agent` | — | Workforce |
| M2-2 | Operator endpoint for manual order confirmation (`emit_confirmed_order`), finishing S2-2 | — | Workforce |
| M2-3 | Courier remittance CSV import (cash actually collected), finishing S2-5 | M1-5 | Workforce |
| M2-4 | Pilot pre-flight CLI that runs the S2-7 checklist against a tenant or a test dataset | — | Workforce |
| M2-5 | Torod courier webhook (KSA) | M1-1 | Workforce |
| M2-6 | SMSA courier webhook (KSA) | M1-1 | Workforce |

## Phase 3: Product backend (P1)

Goal: every number a merchant sees comes from real rows through a tenant-scoped API that both the portal and Tariq use.

| ID | Task | Depends on | Owner |
|---|---|---|---|
| M3-1 | Tenant stats API on the digest.py named queries (TASK-011 endpoints) | — | Repo audit thread |
| M3-2 | Tracking health score from `quality_flags`, `job_runs`, `incidents` | M3-1 | Workforce |
| M3-3 | Tenant API auth: per-tenant scoped API keys, hashed at rest, rotation | — | Workforce |
| M3-4 | Operator endpoint + weekly scheduled audience export (refuser exclusion and seed CSVs) | — | Workforce |
| M3-5 | Hermes MCP read tool `get_cod_stats` (tenant-bound, read-only) + gateway policy and tests | M3-1 | Workforce |
| M3-6 | Onboarding flow (TASK-011): webhook registration, Meta dataset/token, courier secrets, start in shadow | M3-3 | Workforce |
| M3-7 | Honest GTM version note in `consultation.py` (what was verified, who approved) | — | Repo audit thread |

## Phase 4: Frontend and UX (P2)

Goal: a fast, bilingual (Arabic RTL first, English) merchant and agency portal. Default stack, to confirm before
M4-2: React + Vite + TypeScript, built to static files and served by Core behind the M3-3 auth; no server-side
rendering, no separate Node runtime in production.

| ID | Task | Depends on | Owner |
|---|---|---|---|
| M4-1 | Portal scaffold: Vite/React/TS, RTL/LTR i18n, design tokens, CI build, served at `/app` | — | Workforce |
| M4-2 | Sign-in with magic link or tenant API key; session handling | M3-3, M4-1 | Workforce |
| M4-3 | Signal health page (health score, last webhook, incidents, shadow vs live) | M3-1, M3-2, M4-2 | Workforce |
| M4-4 | Delivered ROAS and refusals page (weekly orders, cohort delivery rate, refused COD value) | M3-1, M4-2 | Workforce |
| M4-5 | Audiences page: download exclusion and seed CSVs, schedule status | M3-4, M4-2 | Workforce |
| M4-6 | Onboarding wizard UI | M3-6, M4-2 | Workforce |
| M4-7 | Agency view: many tenants, one login, per-tenant drill-down | M4-3 | Workforce |
| M4-8 | Weekly digest over WhatsApp (template message linking to the portal) | M3-1 | Workforce |

## Phase 5: Hermes / Tariq infrastructure (P2)

| ID | Task | Depends on | Owner |
|---|---|---|---|
| M5-1 | GTM drift sentinel (TASK-030): container fingerprint + beacon check, `job_runs` heartbeat, incident on drift | — | Workforce |
| M5-2 | Docs: HANDOFF_PLAYBOOK Coexist update; deprecate unbuilt claims in `docs/commercial/` | — | Core foundation audit thread |

## Phase 6: Expansion (P3)

| ID | Task | Depends on | Owner |
|---|---|---|---|
| M6-1 | TikTok Events API sender for the D-005 ladder | M2-4 | Workforce |
| M6-2 | Snap Conversions API sender | M2-4 | Workforce |
| M6-3 | Cash-delivered creative scorecard (TASK-040): ad-id macro + order→ad join | M3-1 | Workforce |
| M6-4 | Pre-dispatch WhatsApp confirmation to cut refusals (intervention, measured against control) | M2-2 | Workforce |
| M6-5 | Order risk score at checkout (address quality, refuser history, velocity) shown to the merchant | M1-7 | Workforce |
| M6-6 | Billing and subscriptions (per-tenant plan, usage = delivered orders signalled) | M3-3 | Workforce |

---

## Market use-case map

Ranked by ROI for the merchant and by how much of it Motahai already has. "Built" means code exists today.

| # | Use case | Who pays | Markets | Status |
|---|---|---|---|---|
| 1 | Delivered-only conversion signal (D-005 Coexist ladder) so Meta optimises on cash, not clicks | COD merchants, media buyers | EG, KSA, UAE, KW, wider GCC | Built; pilot pending (Phase 1–2) |
| 2 | Refuser exclusion audiences | Same | Same | Built, manual (M3-4 automates) |
| 3 | Delivered-buyer seed / lookalike audiences | Same | Same | Built, manual (M3-4) |
| 4 | Weekly signal and delivery digest (email, then WhatsApp) | Same | Same | Built (email); M4-8 |
| 5 | GTM container audit and drift monitoring by Tariq | Agencies, merchants | Global | Audit built; drift M5-1 |
| 6 | Courier performance analytics: delivery and refusal rate by courier, city, weekday | Merchants, 3PLs | EG, KSA | Data exists; needs M3-1 views |
| 7 | Pre-dispatch confirmation intervention (WhatsApp) to cut refusals before shipping cost is spent | Merchants | EG, KSA, MENA | M6-4 |
| 8 | Checkout risk score for likely refusals and fake orders | Merchants | COD markets | M6-5 |
| 9 | Cash-delivered creative scorecard | Media buyers | All | M6-3 |
| 10 | Multi-platform CAPI (TikTok, Snap) with the same ladder | Media buyers | MENA (Snap strong in KSA) | M6-1, M6-2 |
| 11 | Agency multi-tenant console | Agencies | All | M4-7 |
| 12 | Other COD-heavy markets: Morocco, Pakistan, India, Indonesia, LatAm | Same | Global | After pilot; needs phone/courier adapters |
| — | Real estate lead quality | — | — | Deferred until the COD pilot proves out (research thread verdict) |

## Definition of done (every task)

1. Acceptance criteria in the issue are met and covered by tests; `pytest` is green on the branch.
2. A PR is open, linked to the issue, reviewed by Opus against the spec, and its diff contains nothing outside scope.
3. No new claim in docs that the code does not support.
4. Board row and issue status updated in the same change.
