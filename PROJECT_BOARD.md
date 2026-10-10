# 📋 Motahai Project Task Board (Sprint & Architecture Plan)
> **Autonomous Multi-Agent Task Orchestration Board**  
> *Last Updated:* 2026-10-10  
> *Repository:* [https://github.com/Mo-ha33/Motahai](https://github.com/Mo-ha33/Motahai)

---

### 1. STRICT AGENT & MODEL ROLES (Token-Saving Protocol)
- **Opus 5.5 (Lead Orchestrator):** STRICTLY FORBIDDEN FROM WRITING CODE. Opus is strictly the Reviewer, Planner, and Orchestrator. It plans the diffs, receives reports, reviews outputs, and directs tasks.
- **1 Sonnet 5.5, 1 Gemini 3.8, and 1 Haiku 5.5 Agents:** MUST write the actual code modifications and implementations.
- **1 Gemini 3.8 Agent:** Handles all GitHub checks, file lookups, log reads, and lightweight inspections.

---

### 2. Core Philosophy & Mission Definition
1. **Primary Mission:** Tariq is an elite Technical Marketer who sets up Google Tag Manager, GA4, Meta Pixel, TikTok, and Snap tracking; intakes assets; organizes and saves them securely; builds an organized tracking taxonomy sheet; and delivers it to the client.
2. **First High-Value Use Case (The Wedge):** Resolving the Cash on Delivery (COD) ad signal distortion in Egypt and the GCC by ensuring Meta/TikTok algorithms only optimize on **delivered and paid orders**.
3. **Execution Guardrail:** No code is written without prior planning, task logging on this board, and git synchronization.

---

### 3. Active Tasks Matrix

| TASK_ID | Title | Assignee | Priority | Status | Tags | Notes |
| :--- | :--- | :--- | :---: | :---: | :--- | :--- |
| TASK-001 | Pin httpx<0.28.0 & verify 68 of 68 tests pass | Haiku 5.5 | P0 | DONE | test, env | Fixed Starlette TestClient bug; 68/68 tests pass in 7.57s |
| TASK-002 | Reconcile GCP Project ID and Service Account email | Gemini 3.8 | P0 | DONE | gcp, security, docs | Standardized service account email to tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com (updated 2026-10-09 20:31) |
| TASK-003 | Enforce HITL approval gate on live GTM publish | Sonnet 5.5 | P0 | DONE | hitl, gtm, safety | Fixed by S1-0 (signed single-use tokens). (updated 2026-10-09 21:16) |
| TASK-004 | Remove fake-HTML fallback in browser_service.py | Haiku 5.5 | P0 | DONE | audit, accuracy | Removed fake HTML fallback; failed site fetches now report explicit ERROR (updated 2026-10-09 20:33) |
| TASK-005 | Label canned metrics in workflow_engine as simulated | Haiku 5.5 | P0 | DONE | telemetry, honesty | Labeled mock CAPI parity and ROAS metrics as simulated: True (updated 2026-10-09 20:34) |
| TASK-010 | Tracking Asset Matrix & Client Delivery Sheet | Sonnet 5.5 | P0 | DONE | core, tariq, sheet | Implemented TrackingAssetMatrixBuilder producing client-facing sheets (updated 2026-10-09 20:37) |
| TASK-011 | Frictionless 1-Click Client Onboarding Flow | Sonnet 5.5 | P0 | BACKLOG | ux, onboarding | Shopify collaborator & 1-click Google service invite |
| TASK-020 | Production Meta CAPI Sender (graph.facebook.com) | Sonnet 5.5 | P0 | DONE | capi, meta, core | Implemented MetaCAPISender with Graph API v20.0 and SHA-256 hashing (updated 2026-10-09 20:37) |
| TASK-021 | Implement Rule D-005 (COD OrderPlaced vs Purchase) | Sonnet 5.5 | P0 | DONE | cod, capi, rules | Fixed by S1-0 (Coexist: DeliveredPurchase, delivered_<order_id>). (updated 2026-10-09 21:16) |
| TASK-022 | Zero-Effort Shopify/Salla Delivery Webhook Listener | Sonnet 5.5 | P0 | IN_PROGRESS | cod, webhook | Parser class landed in d51dcf0; no HTTP route, no HMAC verification, no idempotency yet (review B3/B4) — see S1-1, S1-2. |
| TASK-030 | 24/7 Automated Drift Sentinel & Container Monitor | Haiku 5.5 | P1 | BACKLOG | monitoring, cron | Periodic container fingerprinting & beacon check; systemd timers + job_runs heartbeat; S1-5 |
| TASK-031 | Weekly Signal Hygiene Digest (Sunday WhatsApp) | Sonnet 5.5 | P1 | DONE | retention, reporting | Implemented by S1-4. (updated 2026-10-10 00:24) |
| TASK-040 | Cash-Delivered Creative Scorecard | Sonnet 5.5 | P2 | BACKLOG | ugc, creatives | Narrowed scope: ad-level URL macro template (utm_content={{ad.id}}) + order→ad_id join; no creative dashboards, no creator marketplace. |
| S1-0 | Fix D-003 gate (signed approvals) + D-005 delivered detection & Coexist event | Sonnet 5.5 | P0 | DONE | hitl, cod, capi, security | Signed single-use D-003 tokens, D-005 Coexist DeliveredPurchase, COD delivered detection fixes, access_token auth, stale guard. 155 tests pass. (updated 2026-10-09 21:16) |
| S1-1 | Persistence + idempotency (tenants, orders, capi_events UNIQUE, webhook_deliveries, incidents, digests, job_runs) | Sonnet 5.5 | P0 | DONE | db, idempotency | Committed a7c4a75. 184 tests. (updated 2026-10-09 21:33) |
| S1-2 | Webhook HTTP routes + Shopify HMAC / Salla signature verification | Sonnet 5.5 | P0 | DONE | webhook, security | Committed with S1-3. Follow-ups: rate-limit/prune bad-signature rows; verify Salla signature + merchant field on a real delivery. (updated 2026-10-09 21:59) |
| S1-3 | Checkout capture: UTMs, ad_id, fbp/fbc/ttclid/ScCid + client_details into CAPI payload | Sonnet 5.5 | P0 | DONE | capi, attribution | Committed with S1-2. Verify _mt_ attrs reach note_attributes on a dev store; Salla attribution gap. (updated 2026-10-09 21:59) |
| S1-4 | Sunday digest generator (reports/weekly_digest.py) | Sonnet 5.5 | P1 | DONE | reporting | digest.py: SQL-only metrics, ar/en email, Sunday 10:00 tenant-local, idempotent per week. (updated 2026-10-10 00:24) |
| S1-5 | Scheduler + churn early-warning checks (unread digests, token expiry/Meta 190, webhook failures, 48h silence) | Haiku 5.5 | P1 | IN_PROGRESS | monitoring, cron | S2 wave 2 dispatched 2026-10-10. (updated 2026-10-09 22:21) |
| S1-6 | Platform API lookups (TikTok/Snap macros, Meta partner sharing, Shopify/Salla webhooks, WhatsApp templates) | Haiku 5.5 | P1 | DONE | research, integrations | docs/research/S1-6_platform_lookups.md; open: custom-event optimization unconfirmed, Salla signature/COD enum inferred. (updated 2026-10-09 21:06) |
| S1-7 | Board hygiene | Haiku 5.5 | P2 | DONE | board, hygiene | Review 2026-10-09 board update. |
| S2-1 | event_time = order placed, settlement window, late_delivery terminal state | Sonnet 5.5 | P0 | DONE | cod, capi, d005 | Committed in S2 wave 1. (updated 2026-10-09 22:21) |
| S2-2 | ConfirmedOrder event + confirmation sources (Shopify tag, Salla status, WhatsApp, manual) | Sonnet 5.5 | P0 | IN_PROGRESS | cod, capi, d005 | S2 wave 2 dispatched 2026-10-10. (updated 2026-10-09 22:21) |
| S2-3 | Match keys: encrypted checkout IP/UA (D-006), event_source_url, external_id, hashed fn/ln/ct/country/zp | Sonnet 5.5 | P0 | DONE | capi, emq | Committed in S2 wave 1. (updated 2026-10-09 22:21) |
| S2-4 | partially_refunded -> DeliveredPurchase with net collected value | Sonnet 5.5 | P0 | DONE | cod, capi, bug | Committed in S2 wave 1. (updated 2026-10-09 22:21) |
| S2-5 | Courier fallback: aggregator tracking (OTO/Torod), Bosta webhook, remittance CSV import | Sonnet 5.5 | P1 | IN_PROGRESS | cod, couriers | S2 wave 2 dispatched 2026-10-10. (updated 2026-10-09 22:21) |
| S2-6 | Refuser exclusion + delivered-buyer seed audience CSV exports | Haiku 5.5 | P1 | DONE | audiences, retention | Committed in S2 wave 1. (updated 2026-10-09 22:21) |
| S2-7 | Pilot validation plan: required CAPI fields, custom-conversion optimization, ROAS bidding, A/B test | Haiku 5.5 | P1 | DONE | research, pilot | Committed in S2 wave 1. (updated 2026-10-09 22:21) |
| S2-8 | Thank-you page capture (Salla App Snippet + Shopify checkout_completed): order_id + UA + IP + fbp/fbc → Core | Sonnet 5.5 | P1 | IN_PROGRESS | capture, salla, emq | Salla webhooks carry no UA/IP; Meta requires client_user_agent for website events. Wave 2. Also: handle_order_update made decision-only. |
| S1-8 | Ed25519 asymmetric HITL signing + user separation | Sonnet 5.5 | P1 | DONE | hitl, security | Ed25519 D-003 tokens (no HMAC), deployment/isolation manifests. (updated 2026-10-10 00:24) |
| TASK-101 | Fix E.164 phone normalization for EG (+20), KSA (+966), UAE (+971), KW (+965) — [#24](https://github.com/Mo-ha33/Motahai/issues/24) | Sonnet 5.5 | P0 | DONE | p0, bug, capi | Merged in [#22](https://github.com/Mo-ha33/Motahai/pull/22) (f1d21f2); 644 tests pass on main. (updated 2026-10-10) |
| TASK-102 | Meta Graph API v20.0 → v25.0 + `test_event_code` support — [#25](https://github.com/Mo-ha33/Motahai/issues/25) | Sonnet 5.5 | P0 | DONE | p0, capi | Merged in [#22](https://github.com/Mo-ha33/Motahai/pull/22) (f1d21f2). Pinned to v25.0 (expires 2028-07-29), override via `MOTAHAI_META_GRAPH_API_VERSION`. (updated 2026-10-10) |
| TASK-103 | S1-5 reconciliation sweep + Bosta/OTO webhook replay protection — [#26](https://github.com/Mo-ha33/Motahai/issues/26) | Sonnet 5.5 | P0 | DONE | p0, security, webhooks | Merged in PR #23 (20f2e27); #26 closed. Platform/courier polling continues in #28/#29. (updated 2026-10-10) |
| M1-2 | Poll Shopify and Salla order APIs for orders whose webhook never arrived — [#28](https://github.com/Mo-ha33/Motahai/issues/28) | Sonnet 5.5 | P0 | BACKLOG | phase-1, backend, webhooks, feature | Depends on: #26 (PR #23). (updated 2026-10-10) |
| M1-3 | Poll Bosta and OTO shipment APIs for missed courier updates — [#29](https://github.com/Mo-ha33/Motahai/issues/29) | Sonnet 5.5 | P0 | BACKLOG | phase-1, backend, webhooks, feature | Depends on: #26 (PR #23). (updated 2026-10-10) |
| M1-4 | Churn early-warning checks written as incidents — [#30](https://github.com/Mo-ha33/Motahai/issues/30) | Sonnet 5.5 | P1 | BACKLOG | phase-1, backend, feature | Depends on: #26 (PR #23). (updated 2026-10-10) |
| M1-5 | Alembic baseline migration and models/migrations drift check — [#31](https://github.com/Mo-ha33/Motahai/issues/31) | Sonnet 5.5 | P0 | BACKLOG | phase-1, infra, backend | Depends on: #26 (PR #23) merged. (updated 2026-10-10) |
| M1-6 | Enable SQLite WAL and busy_timeout; document Postgres pool settings — [#32](https://github.com/Mo-ha33/Motahai/issues/32) | Haiku 5.5 | P1 | BACKLOG | phase-1, infra, backend | Depends on: M1-5. (updated 2026-10-10) |
| M1-7 | Re-hash stored phone hashes after the E.164 fix so refuser keys merge — [#33](https://github.com/Mo-ha33/Motahai/issues/33) | Sonnet 5.5 | P0 | BACKLOG | phase-1, backend, capi, bug | Raw phone is never stored (db.py:150-152); re-hash needs re-fetching orders via the M1-2 API clients. Depends on: #28. (updated 2026-10-10) |
| M1-8 | Verify Salla signature and merchant field against a real delivery — [#34](https://github.com/Mo-ha33/Motahai/issues/34) | Sonnet 5.5 | P0 | BACKLOG | phase-1, security, webhooks, needs-human | Depends on: none. (updated 2026-10-10) |
| M2-1 | S2-8: Salla thank-you capture of UA and IP — [#35](https://github.com/Mo-ha33/Motahai/issues/35) | Sonnet 5.5 | P1 | BACKLOG | phase-2, backend, capi, feature | Depends on: none. (updated 2026-10-10) |
| M2-2 | Operator endpoint for manual order confirmation — [#36](https://github.com/Mo-ha33/Motahai/issues/36) | Sonnet 5.5 | P1 | DONE | phase-2, backend, feature | Merged in #67 (620db88) with M3-4; full suite 747 passed on main. (updated 2026-10-10) |
| M2-3 | Courier remittance CSV import — [#37](https://github.com/Mo-ha33/Motahai/issues/37) | Sonnet 5.5 | P1 | BACKLOG | phase-2, backend, feature | Depends on: M1-5. (updated 2026-10-10) |
| M2-4 | Pilot pre-flight CLI for the S2-7 checklist — [#38](https://github.com/Mo-ha33/Motahai/issues/38) | Haiku 5.5 | P1 | DONE | phase-2, backend, feature | Merged in #66 (13fe819). (updated 2026-10-10) |
| M2-5 | Torod courier webhook (KSA) — [#39](https://github.com/Mo-ha33/Motahai/issues/39) | Sonnet 5.5 | P2 | BACKLOG | phase-2, backend, webhooks, feature | Depends on: #26 (PR #23). (updated 2026-10-10) |
| M2-6 | SMSA courier webhook (KSA) — [#40](https://github.com/Mo-ha33/Motahai/issues/40) | Haiku 5.5 | P2 | BACKLOG | phase-2, backend, webhooks, feature | Depends on: #26 (PR #23). (updated 2026-10-10) |
| M3-1 | Tenant stats API on the digest.py named queries — [#41](https://github.com/Mo-ha33/Motahai/issues/41) | Sonnet 5.5 | P1 | DONE | phase-3, backend, feature, owner:other-thread | Merged in PR #68 (4f90eb3). Repo audit thread. (updated 2026-10-10) |
| M3-2 | Tracking health score — [#42](https://github.com/Mo-ha33/Motahai/issues/42) | Sonnet 5.5 | P1 | DONE | phase-3, backend, feature | Merged in #77 (2f61178): GET /v1/tenants/{id}/stats/health-score; 765 passed on main. (updated 2026-10-10) |
| M3-3 | Tenant API auth: scoped API keys — [#43](https://github.com/Mo-ha33/Motahai/issues/43) | Sonnet 5.5 | P1 | DONE | phase-3, backend, security, feature | Merged in #73 (db56339). (updated 2026-10-10) |
| M3-4 | Operator endpoint and weekly schedule for audience export — [#44](https://github.com/Mo-ha33/Motahai/issues/44) | Sonnet 5.5 | P1 | DONE | phase-3, backend, feature | Merged in #67 (620db88); weekly export job, rolling 7-day gate. (updated 2026-10-10) |
| M3-5 | Hermes MCP read tool get_cod_stats — [#45](https://github.com/Mo-ha33/Motahai/issues/45) | Sonnet 5.5 | P1 | BACKLOG | phase-3, hermes, feature | Depends on: M3-1. (updated 2026-10-10) |
| M3-6 | Onboarding flow (TASK-011) — [#46](https://github.com/Mo-ha33/Motahai/issues/46) | Sonnet 5.5 | P1 | DONE | phase-3, backend, feature | Merged in #81 (f14989f): /v1/operator/onboarding; 773 passed on main. (updated 2026-10-10) |
| M3-7 | Honest GTM version note in consultation.py — [#47](https://github.com/Mo-ha33/Motahai/issues/47) | Sonnet 5.5 | P1 | DONE | phase-3, hermes, bug, owner:other-thread | Merged in PR #69 (f2573f7). Repo audit thread. (updated 2026-10-10) |
| M4-1 | Portal scaffold: React + Vite + TS with RTL/LTR i18n — [#48](https://github.com/Mo-ha33/Motahai/issues/48) | Haiku 5.5 | P2 | DONE | phase-4, frontend, feature | Merged in #70 (d2f5582). Portal pages M4-2..M4-7 owned by the Client portal dashboard UI thread. (updated 2026-10-10) |
| M4-2 | Portal sign-in and session — [#49](https://github.com/Mo-ha33/Motahai/issues/49) | Haiku 5.5 | P2 | DONE | phase-4, frontend, security, feature | Merged in #78 (5242cc1) by the portal thread: tenant-key sign-in, stats routes accept stats:read keys (health-score included), GET /v1/tenant/me. (updated 2026-10-10) |
| M4-3 | Portal: signal health page — [#50](https://github.com/Mo-ha33/Motahai/issues/50) | Haiku 5.5 | P2 | DONE | phase-4, frontend, feature | Merged in #84 (c325425) by the portal thread, with the health-score widget. (updated 2026-10-10) |
| M4-4 | Portal: delivered ROAS and refusals page — [#51](https://github.com/Mo-ha33/Motahai/issues/51) | Haiku 5.5 | P2 | DONE | phase-4, frontend, feature | Merged in #76 (557ce32) by the portal thread. (updated 2026-10-10) |
| M4-5 | Portal: audiences page — [#52](https://github.com/Mo-ha33/Motahai/issues/52) | Haiku 5.5 | P2 | IN_PROGRESS | phase-4, frontend, feature | Owner: Client portal dashboard UI thread. Tenant-scoped audience status/download API (audiences:read scope) + Audiences page. (updated 2026-10-10) |
| M4-6 | Portal: onboarding wizard — [#53](https://github.com/Mo-ha33/Motahai/issues/53) | Haiku 5.5 | P2 | DONE | phase-4, frontend, feature | Merged in #84 (c325425) by the portal thread, on the #81 onboarding API. (updated 2026-10-10) |
| M4-7 | Portal: agency multi-tenant view — [#54](https://github.com/Mo-ha33/Motahai/issues/54) | Haiku 5.5 | P2 | BACKLOG | phase-4, frontend, feature | Owner: Client portal dashboard UI thread. (updated 2026-10-10) |
| M4-8 | Weekly digest over WhatsApp — [#55](https://github.com/Mo-ha33/Motahai/issues/55) | Sonnet 5.5 | P2 | BACKLOG | phase-4, backend, feature | Depends on: M3-1. (updated 2026-10-10) |
| M4-9 | Portal: configurable dashboard framework — [#80](https://github.com/Mo-ha33/Motahai/issues/80) | Sonnet 5.5 | P2 | DONE | phase-4, frontend, feature | Merged in #76 (557ce32): widget registry, typed config, theme tokens, Stats API client. (updated 2026-10-10) |
| M5-1 | GTM drift sentinel (TASK-030) — [#56](https://github.com/Mo-ha33/Motahai/issues/56) | Sonnet 5.5 | P2 | BACKLOG | phase-5, hermes, feature | Depends on: none. (updated 2026-10-10) |
| M5-2 | Docs: HANDOFF_PLAYBOOK Coexist update and commercial claims cleanup — [#57](https://github.com/Mo-ha33/Motahai/issues/57) | Sonnet 5.5 | P2 | DONE | phase-5, docs, owner:other-thread | Merged in PR #65 (c37d559). Core foundation audit thread. (updated 2026-10-10) |
| M6-1 | TikTok Events API sender for the D-005 ladder — [#58](https://github.com/Mo-ha33/Motahai/issues/58) | Sonnet 5.5 | P3 | BACKLOG | phase-6, backend, capi, feature | Depends on: M2-4. (updated 2026-10-10) |
| M6-2 | Snap Conversions API sender — [#59](https://github.com/Mo-ha33/Motahai/issues/59) | Sonnet 5.5 | P3 | BACKLOG | phase-6, backend, capi, feature | Depends on: M2-4. (updated 2026-10-10) |
| M6-3 | Cash-delivered creative scorecard (TASK-040) — [#60](https://github.com/Mo-ha33/Motahai/issues/60) | Sonnet 5.5 | P3 | BACKLOG | phase-6, backend, feature | Depends on: M3-1. (updated 2026-10-10) |
| M6-4 | Pre-dispatch WhatsApp confirmation to cut refusals — [#61](https://github.com/Mo-ha33/Motahai/issues/61) | Sonnet 5.5 | P3 | BACKLOG | phase-6, backend, feature | Depends on: M2-2. (updated 2026-10-10) |
| M6-5 | Checkout refusal-risk score — [#62](https://github.com/Mo-ha33/Motahai/issues/62) | Sonnet 5.5 | P3 | BACKLOG | phase-6, backend, feature | Depends on: M1-7. (updated 2026-10-10) |
| M6-6 | Billing and subscriptions — [#63](https://github.com/Mo-ha33/Motahai/issues/63) | Sonnet 5.5 | P3 | BACKLOG | phase-6, backend, feature | Depends on: M3-3. (updated 2026-10-10) |
| B-1 | Board automation: set Project #3 status from Actions — [#64](https://github.com/Mo-ha33/Motahai/issues/64) | Haiku 5.5 | P1 | DONE | phase-1, infra, board | Merged in #27; per-item error annotations in #79. Project #3 has no Review option, so REVIEW rows show as In Progress there. (updated 2026-10-10) |
| B-2 | CI: run pytest and the portal build on every PR — [#71](https://github.com/Mo-ha33/Motahai/issues/71) | Haiku 5.5 | P1 | DONE | phase-1, infra, board | Merged in #72 (e0e0b9e); Python + portal jobs green. (updated 2026-10-10) |
| S-1 | Require authentication on /tasks, /escalations and /webhook/hermes — [#74](https://github.com/Mo-ha33/Motahai/issues/74) | Sonnet 5.5 | P0 | DONE | phase-1, security, backend, bug | Merged in #75 (36ce59a). Before next deploy: Hermes must send Authorization: Bearer <HERMES_API_KEY>. (updated 2026-10-10) |

---

### 4. Sprint Execution Sequence
```
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 0: Safety & Honesty Fixes (TASK-001 -> TASK-005)                  │
│ Objective: 100% green tests, verified GCP IDs, zero canned metrics.    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 1: Core Technical Marketer Service (TASK-010 & TASK-011)         │
│ Objective: Asset intake, clean tracking sheet, 1-click onboarding.     │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 2: COD & Real Meta CAPI Engine (TASK-020 -> TASK-022)             │
│ Objective: Real graph.facebook.com calls & delivered-only webhooks.    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 3: 24/7 Drift Sentinel & Retention Loops (TASK-030 -> TASK-031)  │
│ Objective: Continuous container monitoring and weekly ROI report.      │
└────────────────────────────────────────────────────────────────────────┘
```

**Sprint S1 order:** S1-0 / S1-6 / S1-7 → S1-1 → S1-2 + S1-3 → S1-4 + S1-5.

---

### 5. Verification Log & Evidence
- **TASK-001 Evidence:** Verified via `pytest -q`: `68 passed, 26 warnings in 7.57s`. `httpx` pinned to `0.27.2` in `requirements.txt`.
- **Review 2026-10-09 (Opus):** TASK-003, TASK-021 and TASK-022 reopened to IN_PROGRESS for defects B1–B4 found in d51dcf0 and the gate code. TASK-031 and TASK-040 retitled and narrowed to the 2026-10-09 scope change. TASK-030 reassigned to Haiku 5.5 because Gemini cannot be dispatched from the Claude Code session. Sprint S1 rows S1-0 to S1-7 added. Board edits only; no code changed and no tests re-run.
- **Board sync 2026-10-10:** TASK-101..103 logged as GitHub issues #24, #25, #26 (labels `p0`, `bug`, `capi`, `security`, `webhooks`). Status REVIEW = GitHub board "In Progress (in review)". Adding them to the [GitHub Project #3](https://github.com/users/Mo-ha33/projects/3) failed from the Claude Code session: user-level Projects API (GraphQL and REST `users/Mo-ha33/projectsV2/3`) returned HTTP 403 because the session is scoped to repository endpoints only. `.github/workflows/project-sync.yml` adds labelled issues to the board once the `ADD_TO_PROJECT_PAT` secret is set.
- **Token-saving helpers:** `scripts/dev/qtest.py` (targeted pytest, summary + one line per failure) and `scripts/dev/logscan.py` (level/grep filter, repeat collapsing, short tail for files, stdin or `journalctl -u`).
- **Master plan 2026-10-10:** `docs/MASTER_PLAN.md` phases M1–M6 logged as issues #28–#64. Older rows map onto them: S1-5 → M1-2/3/4, S2-2 → M2-2, S2-5 → M2-3/5/6, S2-8 → M2-1, TASK-011 → M3-6, TASK-030 → M5-1, TASK-040 → M6-3. BACKLOG = board Todo.
- **2026-10-10 13:07:** #23, #68, #69 merged to main by the repo audit thread; main suite 685 passed, 1 skipped.
- **Sprint 2026-10-10 (Mo away 2 h, merges authorised):** merged #27, #72, #70, #66, #75, #73 after full pytest on each merged with main.
