# Motahai AI Workforce — Commercial Pricing & Packaging Playbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish the definitive Commercial Pricing, Packaging, and Unit Economics Playbook for the **Motahai AI Workforce** (`employees.motahai.com`), powered by Wesam.ai and Hermes MCP, structured across 3 tiers (Solo Fixer, Core Growth Trio, Full 8-Agent Swarm).

**Architecture:** A comprehensive commercial framework combining Per-Employee / Per-Seat subscription mechanics, cloud compute & token cost-to-serve models, value-based pricing tiers, customer expansion loops (LTV expansion), and sales enablement battlecards.

**Tech Stack:** Wesam.ai Platform (Seat Management), Hermes Autonomous Agent (VPS MCP Hub), Claude/Gemini LLM token routing, GTM/Analytics tracking stack.

**Spec:** `AGENTS_SPECIFICATION.md`, `ops/hermes/sop/WESAM_AGENT_SOP.md`, `ARCHITECTURE.md`.

## Global Constraints
- **Platform Pricing Alignment:** Must strictly respect Wesam.ai's underlying per-seat/per-employee model while packaging high-margin value offerings for end-clients.
- **Audience Scope:** E-commerce stores (Shopify, WooCommerce, Salla, Zid, Custom Next.js/Vercel) in GCC & Egypt, plus performance marketing agencies.
- **Compliance Regime:** Egyptian PDPL Law 151/2020, GDPR, Google Consent Mode v2, and Meta CAPI EMQ > 8.0 standards.
- **Language / Localization:** Dual English & Arabic commercial positioning hooks, tier names, and objection-handling scripts.

## Review Focus
1. **Margin Safety under High Token Bursts:** Heavy browser automation (Cloudflare MCP / Browserbase) and recursive container repairs could consume unexpected LLM tokens; verify guardrails and usage buffers per tier.
2. **Cannibalization Prevention:** Ensure Tier 1 (1 Agent) solves urgent pain points cleanly but naturally exposes the need for Tier 2 (CAPI/Pixel) without frustrating the customer.
3. **Agency Reseller vs Direct Merchant Pricing:** Differentiate single-store licenses from agency multi-tenant client management.
4. **Wesam Platform Seat Fees vs Motahai Customer Pricing:** Ensure retail prices sustain Wesam platform seat costs with at least 65-75% gross margins.
5. **Human-in-the-Loop (HITL) SLA Clarity:** Distinctly define what is fully autonomous vs what triggers Hermes escalation to human consultants.

---

### Task 1: Unit Economics & Cost-to-Serve Modeling
**Files:**
- Create: `docs/commercial/01_unit_economics_model.md`

**Interfaces:**
- Consumes: Wesam.ai seat licensing costs, Hermes VPS hosting costs, LLM token costs (Gemini Flash / Pro / Claude Sonnet), Browserbase / Cloudflare MCP compute run rates.
- Produces: Base cost-per-agent, compute buffers, target gross margin thresholds (70%+), tier price floors.

- [x] **Step 1: Calculate infrastructure and per-agent operational costs**
  - Wesam.ai per-seat subscription cost.
  - Hermes VPS amortized cost per active client ($20/mo baseline).
  - Browser inspection compute (Playwright / Browserbase sessions).
  - LLM Token consumption estimates for typical monthly workloads (Audit, Remediation, Deduplication).
- [x] **Step 2: Define Gross Margin thresholds and Retail Price Matrix**
  - Document base cost vs selling price for: 1 Seat, 3 Seats, 8 Seats.
  - Establish monthly recurring revenue (MRR) and annual prepayment discount models (e.g., 20% discount on annual).
- [x] **Step 3: Document token throttling & fair usage policy (FUP)**
  - Specify maximum container audits, automated URL crawler runs, and CAPI reconciliation checks per tier per month.

---

### Task 2: Tier 1 "The Solo Fixer" (باقة الموظف المنقذ — 1 AI Employee)
**Files:**
- Create: `docs/commercial/02_tier1_solo_fixer.md`

