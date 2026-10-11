# F2: Deploy Motahai Core, isolated from SmartCafe

Status: plan only. Nothing here has been run. Written 2026-10-11 from the repo at the F1 release branch.
Board refs: F2 (this), S3-8 (blocked on F2), F4 (Hermes HITL public key), F3 (alert channel).

Why: Hermes tools `hermes_motahai_tenant_status / signal_health / promote_live` and workflows W6/W7 need a running
Core. Today Hermes returns `CORE_NOT_CONFIGURED` (F1: no default URL, nothing is sent). The shared VPS port
`127.0.0.1:8000` is SmartCafe's backend; Core must never take or touch it.

## 0. Findings from the repo that change the plan

1. Core's default listen port is **58770** (`AMEEN_PORT`, systemd units, nginx template). The Docker manifests use
   8000 inside the container only. Do not use 8000 anywhere on the shared host.
2. The isolation manifests name `CORE_SERVICE_TOKEN` / `CORE_API_BASE_URL`. **The code does not read them.** The
   real names are `HERMES_TENANT_API_KEY` (Core) and `MOTAHAI_TENANT_API_KEY` + `MOTAHAI_CORE_URL` (Hermes). Use
   the real names; fix the example files as a follow-up.
3. `promote-live` is verified **on Core**, not Hermes (`tenant_api.py` calls `verify_action_token`). So Core also
   needs the HITL **public** key (`HITL_VERIFY_PUBLIC_KEY` or `_PATH`) and its own `HITL_USED_NONCES_PATH`. The
   default nonce path is `/home/deploy/.hermes/hitl_used_nonces.json`, which is wrong for Core: set it to
   `/var/lib/motahai/hitl_used_nonces.json`.
4. The nonce store is a JSON file guarded by a per-process lock. With `--workers 2` two processes can race a
   read-modify-write. Run `--workers 1` for the pilot (traffic is tiny); the flip itself is a conditional UPDATE, so
   a double promote is harmless, but keep one worker anyway.
5. `ensure_fernet_key()` (onboarding) **auto-generates a throwaway Fernet key if `MOTAHAI_FERNET_KEY` is unset**,
   which would make stored credentials unreadable after a restart. Always set the key explicitly before first start.
6. Courier webhook URLs printed by onboarding are built from `settings.EMPLOYEES_PORTAL`, hard-coded to
   `https://employees.motahai.com`. The webhook domain decision (section 7) is therefore a code/config change too.
7. SQLite is opened with no WAL and no busy timeout, and the API and scheduler are two processes on one file.
   Prefer Postgres, or at minimum set `PRAGMA journal_mode=WAL` once on the file.
8. `deployment/contabo/deploy_employees.sh` installs nginx and runs certbot on the host it runs on. **Do not run it
   on the shared VPS.** It also installs an incomplete pip set (no sqlalchemy/cryptography); use `requirements.txt`.
9. `/health` is static: it does not touch the database. A green `/health` does not prove DB or keys work; use the
   checks in section 5.
10. `docs/runbooks/DEPLOY_RELEASE_ecfd0f5.md` is referenced by the board but is not in this repo snapshot.

## 1. Hosting options

| | (a) Separate small VPS (recommended) | (b) Same host, fully isolated | (c) Managed platform |
|---|---|---|---|
| Cost | About 5 to 10 USD/month | Free | 5 to 25 USD/month plus managed Postgres |
| Blast radius | Own kernel, own disk, own firewall. SmartCafe incident or Hermes shell compromise cannot reach Core secrets | Shared kernel and disk. A Hermes shell exploit plus local privilege escalation reaches Core. A runaway SmartCafe/Hermes process starves Core | Provider isolation; secrets live in the platform |
| Hermes to Core latency | Public internet, tens of ms; use HTTPS plus the bearer key | Loopback, under 1 ms, no TLS needed | Public internet, tens of ms |
| Webhook ingress (Shopify/Salla/courier, public HTTPS) | Own nginx or Caddy and own cert on the new host. No coordination | Route must live in SmartCafe's nginx. **Coordination risk**: an nginx reload or template change by SmartCafe can drop Core webhooks, and our edit can break SmartCafe | Platform gives HTTPS and a domain out of the box |
| Ops burden | Patch one more box | Lowest, but touches shared infra rules | Lowest; watch egress and cold starts |
| Fits isolation README | Yes ("prefer separate hosts") | Weakest form of it | Yes if no agent runs there |

