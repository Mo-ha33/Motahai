# 02. Tier 1: "The Solo Fixer" (باقة الموظف المنقذ — 1 AI Employee)

> **Status (2026-10-10):** this is a sales and packaging draft, not a product spec. Anything marked _Not built_ has
> no implementation in this repository and must not be promised to a client. Motahai's built server-side product is
> the Meta CAPI COD signal ladder (Rule D-005: `ConfirmedOrder` and `DeliveredPurchase`). Check
> `docs/MOTAHAI_CORE_FOUNDATION.md` section 6 before quoting any figure.

## 1. Overview & Positioning Hook
- **Tier Name:** The Solo Fixer (`باقة الموظف المنقذ`)
- **Assigned AI Employee:** `Auto-Fix Engineer` (`AF` / `auto-fix-engineer`) operating in **Full-Stack Solo Mode** (supervised directly by Hermes).
- **Core Value Proposition:** Immediate, non-disruptive, autonomous repair of broken e-commerce analytics, duplicate tracking beacons, and consent mode violations without hiring expensive agencies.
- **Positioning Hook (English):**  
  > *"Hire a 24/7 dedicated AI Tracking Engineer for less than the cost of a 1-hour freelance consultation call."*
- **Positioning Hook (Arabic):**  
  > *"وظّف مهندس تتبع رقمي بالذكاء الاصطناعي يعمل معك 24/7 بتكلفة أقل من ساعة استشارة واحدة مع خبير تتبع تقليدي."*

---

## 2. Ideal Customer Profile (ICP)
- **Target Persona:** Solo e-commerce merchants, dropshippers, and boutique direct-to-consumer (DTC) brands.
- **Platforms Supported:** Shopify, WooCommerce, Salla, Zid, Custom Next.js / Webflow.
- **Ad Spend Profile:** \$500 – \$3,000 / month on Meta, Google, or TikTok Ads.
- **Trigger Pain Points:**
  - Google Consent Mode v2 warning banners in Google Tag Manager / GA4.
  - Purchases firing twice (double-counting conversions in Ads Manager).
  - Missing transaction revenue or zero currency attributes in GA4 ecommerce reports.
  - Fear of non-compliance with Egyptian Personal Data Protection Law (PDPL 151/2020) and GDPR PII leakage.

---

## 3. Product Deliverables & Technical Capabilities

### A. Autonomous Browser Audit
- Utilizes headless browser automation to navigate client URLs (Home, Product Detail Page, Cart, Checkout).
- Intercepts `window.dataLayer` pushes and flags unstandardized event names (e.g., camelCase `addToCart` instead of GA4 standard `add_to_cart`).
- Inspects client-side cookie declarations and consent banners.

### B. Sanitized GTM Container Generation
- Ingests the client's current GTM Container JSON export (or generates a fresh pre-configured container from scratch).
- Applies automated regex rules to scrub Personal Identifiable Information (PII) such as phone numbers, emails, and full street addresses before tags fire.
- Injects standard consent initialization triggers (`analytics_storage`, `ad_storage`, `ad_user_data`, `ad_personalization`) defaulted to `denied` until user consent is granted.
- Generates a hardened, download-ready `GTM-XXXXXXX_sanitized.json` export.

### C. Client DataLayer Taxonomy & Handoff Guide
- Generates an auto-documented Markdown/Notion cheat sheet specifying required DataLayer objects for the store's development team or Shopify theme liquid files.
- Step-by-step visual installation manual tailored to the merchant's platform.

---

## 4. Operational Boundaries & Feature Matrix

| Capability | Included in Tier 1 | Tier 2 / 3 Upgrade Required |
|---|:---:|:---:|
| **Assigned AI Employee** | 1 (`Auto-Fix Engineer`) | 3 or 8 Swarm |
| **Max Monitored Stores / Domains** | 1 Store | Up to 5 Stores |
| **GTM Container Sanitation & Fix** | ✅ Yes (Browser Container) | ✅ Yes |
| **Consent Mode v2 & Egyptian PDPL** | ✅ Yes (Client-side) | ✅ Yes |
| **DataLayer Taxonomy Sheet** | ✅ Yes | ✅ Yes |
| **Server-Side Tracking (CAPI)** | ❌ No | ✅ Tier 2: Meta CAPI COD signal ladder (server-side GTM _Not built_) |
| **Match Keys (EMQ inputs)** | ❌ No | ✅ Tier 2: hashed identifiers and click ids; no EMQ score target |
| **Real-Time Network Sniffing & Alerts** | ❌ No | ✅ Included in Tier 3 |
| **GA4 Funnel Attribution & BI Analytics** | ❌ No | ✅ Included in Tier 3 |
| **CRO & A/B Experimentation Design** | ❌ No | ✅ Included in Tier 3 |

---

## 5. Commercial Pricing & Terms

- **Monthly Subscription:** **\$99 / month** (or **399 SAR** / **4,950 EGP**).
- **Annual Subscription (20% Off):** **\$79 / month** billed annually at **\$948 / year** (or **3,190 SAR** / **39,500 EGP**).
- **One-Time Emergency Rescue Sprint:** **\$149 one-time** (Includes 7-day access to Auto-Fix Engineer for an immediate single-store tracking overhaul).
- **Setup Fee:** \$0 (100% Autonomous self-service onboarding).
- **Cancellation Policy:** Cancel anytime with 1-click inside Wesam.ai dashboard.

---

## 6. The "Land-and-Expand" Transition Path
Tier 1 is deliberately structured as an irresistible entry point ("Trojan Horse"). Once the merchant’s client-side tracking is pristine, the Auto-Fix Engineer's handoff report automatically highlights:
> *"Your browser container is now hardened and 100% error-free. However, due to Safari ITP and ad-blockers, you are losing an estimated 18-24% of your Meta/Google conversion signals. To capture these lost orders, hire the Core Growth Trio (Tier 2) to activate Server-Side CAPI today."*
