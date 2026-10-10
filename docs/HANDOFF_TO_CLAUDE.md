# Motahai handoff: current state

**Repo:** `D:\GTM AI Crwue` (GitHub `Mo-ha33/Motahai`)
**Baseline:** HEAD `3a8e0b1`, 509 tests passing. A wave is in progress on top of it (uncommitted, see section 3).
**Protocol:** Opus is reviewer/orchestrator. One orchestrator writes to this folder at a time. No commit or push without the user's say-so.

Ground truth below was checked against `src/ameen_workforce/` at 3a8e0b1. Where this file and the code disagree, the code wins.

---

## 1. What the code does today

### Signal ladder (Rule D-005, "Coexist")
- The merchant's native Shopify/Salla `Purchase` is untouched. Motahai never sends `Purchase`.
- **ConfirmedOrder**: `event_id = confirmed_<order_id>`, `event_time` = confirmation time, sent immediately, value = order total.
  - Sources (`confirmation.py`): an exact merchant tag (case-insensitive; defaults `confirmed`, `cod-confirmed`, `order-confirmed`, `مؤكد`, `تم التأكيد`); per-tenant Salla statuses (`tenants.confirmation_rules.statuses`, **empty by default**); implicit on ship/paid (`implicit_on_ship`, default on); manual `emit_confirmed_order()` (call centre / WhatsApp bot).
  - Salla `in_review` / `under_review` (and their Arabic names) do **not** confirm.
  - Cancellation tags and cancelled/refunded/voided orders never confirm.
  - Every delivered or paid order gets its ConfirmedOrder first if it has none (funnel invariant).
- **DeliveredPurchase**: `event_id = delivered_<order_id>`, `event_time` = **order placed time**.
  - Value = net collected. Shopify: order total minus successful refunds (S2-4). Salla: no refund netting yet (TODO).
  - Sent at `delivered_at + settlement_hours` (tenant setting, default 12 h), capped at `placed + 6.5 d - 1 h`.
  - Past `placed + 6.5 d` the event becomes `late_delivery` and is never sent. Net value <= 0 is suppressed.
  - Delivered means: COD + Shopify `financial_status=paid` and fulfilled; Salla `delivered`/`completed`; Bosta state 45; OTO delivered status. Prepaid: paid.

### Privacy and retention
- D-006: checkout IP and user agent are Fernet-encrypted (`checkout_context`). They are purged after a successful live DeliveredPurchase, or 14 days after capture (scheduler job). A ConfirmedOrder does not purge.
- Customer email and phone are stored only as SHA-256 hashes.

### Approvals (D-003, Ed25519)
- `POST /approvals/gtm-publish` (operator bearer key) issues a token bound to one container and one workspace. TTL 30 min, single-use nonce.
- Tokens are Ed25519-signed (`hitl_tokens.py`). The Core issuer holds only the private key, read from the 0600 PEM at `HITL_SIGNING_PRIVATE_KEY_PATH`. The Hermes/MCP verifier holds only the public key (`HITL_VERIFY_PUBLIC_KEY` as base64url, or `HITL_VERIFY_PUBLIC_KEY_PATH` as PEM) and never sees a private key.
- Keypair: `python -m ameen_workforce.hitl_tokens generate-keypair --out <dir>`.
- No HMAC fallback. `HITL_SIGNING_KEY` is removed. Treat any old copy of it as exposed and rotate.
- Isolation manifests (Core and Hermes on separate hosts or containers): `deployment/isolation/` (README, docker-compose, systemd units).

### Courier webhooks
- `POST /webhooks/bosta/{shop_domain}`: the Authorization header must match the tenant's secret.
- `POST /webhooks/oto/{shop_domain}`: HMAC over `orderId:status:timestamp`.
- Secrets are per-tenant credentials `bosta_webhook_secret` / `oto_webhook_secret`, Fernet-encrypted. There is no global env secret. A tenant without one gets 401.
- Unknown shop gets 404. Nothing touches orders before authentication succeeds.