**Recommendation: (a) a separate small VPS** (1 vCPU, 2 GB RAM, Ubuntu LTS, any EU region close to Contabo). Core
holds the Fernet key, the Ed25519 private key, every tenant's Meta CAPI token and the operator key; it is the
money path. Webhook ingress then needs no change to SmartCafe's nginx, docker or ufw. Use (b) only as a stopgap and
only after the owner and the SmartCafe owner both agree on the nginx route in writing.

## 2. Secrets and env inventory

### Core (file `/etc/motahai/core.env`, 0600 `motahai:motahai`)

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Postgres: `postgresql+psycopg://motahai:...@127.0.0.1:5432/motahai` (install `psycopg[binary]`). SQLite fallback: `sqlite:////var/lib/motahai/motahai.db` |
| `MOTAHAI_FERNET_KEY` | yes | Generate once with `credentials.generate_key()`; back it up offline. Losing it loses every stored token |
| `HERMES_TENANT_API_KEY` | yes | Bearer Hermes presents. Long random. Must differ from `OPERATOR_API_KEY` or all calls 401 |
| `OPERATOR_API_KEY` | yes | Human only: `/approvals/*`, `/v1/operator/*`. Must differ from `HERMES_API_KEY` and `HERMES_TENANT_API_KEY` |
| `HITL_SIGNING_PRIVATE_KEY_PATH` | yes | `/etc/motahai/hitl/hitl_private.pem`, 0600; Core refuses a group/world-readable file |
| `HITL_VERIFY_PUBLIC_KEY` (or `_PATH`) | yes | Public key, needed by Core to verify promote-live tokens |
| `HITL_USED_NONCES_PATH` | yes | `/var/lib/motahai/hitl_used_nonces.json` (not the `/home/deploy` default) |
| `SHOPIFY_APP_SECRET`, `SALLA_WEBHOOK_SECRET` | per platform | App-level secrets for webhook signature checks. Missing means those webhooks are refused |
| Per-tenant secrets (Meta CAPI token, Bosta, OTO) | per tenant | Stored Fernet-encrypted in the DB by `scripts/onboard_store.py` or operator endpoints, not in env |
| `OTO_WEBHOOK_MAX_AGE_SECONDS`, `OTO_MAX_FUTURE_SKEW_SECONDS` | optional | 3600 / 300 |
| `MOTAHAI_META_GRAPH_API_VERSION` | optional | Override of the pinned Graph version |
| `MOTAHAI_TRUSTED_PROXY` | set to `1` | Only when behind our own reverse proxy, so checkout capture sees the real client IP |
| `MOTAHAI_SMTP_HOST/PORT/USER/PASSWORD/FROM` | for digests | Sunday digest email. Not needed for the first checks |
| `AMEEN_HOST` / `AMEEN_PORT` | set | `127.0.0.1` / `58770` (never 8000) |
| `HERMES_API_KEY`, `HERMES_WEBHOOK_URL` | leave unset at first | Advisory Core to Hermes notifications only; unset is safe |
| `MOTAHAI_RUN_SCHEDULER_IN_APP` | `0` | The scheduler is its own service |

### Hermes (`/home/deploy/.hermes/.env`, 0600; add only these)

| Variable | Value |
|---|---|
| `MOTAHAI_CORE_URL` | (a) `https://core.<domain>` ; (b) `http://127.0.0.1:58770`. Must be a hostname or canonical dotted IPv4. The guard refuses credentials in the URL, odd numeric forms (`2130706433`, `127.1`), and loopback on port 8000 |
| `MOTAHAI_TENANT_API_KEY` | The same value as Core's `HERMES_TENANT_API_KEY` |
| `HITL_VERIFY_PUBLIC_KEY` / `_PATH`, `HITL_USED_NONCES_PATH` | F4: public key only, for GTM publish tokens |

### Who holds what

- Core only: Fernet key, DB credentials, **Ed25519 private key**, `OPERATOR_API_KEY`, platform webhook secrets,
  `HERMES_API_KEY`.
- Both sides: `HERMES_TENANT_API_KEY` / `MOTAHAI_TENANT_API_KEY` (same value; read-only counts plus the token-gated
  promote). Ed25519 **public** key (not secret).
