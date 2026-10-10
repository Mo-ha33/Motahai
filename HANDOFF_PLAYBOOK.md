# 🚀 Motahai (Hermes & Tariq) — Master Handoff Playbook
> **Architectural Status, Ground Truth Verification & Multi-Agent Directive**  
> *Target Assignee:* Claude Opus 5.5 (Lead Orchestrator & Reviewer)  
> *Repository:* [https://github.com/Mo-ha33/Motahai](https://github.com/Mo-ha33/Motahai)  
> *Latest Commits on main:* `9ee2d61` (Phase 0 Fixes) & `d51dcf0` (CAPI, COD & Webhooks Engine)  
> *Test Suite:* **74 passed, 0 failed in 9.47s**

---

## 1. STRICT AGENT & MODEL ROLES (Token-Saving Protocol)
- **Opus 5.5 (Lead Orchestrator):** STRICTLY FORBIDDEN FROM WRITING CODE. Opus is strictly the Reviewer, Planner, and Orchestrator. It plans the diffs, receives reports, reviews outputs, and directs tasks.
- **1 Sonnet 5.5, 1 Gemini 3.8, and 1 Haiku 5.5 Agents:** MUST write the actual code modifications and implementations.
- **1 Gemini 3.8 Agent:** Handles all GitHub checks, file lookups, log reads, and lightweight inspections.

---

## 2. Ground Truth & Exact System State (Verified on GitHub `main`)

We heard your feedback loud and clear, and we **executed and pushed the code directly to GitHub** (`d51dcf0` and `9ee2d61`). Here is the verified reality:

| Component / Finding | Previous Status | Current Ground Truth in Repo (`main`) |
|---|---|---|
| **Test Suite Health** | 44 passed, 24 failed | **74 passed, 0 failed (100% green)**. `httpx` pinned to `0.27.2` in `requirements.txt`. |
| **Rule D-003 HITL Gate** | Missing approval step | **Enforced in code.** `ops/hermes/consultation.py` halts `hermes_gtm_cloud_publish` unless it receives a signed, single-use Ed25519 approval token bound to one container and workspace (issued by `POST /approvals/gtm-publish`; see `src/ameen_workforce/hitl_tokens.py`). |
| **Browser Audit Fallback** | Fake HTML on fetch error | **Removed.** `browser_service.py` returns explicit `http_status: 0` and error scorecard. |
| **Hardcoded CAPI/BI Metrics** | Unlabeled mock numbers | **Labeled honestly.** `workflow_engine.py` explicitly tags metrics with `simulated: True` and `provenance: benchmark_simulation`. |
| **Service Account Email** | Discrepancy with GCP | **Standardized.** Reconciled to `tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com`. |
| **Real Meta CAPI Sender** | Vision only (0 calls) | **Implemented in code.** `src/ameen_workforce/capi_service.py` with Meta Graph API v20.0, SHA-256 E.164 phone/email normalization, and `test_event_code` support. |
| **Rule D-005 (COD Logic)** | Vision only | **Implemented & tested.** `process_cod_order_event` emits `OrderPlaced` for in-transit COD, and `Purchase` (`purchase_<id>`) **only when delivered**. |
| **Zero-Effort Webhooks** | Vision only | **Implemented & tested.** `src/ameen_workforce/webhook_listener.py` parses Shopify and Salla webhooks and dispatches delivered CAPI events. |
| **Client Asset Delivery** | Manual | **Implemented & tested.** `src/ameen_workforce/asset_matrix.py` formats IDs and exports Markdown delivery sheets. |
| **Project Task Board** | None | **Live & synced.** `PROJECT_BOARD.md` and CLI tool `tools/board/manage_board.py` tracking 13 tasks. |

---

## 3. Core Architectural & Strategic Decisions (Agreed & Locked)

### Decision 1: Rule D-005 (Server-Side Only COD Purchase)
- **Client-Side:** For COD orders, browser fires custom event `OrderPlaced` on checkout (never `Purchase`).
- **Server-Side:** When the store platform (Shopify/Salla) updates status to `delivered` / `paid`, Motahai sends `Purchase` via CAPI with `event_id = purchase_<order_id>`.
- **Prepaid Orders:** Fire standard D-002 deduplicated browser + server events immediately.

### Decision 2: Frictionless Onboarding & Zero-GTM Prerequisite
- **The COD Wedge does NOT need GTM access.** It needs only:
  1. The store platform app / webhook (Shopify/Salla).
  2. Meta Pixel/Dataset access via **Meta Partner business sharing** OR token paste.
- GTM container setup, linting, and remediation remain a high-value **Phase 2 audit and service**, not a blocker to start.

### Decision 3: Narrow & Defensible UGC / Creative Tracking Scope
- **IN SCOPE:** "Cash-Delivered Creative Scorecard" answering: *Which creative brings orders that actually get delivered and paid?*
  - Implementation: One ad-level URL macro template (e.g., `utm_content={{ad.id}}`). Checkout capture stores `utm_content` on the order. Join order → `ad_id` → delivery status → cash-delivered ROAS.
- **OUT OF SCOPE (Say NO):**
  - No creative dashboards, thumbstops, or hook rate analytics (leave to Motion).
  - No creator marketplace.
  - **Paused:** Ziad autonomous video generation is strictly for Motahai's internal marketing, not sold in the core product.

### Decision 4: Honest Retention Loop & Sunday WhatsApp Report
- **Never claim "Saved ad budget"** (unprovable metric).
- **Report verifiable facts every Sunday over WhatsApp:**
  1. *Signal Hygiene:* "Last week, 38 undelivered orders (21,400 EGP) were kept out of Meta's training algorithms."
  2. *North-Star Trend:* Delivery rate of ad-driven orders week-over-week.
  3. *Incidents Caught:* "Thursday theme update broke checkout listener; restored in 2 hours."
  4. *Creative Scorecard:* Top and bottom 3 ads by delivered cash revenue.

---

## 4. Current Sprint Task Board Status

Managed via `tools/board/manage_board.py` on GitHub:
- `TASK-001` (Pin httpx, 68 tests green): **DONE**
- `TASK-002` (Reconcile GCP project & SA email): **DONE**
- `TASK-003` (Enforce D-003 HITL gate on live deploy): **DONE**
- `TASK-004` (Remove fake-HTML fallback): **DONE**
- `TASK-005` (Label mock metrics as simulated: True): **DONE**
- `TASK-010` (Tracking Asset Matrix & Delivery Sheet): **DONE**
- `TASK-020` (Production Meta CAPI Sender): **DONE**
- `TASK-021` (Implement Rule D-005 in code): **DONE**
- `TASK-022` (Zero-Effort Shopify/Salla Webhook Listener): **DONE**
- `TASK-011` (Frictionless Onboarding Web Flow / Partner invite): **BACKLOG (P0)**
- `TASK-030` (24/7 Automated Drift Sentinel & Scheduler): **BACKLOG (P1)**
- `TASK-031` (Weekly Sunday Signal Hygiene WhatsApp Generator): **BACKLOG (P1)**
- `TASK-040` (Lightweight Ad-Macro URL & Creative Cash Scorecard): **BACKLOG (P2)**

---

## 5. Next Immediate Directives for Opus 5.5

When you pick up this handoff, direct your implementers (**Sonnet 5.5, Gemini 3.8, Haiku 5.5**) to execute the next active sprint tasks:

1. **Direct Sonnet 5.5 on `TASK-011`:**
   - Build the lightweight onboarding receiver: endpoints to accept Shopify/Salla webhook registrations and Meta Partner asset IDs.
2. **Direct Gemini 3.8 / Haiku 5.5 on `TASK-030`:**
   - Implement the background scheduler (APScheduler or systemd timer) for the 24/7 Drift Sentinel: periodic container linting and synthetic sniffer check.
3. **Direct Sonnet 5.5 on `TASK-031`:**
   - Build the Sunday WhatsApp Signal Hygiene report formatter: queries delivered vs undelivered orders from the database and generates the client WhatsApp digest.
