# Hermes × Wesam GTM Swarm — Supervisor Blueprint

Production configuration that turns Hermes Agent (VPS, MCP at
`https://api.motahai.com/mcp/sse`) into the CTO / Senior Tracking Architect for the
8 Wesam AI GTM agents.

## What's in this folder

```
ops/hermes/
├── README.md                     ← you are here (install + security policy, Section D)
├── home/                         ← mirrors /home/deploy/.hermes/
│   ├── SOUL.md                   ← A. identity, standards, refusal rules, Verdict Envelope
│   ├── MEMORY.md                 ← B. small always-loaded index + standing decisions
│   ├── memories/
│   │   ├── client-ledger.md      ← B. client & container state ledger (template + 100000001)
│   │   ├── gtm-patterns.md       ← B. battle-tested patterns & gotchas (Shopify, CAPI, EGP…)
│   │   └── incident-log.md       ← B. incident & remediation log template
│   └── skills/
│       ├── README.md             ← C. catalogue + specs for 5 skills
│       └── gtm-container-linter/ ← C. the critical skill — built and tested
│           ├── SKILL.md
│           ├── scripts/lint_container.py      (stdlib only, deterministic)
│           ├── scripts/fetch_live_version.py  (GTM API, read-only scope)
│           └── tests/                         (6 tests + fixture with planted defects)
├── gateway/                      ← D. enforcement, not just advice
│   ├── tool-policy.yaml          ← per-agent tokens, role allowlists, rate/timeout/budget
│   ├── policy_guard.py           ← drop-in guard for the FastAPI JSON-RPC handler
│   ├── nginx-mcp.conf            ← SSE-safe proxy, edge marker, flood limits
│   └── hermes-resources.conf     ← systemd limits so Hermes can't starve the co-hosted app
├── llm-router/                   ← NVIDIA → OpenRouter → Gemini router (decision record in its README)
├── integration/                  ← soul_router_client.py: SOUL+MEMORY as system prompt, router failover, provenance
├── demo/                         ← competition_demo.py + DEMO.md: live proofs + evidence report
└── sop/
    └── WESAM_AGENT_SOP.md        ← E. request/verdict contracts + 3 recipes with JSON-RPC payloads
```

---

## D. Security & Tool Access Policy

### D.0 Threat model

Hermes runs on a VPS that also hosts other production services, and it exposes powerful
tools (autonomous tasks, a headless browser, memory writes) to agents on a third-party
platform. The design assumes **any agent can be prompt-injected** by the client pages and
containers it reads. The controls, from the outside in:

1. **Capability URL at the edge.** Wesam's MCP connector takes a URL and cannot send
   headers, so the URL itself is the credential: Nginx serves `/mcp/<64-hex-secret>/`,
   injects the role's bearer token towards the gateway, keeps the secret out of access
   logs, and returns 404 for anything else under `/mcp/`. The secret is generated on the
   VPS (`openssl rand -hex 32`) and never enters git or chat. Template:
   `deploy/mcp-capability.conf.tpl`; full edge config: `gateway/nginx-mcp.conf`, section B.
2. **Dangerous tools are not in the public catalogue.** `hermes_terminal_exec`,
   `hermes_skill_create` and `hermes_memory_update` are human-only (`human_operator`,
   loopback/SSH-tunnel only). Agents *propose* memory writes through the `memory_write`
   field of the Verdict Envelope; Hermes or a human applies them.
3. **`policy_guard.py` in the JSON-RPC handler** enforces per-role allowlists, per-tool
   rate limits, concurrency caps, hard timeouts, daily token budgets, pinned argument
   values (models, reasoning effort, allowlisted skills) and an audit log that stores an
   argument **hash**, never values.
4. **SSRF protection** for every URL-taking tool: https only, private/loopback/link-local/
   CGNAT/metadata ranges blocked at request time, plus an OS-level egress firewall for the
   Hermes Unix user as a backstop against DNS rebinding (checklist item).
5. **Least privilege on the host** *(hardening checklist, in progress)*. Run Hermes under
   its own Unix user (not in `docker` or `sudo`), behind the systemd sandbox in `gateway/hermes-resources.conf` (memory/CPU
   ceilings, read-only system and home, writable only `~/.hermes`).
6. **Internal tool loops** *(hardening checklist, in progress)*. A gateway ACL doesn't
   cover tools Hermes calls *internally* during `hermes_run_task`, so MCP-originated
   sessions should run with terminal/file-write toolsets disabled.
7. **No production writes, ever.** SOUL §4 and decision D-003: Hermes never publishes a GTM
   version or theme change. A human publishes after QA, with a rollback path in the verdict.

Host-specific values (IPs, ports, secrets, the co-hosted stack) live in a private ops
runbook that is not part of this repository.