- Hermes only: LLM keys, Telegram, SMTP for the sentinel. Hermes must never hold `OPERATOR_API_KEY`.
- Human operator: `OPERATOR_API_KEY` (password manager), the minted single-use tokens.
- Ed25519 private key isolation: generated by and owned by Linux user `motahai`, `0600`, dir `/etc/motahai/hitl`
  `0750`. On option (a) this is a different machine; on (b) the `hermes` and `deploy` users must not be in group
  `motahai` and must get "Permission denied" reading `/etc/motahai` (isolation README migration step 7).

## 3. Database bootstrap

1. Create the DB and role (Postgres), or the directory `/var/lib/motahai` owned by `motahai` 0700 (SQLite).
2. First start runs `init_db()` (`create_all`, idempotent) from the app lifespan. A failure only logs, so check
   the journal for `Database initialisation failed`.
3. **Fresh database: no ALTERs needed**, `create_all` builds the current schema including
   `tenants.test_event_verified_at` and `tenants.promoted_live_at`.
4. **Only if a database already exists** (older SQLite from a pilot or dev), `create_all` will not add columns. Run
   once, as the DB owner, after a backup (use `DATETIME` instead of `TIMESTAMP WITH TIME ZONE` on SQLite):
   ```sql
   ALTER TABLE tenants ADD COLUMN test_event_verified_at TIMESTAMP WITH TIME ZONE;
   ALTER TABLE tenants ADD COLUMN promoted_live_at TIMESTAMP WITH TIME ZONE;
   ```
   Older S2 columns (see the `db.py` docstring: match-key hashes, `due_at`, `settlement_hours`, `digest_email`,
   `meta_test_event_code`, and the `checkout_context` / `staged_webhooks` tables) need the same care on a non-empty
   database. Prefer starting a clean DB for Core.
5. Backups: Postgres `pg_dump -Fc` nightly to a separate location, 14 days kept, plus a restore test before go-live.
   SQLite: `sqlite3 file ".backup /path"` (not `cp` of a live file). Back up `core.env` and the Fernet key
   separately and offline. Back up the private key once, offline.

## 4. Deploy steps (option a; (b) differs only in the notes at the end)

Operator runs these on the **new** host. No step touches the SmartCafe host.

1. Provision Ubuntu LTS, ufw default deny incoming, allow 22 (key-only ssh), 80, 443. Create user `motahai`
   (system, nologin) and `/etc/motahai` (root:motahai 0750), `/var/lib/motahai` (motahai 0700).
2. Install python3-venv, postgresql (or none for SQLite), caddy or nginx. Clone the tagged release to
   `/opt/ameen-workforce`; `python3 -m venv venv && venv/bin/pip install -r requirements.txt "psycopg[binary]>=3.1,<4"`.
3. Generate the Ed25519 pair as `motahai` (command in `deployment/isolation/README.md`). Copy the printed public key.
4. Write `/etc/motahai/core.env` from section 2. Generate `HERMES_TENANT_API_KEY`, `OPERATOR_API_KEY` and the Fernet
   key on the host; do not paste them in chat or tickets.
5. Install `deployment/isolation/motahai-core-api.service` and `deployment/systemd/motahai-scheduler.service`.
   Edit the API unit to `--workers 1`. Both read `/etc/motahai/core.env`. Do not install `ameen-workforce.service`
   (it runs as root).
6. Reverse proxy with TLS on the webhook domain. Expose only: `/webhooks/`, `/v1/capture/`, `/health`. Return 404
   for everything else publicly, in particular `/approvals/`, `/v1/operator/`, `/v1/tenants/`, `/docs`,
   `/openapi.json`, `/webhook/hermes`, `/tasks`, `/escalations`.
7. Hermes to Core path: either a second proxy `server` block restricted by source IP to the VPS for `/v1/tenants/`,
   or an ssh tunnel / WireGuard. Operator access to `/approvals/*` and `/v1/operator/*` should go through the same
   restricted path or ssh port-forward, not the public vhost.
8. Start: `systemctl enable --now motahai-core-api motahai-scheduler`.
9. Onboard the pilot tenant with `scripts/onboard_store.py` (secrets via `--secrets-file`, mode `shadow`, run from
   the repo root as `motahai` with the env loaded) or the operator onboarding endpoints. Courier webhook URLs use
   the domain in section 7.
