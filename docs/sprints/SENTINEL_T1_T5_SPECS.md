# Token-Exhaustion Sentinel & Dead-Man's Switch — T1–T5 specs

Author: Opus 5.5 (orchestrator), 2026-10-11. Approved by owner. T1–T3 → Gemini 3.8 via Antigravity (Gemini cannot
be launched from Claude Code). T4–T5 → Haiku 5.5 (dispatched from Claude Code). Sonnet 5.5 = sequential escalation
only. Opus reviews every result before merge.

## 0. Hard rules (all tasks)

1. **Zero LLM in the detect → alert path.** Pure Python, stdlib + `httpx` only. No Jinja2 (avoid a new dependency;
   `str.format`/f-strings are enough). Must work with every LLM balance at $0.
2. **The repo is PUBLIC (Mo-ha33/Motahai).** Never commit the admin email, phone number, Telegram chat id, client
   emails, tokens or hostnames. They live only in `/home/deploy/.hermes/.env` (0600) and the contacts file below.
3. **No message content, prompts, keys or client PII** in flow state, incidents, logs or alerts. Admin alerts carry only:
   container public id, step, cause, timestamps, provider names (no store name, no client email — matches T4/T5).
4. The sentinel never raises into, imports from, or blocks the router or hermes-mcp. It reads them.
5. All file writes are atomic (`tmp` + `os.replace`), files 0600, directories 0700.

## 1. Facts the specs rely on (verified in code, branch `fix/free-tier-router-gtm-hardening`, PR #88)

- Router journal (`journalctl -u hermes-llm-router -o cat`): one JSON line per request:
  `{"ts", "served_by": <provider>|null, "latency_ms", "messages", "tools", "attempts": ["<provider>:<outcome>", ...]}`.
  `served_by: null` = every tier failed (client got 503 `router_exhausted`). **The line has no exhaustion kind and
  no container id.**
- Attempt outcomes include: `rate_limited`, `rate_limited:daily`, `payment`, `auth`, `timeout`, `network`,
  `skipped:cooldown`, `skipped:daily_requests`, `skipped:daily_budget`, `skipped:no_key`, `skipped:context`,
  `skipped:no_tools`, `skipped:deadline`, `skipped:busy`, `skipped:local_rpm`, plus HTTP-status outcomes.
  Router-side `exhausted_kind()` only returns `rate_limited` or `mixed`.
- `GET /healthz` (Bearer `ROUTER_API_KEY`) → `{"providers":[{"name","model","cooling_down_s","cooldown_kind",
  "consecutive_failures","last_error","model_gone","rejected_streak","tokens_today","requests_today",
  "daily_request_budget","rpm_tokens","inflight","key_present"}], "degraded":[names], ...}`.
  `cooldown_kind` ∈ `rate_limited | daily_quota | payment | auth | model_gone | ""`.
- Drill mode: `ROUTER_ENABLE_CHAOS=1` + `ROUTER_CHAOS_FILE=/var/lib/hermes-llm-router/chaos.json` containing
  `{"<provider>": 429 | "429daily" | "timeout" | ...}` (see router README §drills).