### D.1 Tool exposure matrix

| Tool | advisor (all 8) | operator_agent | human_operator (SSH tunnel only) | Why |
|---|:-:|:-:|:-:|---|
| `hermes_consult` | ✅ | ✅ | ✅ | Advice only |
| `hermes_audit_code` | ✅ | ✅ | ✅ | Review only |
| `hermes_skills_list` / `hermes_skill_get` | ✅ | ✅ | ✅ | Read only |
| `hermes_memory_read` / `hermes_session_search` | ✅ | ✅ | ✅ | Read only (memory holds no secrets/PII by rule) |
| `hermes_web_search` / `hermes_web_extract` | ✅ | ✅ | ✅ | Public web; SSRF-checked; output treated as untrusted |
| `hermes_run_task` | ❌ | ✅ | ✅ | Autonomous, so it inherits Hermes's internal tools (see D.0-4) |
| `hermes_skill_execute` | ❌ | ✅ allowlisted skills | ✅ | Runs code, but only reviewed skills |
| `hermes_browser_*` (incl. `evaluate`) | ❌ | ✅ | ✅ | Needed for evidence; SSRF + script-size limits |
| `hermes_memory_update` | ❌ | ❌ | ✅ | Memory poisoning changes every future verdict |
| `hermes_skill_create` | ❌ | ❌ | ✅ | Persistent code execution |
| `hermes_terminal_exec` | ❌ | ❌ | ✅ | Shell on a production host |

Agent → role: `operator_agent` = Orchestrator, Auditor, Schema Validator, Server-Side,
QA/Consent, Sentinel. `advisor` = Tag Architect and Auto-Fix Engineer. The Auto-Fix
Engineer writes code and doesn't need to execute anything on the VPS; keeping the code
writer away from the execution tools is deliberate.

Agents propose memory writes through the `memory_write` field of the Verdict Envelope.
Hermes or the human applies them.

### D.2 Rate limits, timeouts, budgets (enforced in `tool-policy.yaml`)

| Layer | Control | Value |
|---|---|---|
| Nginx | per-IP | 120 req/min, burst 40, 20 connections, 256 KB body |
| Guard | per agent per tool | consult 20/min · audit_code 10/min · run_task 4/min · browser 20/min |
| Guard | concurrency | run_task 1 per agent / 3 global · browser 1 per agent / **2 global** (RAM) |
| Guard | hard timeouts | consult 90 s · audit_code 150 s · skill 180 s · run_task 300 s |
| Nginx | SSE | `proxy_buffering off`, `proxy_read_timeout 330s` (> longest tool timeout) |
| Guard | tokens | `max_output_tokens` injected (1,500 default) · daily budget per role (400k / 1.5M / 3M) |
| systemd | host | `MemoryMax=3G`, `CPUQuota=200%`, `Nice=10`, so Hermes gets OOM-killed before co-hosted services |
| Audit | log | `~/.hermes/logs/mcp-audit.jsonl` — principal, tool, status, latency, arg **hash** (never values) |

**Timeout prevention:**
- Send SSE keep-alive comments (`: ping\n\n`) every 15 s from the gateway, so idle proxies
  don't cut long tool calls.
- Split anything that might exceed 5 minutes into steps (see SOP §6). If you need longer
  jobs, add a 22nd tool `hermes_task_status`: `run_task` returns `{job_id}` immediately,
  and agents poll the status tool.
- Every downstream HTTP call in skills has its own timeout (`fetch_live_version.py`: 30 s).

**Token budget (LLM routing: see `llm-router/README.md`):**
- Keep `MEMORY.md` under 3 KB and `SOUL.md` under ~12 KB, because both are paid for on
  every call. Detail lives in `memories/*.md`, which Hermes reads on demand.
- Deterministic skills come first. A 200 KB container through an LLM costs ~60k tokens,
  while the linter report is ~2–5k.
- Cap linter output (`--max-findings 80`). Agents ask for detail by rule when they need it.
- Record the `usage.total_tokens` of each response into `guard.record_tokens(...)`.
- Provider failover (NVIDIA → OpenRouter → Gemini) lives in `llm-router/`; it caps
  `max_tokens` per provider, so a fallback never gets a larger output budget.
- **Model choice:** **Option A (Recommended):** keep Flash for `consult`/`run_task`,
  and route `hermes_audit_code` plus any verdict with `confidence < 0.7` to a stronger
  reasoning model (a dedicated provider entry in `llm-router/providers.yaml`). Code review is where a cheap model's
  misses cost the most. **Option B:** Flash everywhere, which is cheaper but leaves
  more of the "zero-hallucination" standard resting on the linter and on QA.

---

## Install

