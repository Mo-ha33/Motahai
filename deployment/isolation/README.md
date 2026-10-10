# Core / Hermes isolation (trust boundary)

Hermes runs an LLM with shell tools (`hermes_run_task`, `hermes_terminal_exec`). Anything the Hermes runtime
can read, a prompt-injected agent can read. So everything that moves money or approves a production change must
live where no agent runs.

```
 operator (human)                                   Hermes runtime (agent_net / hermes user)
      |  OPERATOR_API_KEY                           LLM + MCP server + skills
      v                                             holds: HITL PUBLIC key, scoped Core service token,
+---------------------------------+                         per-tenant MCP capability tokens, LLM key
| CORE host / containers          |  <---- HTTPS, Hermes -> Core only (core_internal) ----+
| API, scheduler, Postgres        |
| holds: Fernet key, DB creds,    |  Core never calls Hermes to decide anything on the money path.
| HITL PRIVATE key, operator key  |  (Optional status notifications to Hermes use HERMES_API_KEY and are advisory.)
+---------------------------------+
```

## Rules

1. Core (API, scheduler, Postgres, `MOTAHAI_FERNET_KEY`, the Ed25519 HITL private key, `OPERATOR_API_KEY`) runs on a
   host or container with **no LLM agent and no Hermes shell tools**, as its own Linux user (`motahai`, uid 10001).
2. Hermes (LLM, MCP server, skills) runs separately as user `hermes`. It holds only: the HITL **public** key, a
   scoped Core service token, per-tenant MCP capability tokens, and its own LLM provider key.
3. Network: Hermes -> Core only, HTTPS to Core's internal API. Nothing on the Hermes side can reach Postgres, and
   Core never relies on a Hermes answer for a money-path decision (D-003 approvals are verified on the Hermes side
   from a token a human obtained from Core; Core's own sends do not depend on Hermes).
4. D-003: a publish token is Ed25519-signed by Core, verified by the MCP server with the public key. Compromising
   the Hermes runtime reveals the public key, which cannot sign.
5. `/approvals/*` is operator-only (`OPERATOR_API_KEY`, which the Hermes side never holds). Hermes can reach the
   same port, so also block `/approvals/` for the Hermes network at the reverse proxy you put in front of Core.

## Secrets inventory

| Secret | Holder | File / env | Rotation |
|---|---|---|---|
| HITL Ed25519 private key | Core (`motahai`) only | `HITL_SIGNING_PRIVATE_KEY_PATH` -> `/etc/motahai/hitl/hitl_private.pem`, 0600 | Yearly or on suspected exposure: generate a new pair, deploy the new public key to Hermes, then swap the private key (outstanding tokens expire within 30 min) |
| HITL Ed25519 public key | Hermes (public, not secret) | `HITL_VERIFY_PUBLIC_KEY` (base64url) or `HITL_VERIFY_PUBLIC_KEY_PATH` -> `/etc/hermes/hitl_public.pem`, 0644 | With the private key |
| `OPERATOR_API_KEY` | Core + the human operator | `core.env` | Quarterly; must differ from every other key |
| `MOTAHAI_FERNET_KEY` | Core only | `core.env` | Re-encrypt stored credentials, then replace |
| `DATABASE_URL` / `POSTGRES_PASSWORD` | Core only | `core.env`, `postgres.env` | Quarterly |
| Scoped Core service token (`CORE_SERVICE_TOKEN`) | Core validates, Hermes presents | `core.env`, `hermes.env` | Quarterly; scope excludes `/approvals/*` |
| Per-tenant MCP capability tokens | Hermes issues/validates, one per tenant | `MCP_TENANT_TOKENS_FILE` | Per tenant, on offboarding or exposure |
| `HERMES_API_KEY` | Core (advisory notifications to Hermes) | `core.env` | Quarterly; must differ from `OPERATOR_API_KEY` |
| LLM provider key | Hermes only | `hermes.env` | Per provider policy |

Removed: `HITL_SIGNING_KEY` (HMAC). It is no longer read anywhere; delete it from every environment, and treat any
copy that ever sat on the Hermes host as compromised (it can no longer mint tokens, but remove it anyway).

`CORE_SERVICE_TOKEN` and `MCP_TENANT_TOKENS_FILE` name the intended mechanism; check the code you deploy honours them
before relying on them. Generate the HITL keypair on the Core host:

```bash
sudo -u motahai install -d -m 750 /etc/motahai/hitl
sudo -u motahai env PYTHONPATH=/opt/ameen-workforce/src /opt/ameen-workforce/venv/bin/python \
  -m ameen_workforce.hitl_tokens generate-keypair --out /etc/motahai/hitl     # prints the PUBLIC key only
```

## Option A: containers (`docker-compose.yml`)

Services `core-api`, `core-scheduler`, `postgres` (on `core_net`), `hermes` (on `agent_net`). `core_internal`
(`internal: true`) is shared by `core-api` and `hermes` only, so it is the single path from the agent side to Core.
Postgres has no `ports:`; the API has only `expose:`; no container mounts the Docker socket; Core containers run
non-root with a read-only root filesystem, `/tmp` on tmpfs and all capabilities dropped. Each env file holds only that
side's secrets (`postgres.env` is separate so the database container does not receive Core's other keys).