**Interfaces:**
- Consumes: Unit economics baseline from Task 1, Auto-Fix Engineer specs from `AGENTS_SPECIFICATION.md`.
- Produces: Tier 1 product sheet, pricing, SLA, feature matrix, and onboarding flow.

- [x] **Step 1: Formalize Agent Definition & Full-Stack Solo Mode**
  - Role: `Auto-Fix Engineer` (with lightweight audit capabilities) or `Lead GTM Orchestrator` (Solo Mode).
  - Persona target: Solo dropshippers, micro e-com stores (<$3K/mo ad spend), boutique brands suffering from Consent Mode v2 warnings or broken purchase events.
- [x] **Step 2: Detail Deliverables & Technical Scope**
  - Browser-based automated landing page & checkout audit.
  - Sanitized, production-ready GTM Container JSON export & injection snippet.
  - Automated Egyptian PDPL & Consent Mode v2 default configuration.
  - Standardized DataLayer taxonomy cheat-sheet (PDF / Notion template).
- [x] **Step 3: Price Point, Value Hook & Limitations**
  - Price: $99 – $149 / month (or 4,900 EGP / 399 SAR).
  - Value Hook: *"Hire a 24/7 Senior Tracking Engineer for less than the cost of a 1-hour freelance call."*
  - Strict Boundaries: Single domain, 1 GTM container, browser-side only (no CAPI server-side container orchestration).

---

### Task 3: Tier 2 "The Core Growth Trio" (باقة فريق التتبع والأداء — 3 AI Employees)
**Files:**
- Create: `docs/commercial/03_tier2_growth_trio.md`

**Interfaces:**
- Consumes: Task 1 economics, Agent specs (`Lead Orchestrator`, `Auto-Fix Engineer`, `Pixel & CAPI Specialist`).
- Produces: Tier 2 commercial specification, multi-agent synergy workflow, and client retention strategy.

- [x] **Step 1: Define Swarm Dynamics & Roles**
  - `Lead GTM Orchestrator` (Project Manager & Supervisor).
  - `Auto-Fix Engineer` (Container Sanitation & DataLayer repair).
  - `Pixel & CAPI Specialist` (Server-side tracking, Meta EMQ > 8.0, TikTok/Snap CAPI parity).
- [x] **Step 2: Core Deliverables & High-Value Outcomes**
  - Full Browser + Server CAPI deduplication setup (`event_id` parity > 98%).
  - Multi-platform conversion sync (Meta, Google Ads Enhanced Conversions, TikTok, Snap).
  - Continuous 24/7 health checks with proactive alerting on signal loss.
  - Monthly DataLayer hygiene and consent compliance certificates.
- [x] **Step 3: Pricing, ROI Justification & Positioning**
  - Price: $399 – $549 / month (or 18,900 EGP / 1,499 SAR).
  - Target Persona: Scaling stores spending $3K–$20K/month on paid ads.
  - Value Hook: *"Reclaim 15–25% lost ad attribution and stop burning ad spend on duplicate conversions."*

---

### Task 4: Tier 3 "The Full Autonomous Growth & Tracking Department" (8 AI Employees)
**Files:**
- Create: `docs/commercial/04_tier3_autonomous_department.md`

**Interfaces:**
- Consumes: All 8 agent specs from `AGENTS_SPECIFICATION.md`, Hermes HITL escalation protocol.
- Produces: Tier 3 Enterprise/Agency package, full swarm SLA, multi-domain options, and white-label agency terms.

- [x] **Step 1: Orchestration Architecture of the 8 Agents**
  - Full stack: Lead Orchestrator, DataLayer Architect, Pixel & CAPI Specialist, QA Network Sniffer, Auto-Fix Engineer, Growth BI Analyst, GTM Strategy Lead, CRO & Experimentation Engineer.
  - Role of Hermes VPS as supreme controller and client-ledger keeper.
- [x] **Step 2: Advanced Enterprise Deliverables**
  - Real-time Chrome DevTools network traffic sniffing (detect beacon failures live).
  - Full-funnel GA4 BigQuery export attribution modeling & ROAS analytics.
  - Automated A/B test experiment design & checkout friction diagnostics.
  - Omnichannel acquisition GTM strategy recommendations.
  - Dedicated private MCP connector instance + SLA guarantee (<15 min automated response).
