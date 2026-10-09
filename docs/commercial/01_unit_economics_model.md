# 01. Motahai AI Workforce — Unit Economics & Cost-to-Serve Model

## 1. Executive Summary
Motahai AI Workforce operates as a managed autonomous AI workforce layer on top of **Wesam.ai** (Seat/Employee orchestration) and **Hermes** (VPS supervisor via Model Context Protocol - MCP). 

To ensure sustainable commercial viability and scale, our unit economics are engineered around three core principles:
1. **Target Gross Margin > 70%** across all subscription tiers.
2. **Predictable Cost-to-Serve (COGS)** by leveraging dual-speed LLM routing (low-cost reasoning for standard audits, deep reasoning for complex GTM sanitation).
3. **Strict Fair Usage Policy (FUP)** and automated compute throttling to prevent malicious or accidental token spikes.

---

## 2. Infrastructure & Cost Component Breakdown (Monthly Basis per Client)

| Cost Component | Provider / Layer | Unit Cost Formula | Estimated Monthly Burn (Tier 1) | Estimated Monthly Burn (Tier 2) | Estimated Monthly Burn (Tier 3) |
|---|---|---|---|---|---|
| **Wesam.ai Seat Licenses** | Wesam Platform | \$15 / employee / mo (Wholesale) | \$15.00 (1 seat) | \$45.00 (3 seats) | \$120.00 (8 seats) |
| **Hermes MCP Dedicated VPS** | Hostinger / Contabo | Amortized across multi-tenants (~$2.50/client) | \$2.50 | \$4.00 | \$10.00 (Dedicated Slice) |
| **LLM Inference Tokens** | Google Gemini (Flash & Pro) / Anthropic Sonnet | Token usage per job (Input \$0.10/M, Output \$0.40/M avg Flash; Pro/Sonnet for complex fixes) | \$4.50 (approx. 8M tokens) | \$18.00 (approx. 25M tokens) | \$65.00 (approx. 90M tokens) |
| **Headless Browser Automation** | Cloudflare Browser MCP / Browserbase | \$0.05 / browser run (Inspection & Sniffing) | \$3.00 (60 runs) | \$12.50 (250 runs) | \$45.00 (900 runs) |
| **Storage & Logging (GTM/HAR/Artifacts)** | Cloudflare R2 / S3 | Storage + Egress | \$0.50 | \$1.50 | \$5.00 |
| **Total Estimated COGS (Cost of Goods Sold)** | — | — | **\$25.50** | **\$81.00** | **\$245.00** |

---

## 3. Pricing Matrix & Margin Realization

### A. USD Tier Pricing & Gross Margins
| Tier Name | Assigned AI Employees | Retail Price (Monthly) | Annual Prepay (Monthly Equiv - 20% off) | Monthly COGS | Gross Profit (\$) | Gross Margin (%) |
|---|---|---|---|---|---|---|
| **Tier 1: Solo Fixer** | 1 (`Auto-Fix Engineer` or Solo Orchestrator) | **\$99 / mo** | \$79 / mo | \$25.50 | **\$73.50** | **74.2%** |
| **Tier 2: Core Growth Trio** | 3 (`Orchestrator` + `Auto-Fix` + `Pixel/CAPI`) | **\$449 / mo** | \$359 / mo | \$81.00 | **\$368.00** | **81.9%** |
| **Tier 3: Autonomous Department** | 8 (Full Swarm) | **\$1,499 / mo** | \$1,199 / mo | \$245.00 | **\$1,254.00** | **83.6%** |
| **Tier 3 (Agency Multi-Store Pack)** | 8 Swarm + 5 Store Licenses | **\$2,499 / mo** | \$1,999 / mo | \$510.00 | **\$1,989.00** | **79.6%** |

### B. Regional Currency Normalization (GCC & Egypt)
To capture maximum regional market share in the MENA e-commerce ecosystem, localized standard pricing applies:

| Tier | USD Retail | Saudi Riyal (SAR) Retail | Egyptian Pound (EGP) Retail |
|---|---|---|---|
| **Tier 1: Solo Fixer** | \$99 / mo | **399 SAR / mo** | **4,950 EGP / mo** |
| **Tier 2: Core Growth Trio** | \$449 / mo | **1,699 SAR / mo** | **22,500 EGP / mo** |
| **Tier 3: Autonomous Dept** | \$1,499 / mo | **5,699 SAR / mo** | **74,900 EGP / mo** |
| **Agency 5-Store License** | \$2,499 / mo | **9,499 SAR / mo** | **124,900 EGP / mo** |

*Exchange Rate Assumption: 1 USD ≈ 3.75 SAR ≈ 49.50 EGP.*

---

## 4. Fair Usage Policy (FUP) & Throttling Limits

To prevent compute abuse and guarantee high availability, each tier enforces soft and hard monthly operational quotas:

| Operational Metric | Tier 1: Solo Fixer | Tier 2: Core Growth Trio | Tier 3: Autonomous Dept |
|---|---|---|---|
| **Active Monitored Domains** | 1 Domain | 1 Domain (Primary + Subdomains) | 1 Domain (or up to 5 for Agency) |
| **Automated Browser Audit Runs** | 60 runs / month (~2/day) | 300 runs / month (~10/day) | 1,200 runs / month (~40/day) |
| **Container Sanitizations / Exports** | 10 releases / month | 40 releases / month | Unlimited (fair use max 200) |
| **CAPI Event Parity Verifications** | Not Included | Daily Automated Check | Continuous Real-Time Sniffing |
| **Max LLM Tokens Allowed** | 15M tokens / month | 45M tokens / month | 150M tokens / month |
| **Overage Token Pack** | \$15 per 5M tokens | \$15 per 5M tokens | \$10 per 5M tokens |

---

## 5. Dual-Speed LLM Cost Optimization Engine
Motahai utilizes an intelligent router (configured in `ops/hermes/llm-router`) to minimize inference costs while maintaining elite accuracy:

```
                  ┌──────────────────────────────┐
                  │    Incoming Agent Request    │
                  └──────────────┬───────────────┘
                                 │
                 Is task structural / routine?
                 (e.g., Syntax checks, formatting,
                  beacon presence, alert logs)
                                 │
                   ┌─────────────┴─────────────┐
                  YES                          NO
                   ▼                           ▼
        ┌─────────────────────┐     ┌─────────────────────┐
        │  Gemini 2.0 Flash   │     │  Claude 3.5 Sonnet  │
        │  (Fast & Cheap)     │     │  or Gemini Pro      │
        │  Cost: $0.10 / 1M   │     │  Cost: $3.00 / 1M   │
        └─────────────────────┘     └─────────────────────┘
```

By routing **80%** of network verification and formatting tasks through Gemini Flash and reserving Claude 3.5 Sonnet / Gemini Pro for the remaining **20%** (complex code remediation, GTM regex deduplication, and strategic BI analysis), average token cost is kept under **\$0.70 / Million tokens blended**.