```bash
cd deployment/isolation
for f in core postgres hermes compose; do cp $f.env.example $f.env; done   # then edit the placeholders
chmod 600 core.env postgres.env hermes.env
docker compose --env-file compose.env up -d --build
```

Limits: `agent_net` has internet egress (LLM APIs), and Docker cannot restrict it to "Core only" by itself; apply a
`DOCKER-USER` firewall rule or an egress proxy if you need that. Core's operator endpoints are reached through a
reverse proxy you attach to `core_net` (not included). The Core image installs `psycopg` for Postgres; verify the
code path you run supports it.

## Option B: systemd on one or two hosts

`motahai-core-api.service` and `deployment/systemd/motahai-scheduler.service` (existing) both run as `User=motahai`.
`hermes-runtime.service` runs as `User=hermes`.

| Path | Owner | Mode |
|---|---|---|
| `/etc/motahai/` | root:motahai | 0750 |
| `/etc/motahai/core.env` | motahai:motahai | 0600 |
| `/etc/motahai/hitl/hitl_private.pem` | motahai:motahai | 0600 (Core refuses to sign if group/world readable) |
| `/etc/hermes/hermes.env` | hermes:hermes | 0600 |
| `/etc/hermes/hitl_public.pem` | root:hermes | 0644 (public) |
| `/var/lib/motahai/` | motahai:motahai | 0700 |
| `/home/hermes/.hermes/hitl_used_nonces.json` | hermes:hermes | 0600 |

Two users on one kernel is weaker than two hosts: a local privilege escalation defeats it. Prefer separate hosts or
VMs for Core when the budget allows; the manifests are the same. The `hermes` user must have no `sudo` rights and
must not be in the `motahai` group.

## Migration from today's shared VPS

1. Generate the keypair on the Core side (command above). Keep the private PEM at 0600 `motahai`.
2. Create users `motahai` and `hermes` and the directories in the table. Move `core.env` contents
   (`MOTAHAI_FERNET_KEY`, `DATABASE_URL`, `OPERATOR_API_KEY`) out of `/opt/ameen-workforce/.env` and any world- or
   agent-readable location; add `HITL_SIGNING_PRIVATE_KEY_PATH`.
3. Move the SQLite file (or migrate to Postgres) into `/var/lib/motahai`; point API and scheduler at one `DATABASE_URL`.
4. Switch the API from `User=root` (`ameen-workforce.service`) to `motahai-core-api.service`; keep the scheduler on
   `motahai-scheduler.service`. Stop and disable the old unit.
5. On the Hermes host: install `cryptography` in the Hermes venv, deploy the updated `consultation.py`, set
   `HITL_VERIFY_PUBLIC_KEY` and remove `HITL_SIGNING_KEY` from the environment, units and shell profiles.
6. Rotate every secret that was ever readable by the Hermes user: operator key, Fernet key (re-encrypt stored
   credentials first), DB credentials, `HERMES_API_KEY`.
7. Test as the `hermes` user: `cat /etc/motahai/core.env` and `cat /etc/motahai/hitl/hitl_private.pem` must fail with
   "Permission denied", a tokenless `hermes_gtm_cloud_publish` must return `AWAITING_HITL_APPROVAL`, and a token from
   `POST /approvals/gtm-publish` must publish exactly once.
8. Add the firewall rule so the Hermes host can reach only Core's API port, and Core's Postgres is not reachable
   from it.

All hostnames, addresses and values in this folder are placeholders.