- [x] **Step 3: Pricing, Licensing & Agency White-Label Structure**
  - Price: $1,299 – $1,899 / month (or 65,000 EGP / 4,999 SAR) for brands.
  - Agency Partner Pack: $2,499 / month (up to 5 client stores managed concurrently).
  - Value Hook: *"A complete 8-person elite technical tracking, BI, and CRO department at 1/10th of a single junior hire's salary."*

---

### Task 5: Commercial Conversion, Expansion Loops & Packaging Mechanics
**Files:**
- Create: `docs/commercial/05_packaging_and_expansion_loops.md`

**Interfaces:**
- Consumes: Tiers 1, 2, and 3 specifications.
- Produces: Upgrade triggers, self-serve expansion flywheels, and churn mitigation playbooks.

- [x] **Step 1: Map the "Trojan Horse" Land-and-Expand Strategy**
  - Step 1.1: Free 60-second GTM Audit report generated by QA Sniffer.
  - Step 1.2: Client signs up for Tier 1 ($99) to auto-fix the urgent breaking container errors.
  - Step 1.3: Post-fix report highlights missing server-side CAPI events & low EMQ score -> 1-click upgrade to Tier 2.
  - Step 1.4: As ad spend scales >$20K, client unlocks Tier 3 for CRO, BI, and continuous network sniffing.
- [x] **Step 2: Design Add-Ons & Usage Overage Packs**
  - Extra Store Seat: +$149/mo per additional domain.
  - Rapid Custom Tag Migration Pack (One-time): $299.
  - Hermes Human-in-the-Loop Senior Partner Review: $250 / session.

---

### Task 6: Sales Enablement, Battlecards & Competitive Positioning
**Files:**
- Create: `docs/commercial/06_sales_battlecards_and_scripts.md`

**Interfaces:**
- Consumes: Value propositions from all tiers.
- Produces: Sales scripts, objection handling matrix, and comparison battlecards.

- [x] **Step 1: Competitive Battlecards Matrix**
  - Motahai vs Traditional Tracking Agencies (Speed: 10 mins vs 2 weeks; Cost: 90% cheaper; Uptime: 24/7).
  - Motahai vs Off-the-shelf Shopify Apps (Elevar / Stape / Analyzify): Motahai provides full code remediation, customized tag logic, PDPL compliance, and autonomous AI reasoning rather than rigid black-box plugins.
  - Motahai vs In-House Technical Hires (Instant onboarding, zero payroll taxes, continuous model upgrades).
- [x] **Step 2: Objection Handling & FAQs (Arabic & English)**
  - "Can AI break my checkout?" (Safety sandbox, container draft versioning, zero live injection without explicit approval).
  - "Is client data private?" (Zero-PII scrubbing, local VPS MCP isolation via Hermes, Egyptian PDPL compliance).
  - "Why not just use Stape or Elevar?" (Detailed technical comparison of intelligent remediation vs passive server proxies).
- [x] **Step 3: Outreach Scripts & Discovery Framework**
  - Cold audit outbound messaging (Email & WhatsApp templates).
  - 15-minute Discovery call framework based on "The Mom Test".

---

### Task 7: Master Playbook Synthesis & Executive Summary
**Files:**
- Create: `docs/commercial/MOTAHAI_PRICING_PLAYBOOK.md`

**Interfaces:**
- Consumes: Documents 01 through 06.
- Produces: Complete unified commercial master playbook for Ameen Digital & Wesam.ai leadership.

- [x] **Step 1: Consolidate Master Document**
  - Integrate Executive Summary, 3-Tier Packaging Matrix, Unit Economics Table, Expansion Loops, and Go-To-Market Roadmap into a single master reference document.
- [x] **Step 2: Verification and Quality Review**
  - Check consistency of agent names, pricing figures across SAR/EGP/USD, and platform alignment with Wesam.ai per-seat model.