### Storefront capture
- `POST /v1/capture/{tenant_key}` (`tenant_key` = Shopify `shop_domain` or Salla merchant id). It writes only to `pending_captures`, never to `orders`.
- The scheduler merges captures every 15 min, only when the order total matches (max(1%, 1.0) tolerance) and the tenant is the same. Only empty order fields are filled.
- CORS: the tenant's storefront origin only. Body capped at 4 KB. Rate limit 30/min per IP and 600/min per tenant, held in process memory.
- Snippets: `storefront/salla/salla_capture.js` (posts to capture and attaches order notes) and `storefront/shopify/` (cart attributes).
- Removed and tested as gone: `/storefront/capture`, `/webhooks/salla/capture`.

### Scheduler
- `python -m ameen_workforce.scheduler` runs one daemon process (PYTHONPATH must include `src/`). Each 15-min cycle runs: `send_due_events`, `retry_failed_events`, purge of expired checkout context, `merge_pending_captures`, and a heartbeat.
- `--check` exits non-zero when the last successful cycle is older than 45 min.
- Production unit: `deployment/systemd/motahai-scheduler.service` (user `motahai`, not root). Notes: `deployment/systemd/README_scheduler.md`.
- The in-app loop runs only with `MOTAHAI_RUN_SCHEDULER_IN_APP=1`, and only with a single uvicorn worker.

### Onboarding CLI: `scripts/onboard_store.py`
- Secrets come from `--secrets-file PATH` (KEY=VALUE; refused on POSIX unless mode 600), `--secrets-stdin`, or `<flag> -` (hidden prompt on a TTY). Secret flags on argv still work but print a warning.
- Keys: `META_CAPI_TOKEN`, `WEBHOOK_SECRET`, `BOSTA_WEBHOOK_SECRET`, `OTO_WEBHOOK_SECRET`.
- `--generate-courier-secrets` creates strong courier secrets and prints them once with their webhook URLs.
- `--verify-capi-ping` requires `--test-event-code` and sends only `MotahaiConnectionTest`, never `DeliveredPurchase`.
- Needs `MOTAHAI_FERNET_KEY` for any run that is not `--dry-run`.

```bash
python scripts/onboard_store.py --name "Pilot Store 1" --platform shopify \
  --shop-domain pilot1.myshopify.com --meta-dataset-id 123456789012345 \
  --secrets-file ~/motahai/pilot1.env --mode shadow \
  --verify-capi-ping --test-event-code TEST12345
```

---

## 2. Known gaps (do not report these as done)

- **No Alembic yet.** `create_all()` never alters existing tables. Write migrations before upgrading any non-empty database.
- **Salla is unverified on a real store.** The thank-you page selectors, the `order.status.updated` payload shape, and order-total parity are inferred from docs. Refund payloads are not netted.
- **Meta effects are unproven.** ConfirmedOrder optimization and category effects are pilot `[Test]` items.
- **nginx X-Forwarded-For.** `deployment/nginx/employees.motahai.com.conf` currently sets `$proxy_add_x_forwarded_for`, which lets a client spoof the first hop. Change it to `proxy_set_header X-Forwarded-For $remote_addr;` before `MOTAHAI_TRUSTED_PROXY=1` is set anywhere. While the flag is unset, capture ignores the forwarded header.
- **Webhooks are acked before processing.** A task lost between the 200 and the commit is not redelivered. The reconciliation sweep is the safety net.
- **Refuser audience export is manual.** `audiences.export_tenant_audiences` exists but no scheduled job calls it.
- **Courier coverage.** Bosta and OTO only. Torod and SMSA are not implemented.

---

## 3. In progress (this wave; other agents are editing these)

- Sunday Signal Hygiene digest (S1-4, `digest.py`): being added by another agent. Not yet in the committed tree, and not scheduled.
- Scheduler, `db.py`, `service.py` changes for this wave.
- Pilot handbook (`docs/pilot/media_buyer_handbook.md`) and `scripts/run_pilot_simulation.py`.
- Do not assume any of these are landed until the test run confirms them.

---

## 4. Verify

```bash
python -m pytest -q
```

Baseline at 3a8e0b1: 509 passed. The onboarding module tests are in `tests/test_onboarding_cli.py`.
