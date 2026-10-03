# MEMORY — Hot Index (always loaded)

> Budget: keep this file **< 3 KB**. It is injected into every session. Detail lives in
> `memories/*.md` and is read on demand with `hermes_memory_read` / file read.
> Some Hermes builds enforce a hard character cap on MEMORY.md — check yours
> (`hermes doctor` / config) and stay well under it.

## Write rules
1. Only verified facts. Each line ends with `[src: <evidence ref> | <YYYY-MM-DD>]`.
2. No secrets, tokens, customer PII, or raw order data. Ever.
3. Hypotheses go in `memories/incident-log.md` marked `HYPOTHESIS`, never here.
4. One fact per line. Replace stale lines; do not append contradictions.
5. Writers: Hermes (after a verified verdict) and the human operator. Wesam agents
   propose via `memory_write` in the Verdict Envelope; Hermes decides.

## Where things live
| File | Contents | Read when |
|---|---|---|
| `memories/client-ledger.md` | Per-client containers, stores, gateways, fingerprints, open issues | Any client-specific task |
| `memories/gtm-patterns.md` | Battle-tested patterns & gotchas (Shopify, CAPI, EGP, consent, sGTM) | Designing or reviewing any fix |
| `memories/incident-log.md` | Incidents, root causes, remediations | Drift alerts, regressions, "seen this before?" |
| `skills/*/SKILL.md` | Executable procedures | Before running a skill |

## Active clients (one line each — detail in client-ledger)
- `<client-slug>` — GTM container `100000001` (`GTM-<<REQUIRED>>`), platform `<<REQUIRED>>`, health `—`, open criticals `—` [src: operator brief | <<date>>]

## Standing decisions (الفتاوى المعتمدة)
- D-001: Purchase is deduped on Shopify order ID (`order.id` / `checkout.order.id`), never on cart token. [src: SOUL §3.1 | 2026-10-03]
- D-002: Browser + server share one `event_id`: purchase = `purchase_<order_id>` (deterministic); other events = UUID generated once in the dataLayer push. [src: gtm-patterns P2 | 2026-10-03]
- D-003: Hermes never publishes GTM versions or Shopify theme changes; humans publish after QA. [src: SOUL §4 | 2026-10-03]
- D-004: Container audits require a `gtm-container-linter` report as evidence. [src: SOUL §4 | 2026-10-03]

## Last updated
2026-10-03 — initial schema (operator)