10. Only after the judging window: set the two Hermes variables, `systemctl restart hermes-mcp`.

Option (b) notes: own user `motahai`; Postgres or SQLite under `/var/lib/motahai`; uvicorn on
`127.0.0.1:58770` (verify with `ss -ltnp` that nothing else holds it); **no** edits to SmartCafe docker, nginx or
ufw; Hermes uses `MOTAHAI_CORE_URL=http://127.0.0.1:58770`; webhook ingress still needs a public route, which means a
location in SmartCafe's nginx (a coordination item, section 7).

## 5. Verification checklist

On the Core host first:

1. `curl -s http://127.0.0.1:58770/health` returns `status: online`.
2. `journalctl -u motahai-core-api -n 50` has no `Database initialisation failed`; tables exist (`\dt`).
3. As `motahai`: `python -m ameen_workforce.scheduler --check` exits 0 after the first cycle (up to 15 min).
4. Operator key works: `POST /approvals/promote-live` for an unknown tenant returns **404**; without the key **401**.
5. Public vhost: `curl -i https://<webhook-domain>/approvals/promote-live` returns 404 (not routed).
6. `sudo -u hermes cat /etc/motahai/core.env` (option b) fails with "Permission denied".

Through Hermes (after the judging window and the env change):

7. `hermes_motahai_tenant_status` with an unknown tenant returns **TENANT_NOT_FOUND (404)**, not
   `CORE_NOT_CONFIGURED`. That proves URL, key and routing are all correct and the F1 guard accepted the URL.
8. Direct check that the key is enforced: `POST /v1/tenants/<unknown>/promote-live` with no bearer returns 401; with
   the bearer but a missing/invalid token returns 404 for an unknown tenant, 403 `token_rejected` for a real one.
9. Promote-live without `OPERATOR_API_KEY`-minted token never flips; `mode` stays `shadow` (check the DB).
10. Send one signed test webhook from the platform (Shopify "send test"); `webhook_deliveries` shows
    `signature_ok = true`; `tenant_status` shows `webhooks_verified: true`.

## 6. Rollback and timing

Rollback is cheap because nothing on the existing stack changes: unset `MOTAHAI_CORE_URL` and
`MOTAHAI_TENANT_API_KEY` in Hermes and restart `hermes-mcp` (tools return `CORE_NOT_CONFIGURED` again), then
`systemctl stop motahai-core-api motahai-scheduler` (or delete the VPS). Point platform webhooks back or disable
them. A tenant flipped `live` can be set back to `shadow` with an operator-run SQL update; sent CAPI events cannot
be recalled, so keep tenants in `shadow` until checklist step 10 and the Test Events verification pass.

**Must wait until after the judging window** (do not touch the production Hermes VPS before then): any change to
`/home/deploy/.hermes/.env`, any `hermes-mcp` restart, and any deploy of the F1/telemetry/proxy commits. Everything on
the new host (steps 1 to 9, checks 1 to 6) can be prepared earlier because it does not touch the VPS.

## 7. Open decisions for the owner

1. **Host**: separate small VPS (recommended) or same-host isolation. If same-host, who agrees the nginx route with
   SmartCafe and who owns reloads?
2. **Webhook domain**: keep `employees.motahai.com` (DNS currently points to the Contabo VPS or the Wesam CNAME;
   `DNS_SETUP.md` plans a CNAME to Wesam) or a new `core.motahai.com`? Courier URLs derive from the hard-coded
   `EMPLOYEES_PORTAL`; if the domain changes, make that configurable first (small code change).
3. **Operator key holder**: who stores `OPERATOR_API_KEY` and mints promote-live tokens (one named human, in a
   password manager; never on the Hermes host)?
4. **Database**: Postgres (recommended) or SQLite with WAL.
5. **Backup target** and who restores; where the offline copy of the Fernet key lives.
6. **SMTP** for the Sunday digest, and whether to enable it in the first week.
7. Approve the follow-ups: fix `CORE_SERVICE_TOKEN` / `CORE_API_BASE_URL` names in `deployment/isolation/*.example`,
   make nonce store cross-process safe, make `/health` check the DB, enable SQLite WAL.
