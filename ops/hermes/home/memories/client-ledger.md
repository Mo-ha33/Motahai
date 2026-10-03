# Client & Container State Ledger

> Read on demand. One `## <client-slug>` section per client. Facts carry
> `[src | date]`. No secrets, no PII. Unknowns are `<<REQUIRED>>` — never guessed.

---

## Template (copy for each new client)

```markdown
## <client-slug>

### Identity
- Legal/brand name: <<REQUIRED>>
- Primary domains: <<REQUIRED>> (checkout domain if different: <<REQUIRED>>)
- Markets / currencies: <<e.g. EG / EGP; SA / SAR>>
- Consent regime: <<none-required | PDPL-EG | GDPR-EEA (TCF v2.2) | mixed>>
- Owner on client side: <<role only, no personal contact data>>

### Platform
- Platform: <<Shopify (Basic/Grow/Advanced/Plus) | WooCommerce | Salla | Zid | custom>>
- Checkout: <<Shopify Checkout Extensibility | native | custom>>
- Shopify Customer Events: custom pixel name(s) <<...>>, app pixels <<...>>
- Theme: <<name>>, dataLayer source: <<theme snippet | app (name) | Web Pixel>>

### Payments (affects purchase timing)
| Gateway | Flow | Purchase fires when | Known issue |
|---|---|---|---|
| Fawry | Pay-at-outlet / reference code | <<order created (unpaid) | paid webhook>> | Purchase before payment → inflated |
| Paymob | Off-site redirect / iframe | <<thank-you page>> | Self-referral, lost session |
| Tabby / valU | BNPL redirect | <<thank-you page>> | Self-referral |
| COD | Cash on delivery | order created | Revenue ≠ collected; RTO not reversed |

### GTM
| Container | Public ID | Type | Live version | config_fingerprint | Last lint | Health | Crit/High |
|---|---|---|---|---|---|---|---|
| 100000001 | GTM-<<REQUIRED>> | web | <<v>> | <<16 hex>> | <<date>> | <<0-100>> | <<n/n>> |
| <<sGTM id>> | GTM-<<...>> | server | <<v>> | <<...>> | <<date>> | — | — |

### Destinations
- GA4: `G-<<REQUIRED>>` (property <<id>>), unwanted referrals configured: <<yes/no + list>>
- Google Ads: `AW-<<REQUIRED>>`, Enhanced Conversions: <<off | tag | API>>
- Meta: Pixel/Dataset `<<REQUIRED>>`, CAPI via <<Stape | sGTM | Shopify app | none>>, EMQ <<x/10>>
- TikTok / Snap / others: <<...>>
- sGTM host: <<first-party subdomain or same-origin path>>, provider <<Stape | GCP>>

### Open issues (link to incident IDs)
- [ ] INC-<<id>> — <<one line>> (owner: <<agent>>, sev: <<...>>)

### Change history (newest first)
- <<YYYY-MM-DD>> — <<what changed>> — GTM v<<n>> — verdict HV-<<id>>
```

---

## <client-slug-1>

### Identity
- Legal/brand name: <<REQUIRED>>
- Primary domains: <<REQUIRED>>
- Markets / currencies: <<REQUIRED>>

### GTM
| Container | Public ID | Type | Live version | config_fingerprint | Last lint | Health | Crit/High |
|---|---|---|---|---|---|---|---|
| 100000001 | GTM-<<REQUIRED>> | web | <<pending first fetch>> | <<pending>> | — | — | — |

### Open issues
- [ ] First baseline audit: run `gtm-container-linter` on live version, record fingerprint. (owner: Container Sanitation Auditor)