> Credentials rule (repo `credentials-guard` skill): no token, key or password goes into
> git or chat. Everything secret goes in `/home/deploy/.hermes/.env` (or the new
> `hermes` user's) with `chmod 600`.

**Deploying `home/` (SOUL, memory, linter skill):** use
`deploy/deploy_blueprint.sh` from your machine. It does the backup, a self-test of the
linter on the VPS, the install (never overwrites live memory or ledgers), the restart of
`hermes-mcp.service`, checksum verification, and a one-command rollback. See
`deploy/README.md`. The manual steps below cover the parts it doesn't do.

```bash
# 0) on the VPS — back up
tar czf ~/hermes-backup-$(date +%F).tgz -C ~ .hermes

# 1) copy files (from a checkout of this repo on your machine)
rsync -av ops/hermes/home/SOUL.md ops/hermes/home/memories ops/hermes/home/skills \
      -e "ssh -p 2222" deploy@<VPS>:~/.hermes/
#    MEMORY.md: MERGE by hand into the existing file — do not overwrite live memory.

# 2) linter self-test on the VPS (Python ≥ 3.11)
cd ~/.hermes/skills/gtm-container-linter && python3 -m pytest tests -q

# 3) GTM read-only access
#    GCP: enable "Tag Manager API" → create service account → JSON key
#    GTM Admin → User Management → add SA e-mail with **Read** on the account
mkdir -p ~/.hermes/secrets && chmod 700 ~/.hermes/secrets
#    upload key to ~/.hermes/secrets/gtm-readonly.json ; chmod 600 it
echo 'GTM_SA_KEY_FILE=/home/deploy/.hermes/secrets/gtm-readonly.json' >> ~/.hermes/.env
pip install google-auth requests pyyaml   # inside Hermes's venv

# 4) gateway
#    copy gateway/tool-policy.yaml + policy_guard.py next to the FastAPI app, wire per the
#    docstring at the top of policy_guard.py, generate one token per agent:
python3 -c "import secrets,hashlib;t=secrets.token_urlsafe(32);print('TOKEN',t);print('HASH ',hashlib.sha256(t.encode()).hexdigest())"
#    HASH → ~/.hermes/.env as MCP_TOK_ORCH=..., MCP_TOK_AUDITOR=..., etc.
#    TOKEN → that agent's MCP connector in Wesam (Authorization: Bearer <token>)
#    If Wesam allows only ONE connector for the whole workspace, use two tokens
#    (advisor / operator_agent) and attach the matching connector per agent.

# 5) nginx (runs in the edge-nginx container) + systemd
#    copy the snippet into the conf directory mounted into edge-nginx, include it in the
#    api.motahai.com server{}, and point proxy_pass at the host bridge IP (not 127.0.0.1:
#    inside the container that is the container itself)
docker exec edge-nginx nginx -t && docker exec edge-nginx nginx -s reload
for unit in hermes-mcp.service hermes.service; do       # MCP wrapper + agent gateway
  sudo mkdir -p /etc/systemd/system/$unit.d
  sudo cp gateway/hermes-resources.conf /etc/systemd/system/$unit.d/resources.conf
done
sudo systemctl daemon-reload && sudo systemctl restart hermes-mcp.service
```

### Verify

```bash
# unauthenticated → 401
curl -s -o /dev/null -w "%{http_code}\n" https://api.motahai.com/mcp/sse
# advisor token: tools/list must NOT contain hermes_terminal_exec / run_task
# operator token through the public URL: hermes_terminal_exec → 403
# browser_navigate to https://127.0.0.1/ → 403 "non-public address"
# audit log grows with one line per call:
tail -f ~/.hermes/logs/mcp-audit.jsonl
# Wesam: Lead Orchestrator runs SOP Recipe 1 → reply parses as hermes.verdict.v1
```

## Rollout order

1. **Day 0 — security:** D.0 items 1–5, gateway guard, endpoint authentication.
2. **Day 1 — identity & memory:** SOUL.md, MEMORY.md merge, memories/.
3. **Day 1 — first evidence:** linter baseline on container 100000001, then record the
   fingerprint in the client ledger.
4. **Week 1:** build `datalayer-schema-validator` and `capi-dedup-debugger` (specs in
   `home/skills/README.md`).
5. **Week 2:** `consent-mode-auditor`, `shopify-pixel-patcher`; Sentinel daily drift loop.

## Assumptions to confirm

- The 21 MCP tool **argument names** (SOP payloads assume `question`/`task`/`code`/`context`).
  Align with `tools/list`.
- Where your Hermes build loads `MEMORY.md` from (root vs `memories/`) and its size cap.
- Whether Wesam supports per-agent MCP connectors with custom headers.
- The FastAPI gateway port (`<<MCP_GATEWAY_PORT>>`) and whether Cloudflare fronts
  `api.motahai.com` (affects real-IP rate limiting).
