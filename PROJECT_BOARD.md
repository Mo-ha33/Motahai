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
| TASK-002 | Reconcile GCP Project ID and Service Account email | Gemini 3.8 | P0 | IN_PROGRESS | gcp, security, docs | Standardize agentic-ai-494313 across code and docs |
| TASK-003 | Enforce HITL approval gate on live GTM publish | Sonnet 5.5 | P0 | BACKLOG | hitl, gtm, safety | D-003 compliance: live deploy requires explicit token |
| TASK-004 | Remove fake-HTML fallback in browser_service.py | Haiku 5.5 | P0 | BACKLOG | audit, accuracy | Ensure failed site fetches report honest ERROR status |
| TASK-005 | Label canned metrics in workflow_engine as simulated | Haiku 5.5 | P0 | BACKLOG | telemetry, honesty | Mark simulated: true on mock CAPI parity & ROAS |
| TASK-010 | Tracking Asset Matrix & Client Delivery Sheet | Sonnet 5.5 | P0 | BACKLOG | core, tariq, sheet | Intake assets, organize credentials, deliver clean sheet |
| TASK-011 | Frictionless 1-Click Client Onboarding Flow | Sonnet 5.5 | P0 | BACKLOG | ux, onboarding | Shopify collaborator & 1-click Google service invite |
| TASK-020 | Production Meta CAPI Sender (graph.facebook.com) | Sonnet 5.5 | P0 | BACKLOG | capi, meta, core | Real server-side CAPI sender with retry & SHA-256 |
| TASK-021 | Implement Rule D-005 (COD OrderPlaced vs Purchase) | Sonnet 5.5 | P0 | BACKLOG | cod, capi, rules | Browser sends OrderPlaced; Server sends Purchase on delivery |
| TASK-022 | Zero-Effort Shopify/Salla Delivery Webhook Listener | Sonnet 5.5 | P0 | BACKLOG | cod, webhook | Trigger CAPI purchase upon order status = delivered |
| TASK-030 | 24/7 Automated Drift Sentinel & Container Monitor | Gemini 3.8 | P1 | BACKLOG | monitoring, cron | Periodic container fingerprinting & beacon check |
| TASK-031 | Weekly "Cash Saved on COD" Client Retention Report | Gemini 3.8 | P1 | BACKLOG | retention, reporting | Weekly report proving saved ad spend on returned orders |
| TASK-040 | Lightweight Creative Tracking & Auto-UTM Generator | Sonnet 5.5 | P2 | BACKLOG | ugc, creatives | Simple UTM generator & delivered cash scorecard for creators |

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

---

### 5. Verification Log & Evidence
- **TASK-001 Evidence:** Verified via `pytest -q`: `68 passed, 26 warnings in 7.57s`. `httpx` pinned to `0.27.2` in `requirements.txt`.
