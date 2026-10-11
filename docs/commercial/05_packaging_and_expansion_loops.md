# 05. Commercial Conversion, Expansion Loops & Packaging Mechanics

> **Status (2026-10-10):** this is a sales and packaging draft, not a product spec. Anything marked _Not built_ has
> no implementation in this repository and must not be promised to a client. Motahai's built server-side product is
> the Meta CAPI COD signal ladder (Rule D-005: `ConfirmedOrder` and `DeliveredPurchase`). Check
> `docs/MOTAHAI_CORE_FOUNDATION.md` section 6 before quoting any figure.

## 1. The "Trojan Horse" Customer Acquisition & Expansion Flywheel

To minimize Customer Acquisition Cost (CAC) and maximize Lifetime Value (LTV), Motahai employs a frictionless **4-stage expansion engine**:

```
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Stage 1: Free 60-Second "Leakage Diagnostic" (Zero Friction Hook)      │
 │ Automated Browser Inspection scans store & highlights \$ Lost in Ads   │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Stage 2: Land with Tier 1: Solo Fixer (\$99/mo or \$149 Sprint)         │
 │ Auto-Fixes Consent Mode v2, duplicate purchases, and DataLayer errors  │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Stage 3: Expansion to Tier 2: Core Growth Trio (\$449/mo)              │
 │ Unlocks Meta CAPI, restores Safari ITP loss (+20% tracked conversions) │
 └───────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Stage 4: Retention & Scale with Tier 3: Autonomous Dept (\$1,499/mo)   │
 │ Full Swarm covers BigQuery BI, continuous DevTools QA, and CRO lifts   │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Expansion Trigger Points & Automated In-Product Nudges

| Lifecycle Event | In-App / Email Automated Trigger | Recommended Action | Expected Conversion |
|---|---|---|:---:|
| **Post-Fix Completion (Tier 1)** | *"Your browser tags are now clean. However, 22% of iOS traffic is still blocked by Safari ITP. Activate Server-Side CAPI to recover \$1,800/mo in untracked sales."* | Upgrade to **Tier 2 (Growth Trio)** | 28% Upgrade Rate |
| **Ad Spend Crossing Threshold** | Monthly ad spend detected or logged > \$10,000/mo. Lead Orchestrator flags high opportunity cost of untracked funnels. | Upgrade to **Tier 3 (Full Swarm)** | 18% Upgrade Rate |
| **New Domain / Store Launch** | Merchant attempts to add a second brand, international store, or regional localized sub-store. | Add **Additional Store License** (+\$149/mo) | 45% Attach Rate |
| **Peak Sales Season (Black Friday / Ramadan)** | Proactive alert: *"High traffic incoming! Stress-test your dataLayer and activate QA Network Sniffer to prevent checkout tracking crashes."* | Activate **Seasonal Surge Pack** | 35% Attach Rate |

---

## 3. Commercial Add-On Catalog & Revenue Maximizers

| Add-On Item | Description | Pricing Model | Gross Margin |
|---|---|---|:---:|
| **Additional Store License** | Connect an extra e-commerce store domain to an existing Tier 2 or Tier 3 plan. | **+\$149 / store / month** | 88% |
| **Legacy Tag Migration Sprint** | One-time deep refactoring of bloated, spaghetti custom JavaScript tags into modern modular GTM standards. | **\$299 one-time** | 75% |
| **Hermes Executive HITL Strategy Call** | 45-minute live screen-share strategy consultation with Ameen Digital senior technical partner. | **\$250 / session** | 60% |
| **Dedicated Private VPS Node** | Isolated Hermes & Wesam compute container for strict SOC2/Enterprise data isolation. | **+\$400 / month** | 70% |
| **Compute Token Booster Pack** | Extra 10 Million LLM tokens + 100 automated browser runs for high-frequency stores. | **\$29 / pack** | 80% |

---

## 4. Retention & Churn Prevention Playbook

1. **The Monthly "Attribution Value Proof" Certificate:**
   - On the 1st of every month, Lead Orchestrator generates an automated Executive Attribution Report.
   - Highlights: Exact number of orders recovered by CAPI that browser pixels missed, exact duplicate tags suppressed, and estimated ad spend saved.
   - *Outcome:* Makes cancelling the subscription psychologically painful because the business value is explicitly quantified in dollars.
2. **24/7 Silent Sentinel Alerts:**
   - Whenever an external developer updates a Shopify liquid template or an app injects conflicting JavaScript, Motahai immediately alerts the merchant on Slack/WhatsApp and offers a 1-click autonomous rollback.
3. **Annual Prepayment Incentive:**
   - Offer 2 months completely free (20% discount) upon checkout to lock in 12-month annual contracts, dramatically driving down churn to < 2.5% monthly.
