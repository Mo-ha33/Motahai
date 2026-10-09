# 03. Tier 2: "The Core Growth Trio" (باقة فريق التتبع والأداء — 3 AI Employees)

## 1. Overview & Strategic Role
- **Tier Name:** The Core Growth Trio (`باقة فريق التتبع والأداء`)
- **Assigned AI Employees:**
  1. `Lead GTM Orchestrator` (`LO` / `lead-gtm-orchestrator`) — Supervisor & Workflow Dispatcher.
  2. `Auto-Fix Engineer` (`AF` / `auto-fix-engineer`) — Container Sanitation & Code Injection Specialist.
  3. `Pixel & CAPI Specialist` (`PC` / `pixel-capi-specialist`) — Attribution, Consent Mode & Server-Side Cloud Gateway Specialist.
- **Core Value Proposition:** End-to-end attribution recovery across both client-side and server-side infrastructures. Fixes signal degradation, optimizes Meta Event Match Quality (EMQ), and prevents double-firing ads through guaranteed deduplication.
- **Positioning Hook (English):**  
  > *"Stop burning 20% of your ad spend on blind algorithms. Hire a dedicated 3-agent tracking department to restore Meta CAPI, maximize EMQ, and automate server-side attribution."*
- **Positioning Hook (Arabic):**  
  > *"لا تترك خوارزميات الإعلانات تعمل في الظلام وتفقد 20% من مبيعاتك. وظّف فريقاً مؤلفاً من 3 خبراء ذكاء اصطناعي لإدارة التتبع السحابي (CAPI)، ورفع كفاءة التوفيق الإعلاني (EMQ) وتفادي التكرار نهائياً."*

---

## 2. Ideal Customer Profile (ICP)
- **Target Persona:** High-growth e-commerce brands, performance marketing leads, and media buyers.
- **Monthly Paid Ad Spend:** \$3,000 – \$20,000 / month across Meta (Facebook & Instagram), Google Ads, TikTok, and Snapchat.
- **Typical Platforms:** Shopify Plus, Magento / Adobe Commerce, Salla / Zid Pro, WooCommerce.
- **Critical Business Pain Points:**
  - High Cost Per Acquisition (CPA) caused by ad platform pixel under-reporting (iOS 14.5+ Safari ITP privacy blocking).
  - Low Meta Event Match Quality (EMQ score < 6.0/10), leading to expensive ad delivery and poorly optimized Lookalike/Retargeting audiences.
  - Server-side setup complexity (Cloudflare Workers, Stape, AWS ECS, GCP Cloud Run) that marketing teams cannot configure without an engineering department.
  - Frequent silent tracking breakages occurring after shopify theme updates or developer code changes.

---

## 3. The 3-Agent Collaborative Workflow

```
                                  ┌────────────────────────────────┐
                                  │     LEAD GTM ORCHESTRATOR      │
                                  │  (Receives Job & Dispatches)   │
                                  └───────────────┬────────────────┘
                                                  │
                         ┌────────────────────────┴────────────────────────┐
                         ▼                                                 ▼
            ┌──────────────────────────┐                      ┌──────────────────────────┐
            │    AUTO-FIX ENGINEER     │                      │  PIXEL & CAPI SPECIALIST │
            │ (Browser Tags, PII regex,│                      │  (Cloud Gateway, Server  │
            │  Consent Mode v2)        │                      │   CAPI, Deduplication)   │
            └────────────┬─────────────┘                      └────────────┬─────────────┘
                         │                                                 │
                         └────────────────────────┬────────────────────────┘
                                                  │
                                                  ▼
                                  ┌────────────────────────────────┐
                                  │   HERMES SUPERVISOR VERDICT    │
                                  │   (Automated QA & Deployment)  │
                                  └────────────────────────────────┘
```

1. **Lead GTM Orchestrator:**
   - Scans store performance, schedules recurring health jobs, validates client requirements, and coordinates work streams between agents.
2. **Auto-Fix Engineer:**
   - Cleans browser-side GTM containers, configures standardized dataLayer triggers, removes PII, and applies consent default states.
3. **Pixel & CAPI Specialist:**
   - Provisions and configures Server-Side GTM containers (or Cloudflare Worker / AWS Stape gateways).
   - Generates and enforces unique `event_id` keys shared between browser tags and server payload requests.
   - Sets up advanced user matching parameters (hashed `em`, `ph`, `fn`, `ln`, `ct`, `zp`, `fbp`, `fbc`).

---

## 4. Key Performance Indicators & SLA Commitments

| Target Metric | Baseline / Typical Client | Guaranteed Trio Outcome |
|---|:---:|:---:|
| **Meta Event Match Quality (EMQ)** | 4.0 – 6.2 / 10 | **8.5+ / 10** |
| **CAPI vs. Browser Deduplication Rate** | 60% – 80% (or unlinked) | **> 98.5%** |
| **Consent Mode v2 Compliance** | Partially missing / Unverified | **100% Certified v2 Integration** |
| **Signal Recovery (Safari/ITP)** | +0% | **+18% to +26% Tracked Conversions** |
| **Tracking Health Check Frequency** | Occasional / Never | **Daily Automated Health Check** |

---

## 5. Commercial Pricing & Margin Architecture

- **Monthly Subscription:** **\$449 / month** (or **1,699 SAR** / **22,500 EGP**).
- **Quarterly Commitment (10% Off):** **\$399 / mo** billed at **\$1,197 / quarter**.
- **Annual Subscription (20% Off):** **\$359 / mo** billed at **\$4,308 / year** (or **14,400 SAR** / **189,000 EGP**).
- **Unit Economics Snapshot:**
  - Wholesale Wesam.ai Cost: \$45.00 (3 seats × \$15)
  - Cloud Compute & Token Allocation: \$36.00
  - Net Gross Margin: **81.9%** (\$368.00 gross profit per active customer/month).

---

## 6. Real ROI Calculation & Pitch Deck Formula
When communicating with performance marketers, present this simple math:
> **For an e-commerce brand spending \$10,000/month on Meta Ads:**
> - Loss without Server-Side CAPI: ~20% of purchases never attributed to the ad platform (\$2,000 in untracked ad performance data).
> - Algorithmic Penalty: Because Meta doesn't know who bought, lookalike models deliver to colder prospects, driving CPA up by ~15-25%.
> - **The Math:** By investing **\$449/mo** in the Core Growth Trio, you restore full attribution signal, recover 20% more conversion events, and lower blended CPA by 12–18%.
> - **Net ROI:** The package pays for itself within the first 7 days of ad campaign optimization.
