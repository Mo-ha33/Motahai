# 📋 Motahai Project Task Board (Sprint & Architecture Plan)
> **Autonomous Multi-Agent Task Orchestration Board**  
> *Last Updated:* 2026-10-09  
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
| TASK-040 | Cash-Delivered Creative Scorecard | Sonnet 5.5 | P2 | BACKLOG | ugc, creatives | Narrowed scope: ad-level URL macro template (utm_content={{ad.id}}) + order→ad_id join; no creative dashboards, no creator marketplace; Ziad video paused. |
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

**Sprint S3 (approved 2026-10-11) — W6 COD Signal Activation + W7 Health Check on Tariq:**

| TASK_ID | Title | Assignee | Priority | Status | Notes |
| :--- | :--- | :--- | :---: | :---: | :--- |
| S3-7 | Lookups: Meta custom-conversion optimization on server-only custom events; Wesam cron triggers | Antigravity (Gemini 3.8) | P0 | DONE (conditional) | Accepted as working hypothesis; no sources cited. Per D-005 the optimization test uses ConfirmedOrder, not DeliveredPurchase; ~50 events/ad set/7d is the gate for the optimization test, not for going live. action_source stays `website`. |
| S3-1 | Core read-only API: tenant status + signal health (counts only, Hermes-scoped bearer) | Sonnet 5.5 | P0 | DONE | Merged to main via integration/release (2026-10-11). |
| S3-3 | Per-tenant test_event_code + verify path (replay one shadow event to Meta Test Events) | Sonnet 5.5 | P0 | DONE | Merged to main via integration/release (2026-10-11). |
| S3-2 | Hermes tools hermes_motahai_tenant_status / signal_health / promote_live | Gemini 3.8 (Antigravity) | P0 | DONE | G1 by Antigravity (085902e); merged to main 2026-10-11. Core not deployed on VPS yet → tools return not-configured. |
| S3-4 | promote_live endpoint + operator mint endpoint, Ed25519 action token (promote_live, tenant) + Shopify/Salla courier exemption | Sonnet 5.5 | P0 | DONE | Merged to main via integration/release (2026-10-11). |
| S3-5 | W6/W7 in build_tariq_payload.py (behind --with-cod-workflows) + Arabic M6-1..3 | Haiku 5.5 | P0 | DONE | Merged (233d41a); COD workflows still off by default. |
| S3-6 | Drift Sentinel scheduler job (read-only GTM live-version fingerprint → incident → digest) | Haiku 5.5 | P1 | BACKLOG | Core scheduler, not Wesam cron. |
| S3-9 | Tariq prompt hardening (no-tool-no-claim, one store per conversation, resume, waiting msg, intent map) | Gemini 3.8 (Antigravity) | P1 | DONE | G2 by Antigravity (085902e); live on Wesam (default mode). |
| S3-10 | hermes_storefront_probe: deterministic headless network-capture probe (GTM/GA4/Meta ids, duplicates, consent signals; UNKNOWN on load failure) | Gemini 3.8 (Antigravity) | P0 | DONE | G3 by Antigravity + Sonnet SSRF request guard / worker-thread fix (91161c0). Merged 2026-10-11. |
| S3-8 | Inject + verify W6/W7 on Wesam via Chrome MCP | Antigravity | P0 | BLOCKED | Inject only after S3-1 + S3-2 are deployed on the VPS (S3-4 for the promotion step); otherwise Tariq calls tools that do not exist. |

**Sentinel (approved 2026-10-11) — token-exhaustion sentinel & dead-man's switch** (spec: docs/sprints/SENTINEL_T1_T5_SPECS.md)

| TASK_ID | Title | Assignee | Priority | Status | Notes |
| :--- | :--- | :--- | :---: | :---: | :--- |
| T1 | Flow-state writer (ops/hermes/sentinel/flow_state.py + small consultation.py hook) | Haiku 5.5 | P0 | DONE | aed7a02; merged to main via integration/release 2026-10-11. |
| T2 | hermes-sentinel detector (journal + /healthz + flow state → incidents) | Sonnet 5.5 | P0 | DONE | e31ce8a; merged to main via integration/release 2026-10-11. |
| T3 | Dispatcher (Telegram primary, SMTP fallback + client holding email) | Haiku 5.5 | P0 | DONE | edc66f2; merged to main via integration/release 2026-10-11. |
| T4 | Static alert/client templates (ar/en) | Haiku 5.5 | P1 | DONE | 21fdf0f; merged to main via integration/release 2026-10-11. |
| T5 | systemd service/timer + operator runbook | Haiku 5.5 | P1 | DONE | 2c93569; merged to main via integration/release 2026-10-11. |

**Sprint S3 order:** S3-1 + S3-3 + S3-5 → S3-2 + S3-4 → VPS deploy → S3-8. S3-6 in parallel.

---

### 5. Verification Log & Evidence
- **TASK-001 Evidence:** Verified via `pytest -q`: `68 passed, 26 warnings in 7.57s`. `httpx` pinned to `0.27.2` in `requirements.txt`.
- **Review 2026-10-09 (Opus):** TASK-003, TASK-021 and TASK-022 reopened to IN_PROGRESS for defects B1–B4 found in d51dcf0 and the gate code. TASK-031 and TASK-040 retitled and narrowed to the 2026-10-09 scope change. TASK-030 reassigned to Haiku 5.5 because Gemini cannot be dispatched from the Claude Code session. Sprint S1 rows S1-0 to S1-7 added. Board edits only; no code changed and no tests re-run.