- SMTP env (same names as Core's `digest.py`): `MOTAHAI_SMTP_HOST`, `MOTAHAI_SMTP_PORT` (default 587, STARTTLS),
  `MOTAHAI_SMTP_USER`, `MOTAHAI_SMTP_PASSWORD`, `MOTAHAI_SMTP_FROM`. Not yet confirmed present on the VPS.
- Telegram: `TELEGRAM_BOT_TOKEN` exists in `~/.hermes/.env`. **The Bot API cannot message a phone number**: the
  admin must send `/start` to the bot once; the operator reads the chat id from `getUpdates` and sets
  `ADMIN_TELEGRAM_CHAT_ID`. The phone number is never stored.

## 2. Shared contracts (all tasks code against these; do not change without Opus)

**Package:** `ops/hermes/sentinel/` (`__init__.py`, `flow_state.py` [T1], `detect.py` + `__main__.py` [T2],
`dispatch.py` [T3], `templates.py` [T4]); deploy files under `ops/hermes/sentinel/deploy/` [T5].

**Env:** `HERMES_SENTINEL_DIR` (default `/home/deploy/.hermes/sentinel`), `ROUTER_URL` (default
`http://127.0.0.1:<router port from the unit>`), `ROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID`,
`ADMIN_ALERT_EMAIL`, `MOTAHAI_SMTP_*`, optional `SENTINEL_HEARTBEAT_URL`, optional `SENTINEL_FLOW_DEADLINE_S`
(default 300).

**C1 — flow-state record** (`$HERMES_SENTINEL_DIR/flow/<GTM-ID>.json`, one file per container, written by T1):
```json
{"container": "GTM-XXXXXXX", "step": "verify_access", "outcome": "CONNECTED",
 "owed_by": "agent", "deadline": "2026-10-11T12:05:00Z", "updated_at": "2026-10-11T12:00:00Z", "seq": 7}
```
`owed_by` ∈ `agent | client | operator | human | none`; `deadline` is null unless `owed_by == "agent"`.

**C2 — incident** (produced by T2, consumed by T3/T4; persisted in `$HERMES_SENTINEL_DIR/incidents.json`):
```json
{"incident_key": "router:exhausted", "severity": "CRIT", "state": "open",
 "kind": "router_exhausted", "exhaustion_kind": "daily_quota", "scope": "router",
 "container_id": null, "last_successful_step": null, "providers": ["nvidia", "gemini"],
 "http_status": 503, "opened_at": "...", "timestamp": "...", "count": 3}
```
- `severity` ∈ `WARN | CRIT`; `state` ∈ `open | resolved`. A resolved incident is sent once as RECOVERED.
- `kind` ∈ `router_exhausted | router_unreachable | provider_degraded | budget_80 | flow_stalled | sentinel_error`.
- `exhaustion_kind` ∈ `rate_limited | daily_quota | payment | auth | model_gone | mixed | null`.
- `scope` ∈ `router | provider | container`; `container_id` / `last_successful_step` only for `flow_stalled`.

**C3 — templates API** (T4 implements, T3 calls; all return plain text, no Markdown/HTML):
```python
admin_telegram(incident: dict, recovered: bool = False) -> str           # <= 3500 chars
admin_email(incident: dict, recovered: bool = False) -> tuple[str, str]  # (subject, body)
client_holding_email(lang: str = "ar", store_name: str | None = None) -> tuple[str, str]
client_followup_email(lang: str = "ar", store_name: str | None = None) -> tuple[str, str]
```

**C4 — client contacts** (`$HERMES_SENTINEL_DIR/contacts.json`, 0600, maintained by the operator, never in git):
`{"GTM-XXXXXXX": {"email": "...", "store_name": "...", "lang": "ar"}}`. Missing entry → no client email; the admin
alert says "client not notified: no contact on file".

---

## T1 — flow-state writer (Gemini worker 1)

**Files:** `ops/hermes/sentinel/flow_state.py`; a minimal hook in `ops/hermes/consultation.py` (≤ 10 lines, one
call after each GTM tool returns); tests. `consultation.py` is also edited by G1/S3-4 branches and has uncommitted
edits in the shared checkout — keep the hook small and isolated so merges stay trivial.

- `record(container, step, outcome, now=None)` maps (step, outcome) → (`owed_by`, `deadline`) with this table:

| step | outcome | owed_by | deadline |
|---|---|---|---|
| verify_access | CONNECTED | agent | now + 300 s |
| verify_access | NOT_INVITED, PERMISSION_MISSING, CONTAINER_NOT_FOUND, INVALID_ID, WORKSPACE_LIMIT | client | null |
| verify_access / workspace_apply / cloud_publish | operator-side error codes (e.g. API_DISABLED, quota) | operator | null |
| verify_access / workspace_apply | RETRYABLE errors | agent | now + 300 s |
| workspace_apply | applied | agent | now + 300 s |
| workspace_apply | LINT_FAILED | agent | now + 300 s |
| cloud_publish | awaiting / missing / rejected approval token | human | null |
| cloud_publish | published | none | null |

  Read the actual reason codes from `gtm_api.py` / `consultation.py` and map every one; unknown outcome →
  `operator`, never `agent` (no false stall alarms).
- `seq` increments per container. Never raises: any I/O error is logged at WARNING and swallowed.
- Only the public container id, step and outcome are stored (no account/workspace ids, no args, no text).

**Tests:** full mapping table; unknown outcome; atomic write + permissions (skip chmod asserts on Windows);
hook failure does not change the tool's return value.

## T2 — `hermes-sentinel` detector (Gemini worker 2)

**Files:** `ops/hermes/sentinel/detect.py`, `ops/hermes/sentinel/__main__.py` (`python -m sentinel --once`), tests.
One pass per invocation (systemd timer every 60 s, T5). State in `$HERMES_SENTINEL_DIR/state.json` (journal cursor,
per-provider budget day, last tick) and `incidents.json` (C2).

Detection (each pass):
1. **Journal:** `journalctl -u hermes-llm-router -o cat --show-cursor --after-cursor <saved>` (first run:
   `--since -2min`). Parse JSON lines; ignore non-JSON. Any `served_by: null` → open/refresh
   `router:exhausted` (CRIT, http_status 503). `exhaustion_kind` derived from the attempts' outcomes plus the
   same pass's `/healthz` `cooldown_kind`: all providers `rate_limited*`/`skipped:cooldown` with kind
   rate_limited → `rate_limited`; any `rate_limited:daily` / `skipped:daily_requests` / `skipped:daily_budget` /
   cooldown_kind `daily_quota` for every provider → `daily_quota`; `payment` → `payment`; `auth` → `auth`;
   `model_gone` → `model_gone`; otherwise `mixed`.
   Resolve when the pass sees ≥ 1 served line and no `served_by: null` line for 2 consecutive passes.
2. **`/healthz`:** unreachable or non-200 for 2 consecutive passes → `router:unreachable` CRIT. Each name in
   `degraded` → `provider:<name>:degraded` CRIT with that provider's `cooldown_kind`/`model_gone`. For providers with
   `daily_request_budget > 0`: `requests_today / budget ≥ 0.8` → `provider:<name>:budget80` WARN (once per provider
   per budget day; resolves silently at day roll — no RECOVERED message for WARN budget).
3. **Flow state (C1):** `owed_by == "agent"` and `now > deadline` → `flow:<GTM-ID>:<seq>` CRIT `flow_stalled`
   (scope container, `last_successful_step` = record step). Resolves when the file's `seq` changes.
4. Sentinel's own failures (journal unreadable, state corrupt) → `sentinel:error` WARN; never crash the pass.
5. Re-notify an still-open CRIT every 6 h (admin only). Silences: `$HERMES_SENTINEL_DIR/silence.json`
   `{"<incident_key or prefix>": "<until ISO>"}` suppresses sends, not detection.
6. Emit the list of notifications to send (open / reminder / recovered) and call T3's `dispatch()`; persist state
   only after dispatch returns (so a crash re-sends rather than drops).
7. Ping `SENTINEL_HEARTBEAT_URL` (if set) at the end of a successful pass.

**Acceptance:** tests with fixture journal lines + fake `/healthz` (no subprocess in unit tests; wrap `journalctl`
behind a function). Drill simulations: `429daily` on every tier → exactly 1 CRIT `daily_quota`, then 1 RECOVERED
after service returns; payment 402 → `provider:<x>:degraded` with `payment`; flow stall → 1 CRIT, then RECOVERED
when seq changes; no duplicate alerts across 10 passes.

## T3 — dispatcher (Gemini worker 3)

**Files:** `ops/hermes/sentinel/dispatch.py`, tests.
- `dispatch(notifications, contacts) -> list[result]`, using C3 templates only.
- Admin: CRIT open / reminder / RECOVERED → Telegram **and** email; WARN → Telegram only, email if Telegram fails.
  Telegram: `POST https://api.telegram.org/bot<TOKEN>/sendMessage` (`chat_id`, `text`,
  `disable_web_page_preview: true`), timeout 10 s, one retry after 5 s, then fall back to email.
  Email: STARTTLS with `MOTAHAI_SMTP_*` (copy the pattern of `digest.py`'s SMTP sender; do not import the Core
  package — it is not installed on the VPS), one retry, then log.
- Client: only `flow_stalled` CRIT, at most one holding email per incident and one follow-up on RECOVERED, only if
  C4 has a contact; language from contact (default `ar`).
- Never put the bot token in logs or exceptions (redact URLs). Missing config → skip that channel, log once,
  report `skipped:not_configured` (the run still succeeds).

**Acceptance:** mocked transports: Telegram down → email sent; both down → logged, no raise; WARN email only on
Telegram failure; client email once per incident; token never in captured logs; works with no SMTP env.

## T4 — templates (Haiku, dispatched 2026-10-11)

`ops/hermes/sentinel/templates.py` implementing C3. Client holding copy (Arabic, owner-approved, verbatim):
«استلمنا طلبكم ونعمل الآن على استكمال فحص بنية التتبع الخاصة بمتجركم. نحتاج وقتاً إضافياً لإتمام المعالجة،
وسنرسل لكم التقرير فور جاهزيته، وسنُطلعكم إن تأخر أكثر من المتوقع.»

## T5 — runbook + systemd (Haiku, dispatched 2026-10-11)

`ops/hermes/sentinel/deploy/hermes-sentinel.service` (oneshot) + `hermes-sentinel.timer` (60 s) +
`docs/runbooks/HERMES_SENTINEL_RUNBOOK.md`.
