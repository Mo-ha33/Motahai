# Sprint S3 Wave 2 — Gemini 3.8 task specs (for Antigravity)

Author: Opus 5.5 (orchestrator), 2026-10-11. Gemini cannot be launched from Claude Code, so these specs are run by
Antigravity. Opus reviews each result before merge. Running in parallel with Sonnet 5.5 on S3-4 (promote-live
endpoint + courier exemption, Core side).

Shared rules for every task below:
- Run only after Antigravity has reconciled local `main` with `origin/main` (25 behind / 3 unpushed Tariq commits).
- **G1 base:** branch from `worktree-agent-ad7073c0d28994b21` (67e30ef = S3-1 + S3-3 + S3-4, Opus-approved). It
  already edits `ops/hermes/consultation.py` (verify_publish_token now rejects tokens carrying `act`; the
  byte-for-byte parity test must keep passing). Merge the reconciled `main` into your branch and resolve any
  `consultation.py` / `tool-policy.yaml` conflicts keeping BOTH sides.
- Work on a feature branch, never on `main`. Do not push; Opus reviews first.
- Do not deploy to the VPS. Deployment is a separate, operator-confirmed step.
- Full suite must pass (`python -m pytest -q`); the 2 `tests/test_audience_routes.py` symlink failures
  (WinError 1314 on Windows) are known.
- Never log, echo or return tokens, API keys or approval tokens.

---

## G1 — S3-2: Hermes tools for the Core tenant API

**Files:** new `ops/hermes/motahai_api.py`; `ops/hermes/consultation.py` (registration only);
`ops/hermes/gateway/tool-policy.yaml`; `ops/hermes/gateway/tests/test_policy_guard.py`; new
`ops/hermes/gateway/tests/test_motahai_api.py` (or the location the other Hermes tool tests use).

**Pattern to copy:** `ops/hermes/gtm_api.py` + how `consultation.py` loads it (`_gtm_api()` sibling loader with
file-path fallback) and registers `hermes_gtm_verify_access` inside `register_consultation_tools(server)`.

**Tools (names are fixed — the Tariq payload already references them):**

| Tool | Core call | Notes |
|---|---|---|
| `hermes_motahai_tenant_status(tenant_key)` | `GET {MOTAHAI_CORE_URL}/v1/tenants/{tenant_key}/status` | read-only |
| `hermes_motahai_signal_health(tenant_key, window_days=7)` | `GET .../v1/tenants/{tenant_key}/signal-health?window_days=N` | read-only; 1..28 |
| `hermes_motahai_promote_live(tenant_key, hitl_approval_token)` | `POST .../v1/tenants/{tenant_key}/promote-live` body `{"approval_token": ...}` | D-003 gated |

- Auth: `Authorization: Bearer {MOTAHAI_TENANT_API_KEY}` (VPS env; same value as Core's `HERMES_TENANT_API_KEY`).
  Missing env → return an operator-audience error, make no call.
- Validate before calling: `tenant_key` = lowercase domain (`^[a-z0-9.-]{3,255}$`) or Salla merchant id
  (`^[0-9]{1,20}$`); `window_days` int 1..28. URL-encode the path segment.
- Timeout 15 s. Return JSON strings in the same envelope style as `gtm_api.error_response(...)`:
  - 200 → `{"ok": true, ...Core body}`
  - 401 → `CORE_AUTH_FAILED` (audience operator)
  - 404 → `TENANT_NOT_FOUND` (audience operator)
  - 403 `token_rejected` → `TOKEN_REJECTED` (audience operator; tell the agent to request a NEW token)
  - 409 `precondition_failed` → `PRECONDITION_FAILED` with the Core `failed` list passed through
  - 409 `already_live` → `ALREADY_LIVE`
  - network/5xx → `CORE_UNREACHABLE`, `retryable: true`
- Core response contract (S3-1, approved): `/status` → `tenant_key, platform, mode, active, created_at,
  has_meta_credentials, last_webhook_at, webhook_failures_7d, last_capi_event_at, webhooks_verified,
  checklist_ready, onboarded, missing_required_steps, test_event_code_set`. `/signal-health` →
  `tenant_key, window{start,end,days}, cohort_window{start,end}, orders{placed,cod,confirmed},
  cohort{orders,delivered,refused}, capi_events{sent,shadow,failed,stale,late_delivery}, would_have_sent,
  open_incidents[{type,opened_at}]`. Promote-live (S3-4, approved 67e30ef): 200 `{tenant_key, mode:"live",
  promoted_live_at}`; errors are top-level JSON: 404 `{"detail":"Not found"}`, 409 `{"error":"already_live"}`,
  403 `{"error":"token_rejected"}`, 409 `{"error":"precondition_failed","failed":[inactive, checklist_not_ready,
  test_event_not_verified, test_event_code_still_set, no_delivered_evidence, no_shadow_events]}`. Operators mint
  the token via `POST /approvals/promote-live` (OPERATOR_API_KEY) or `python -m ameen_workforce.hitl_tokens
  issue-action --action promote_live --subject <tenant_key>`. Pass Core bodies through unchanged; do not rename fields.

**tool-policy.yaml:**
- `hermes_motahai_tenant_status`, `hermes_motahai_signal_health`: same limits/role placement as
  `hermes_gtm_verify_access` (read-only).
- `hermes_motahai_promote_live`: mirror `hermes_gtm_cloud_publish` exactly (rate 2, concurrency 1,
  global_concurrency 1, `approval_required: {gate: D-003, mechanism: signed_single_use_token,
  arg: hitl_approval_token, issuer: "POST /approvals/promote-live"}`, and the same role placement and token-arg
  redaction that cloud_publish has).

**Tests (acceptance):**
- Policy guard: tier, limits and approval_required for all three tools.
- `motahai_api`: each status-code mapping (mock the HTTP layer); validation rejects bad tenant_key/window
  without a call; missing env → no call; the approval token and API key never appear in the returned string or
  captured logs.
- `tests/test_tariq_payload.py::test_allowed_tools_are_registered_in_hermes` style check passes for the
  flag-on tool set (extend it so the COD tools must be registered in `consultation.py`).

**Known risk to record in the module docstring:** the Core key can read any tenant's counts. For the pilot
(stores we onboard ourselves) this is accepted; before opening W6 to arbitrary stores, bind tenant_key to the
Wesam conversation server-side.

---

## G2 — S3-9: Tariq prompt hardening (`scripts/build_tariq_payload.py` + playbook)

Goal: smoother client experience and stricter Hermes routing. Default payload WILL change (re-inject after
review). Keep inner size ≤ `INNER_BUDGET_BYTES` (18 KB) in BOTH modes.

Add to `strict_guardrails` / rules (terse, in the existing style):
1. **No tool result → no claim.** Never state a container, store or signal status that a tool did not return in
   this conversation. If a tool fails: say so plainly, retry only where the routing note allows, then escalate
   via `hermes_consult`. Never simulate tool output.
2. **One store per conversation.** tenant_key / GTM ID are fixed at W1. A request about a different store →
   do not call tools for it; ask the operator via `hermes_consult`.
3. **Resume, don't restart.** When a returning client writes, call the status tool for their stage
   (`hermes_gtm_verify_access` for W2–W5; with the COD flag, `hermes_motahai_tenant_status` for W6) and continue
   from the right step; never repeat W1 intake for assets already received.
4. **Waiting message.** Before a call that can take over ~30 s (`hermes_run_task`, `hermes_gtm_workspace_apply`,
   publish), send one short Arabic line saying what is being checked; no silent waits, no fake progress.
5. **Language and pace.** Mirror the client's language/dialect; one question per message; no internal tool names,
   IDs or English jargon to clients.
6. **Claims list.** Never quote industry return/cancellation rates or "saved budget"; only this store's measured
   numbers. Google Ads delivered-revenue sync is NOT available — never offer it.
7. **Intent → workflow map** (Arabic + English cues): tracking setup / GTM → W1; "الطلبات المرتجعة",
   "مبيعات وهمية", "الدفع عند الاستلام", COD → W6 (flag on) else escalate; "هل التتبع شغال", health → W7 (flag on)
   else W5.

Tests: each new rule present; intent map references only workflows that exist in that mode; size budget in both
modes; existing tests unchanged in meaning.

---

## G3 — S3-10: deterministic storefront probe (`hermes_storefront_probe`) — replaces the G3 standby slot

Why: the Access Gate (Gate 2: GTM snippet installed; later GA4/Meta beacons) must not depend on an LLM reading
HTML. Plain HTML regex misses GTM loaded by Shopify app embeds / Customer Events pixels and Salla/Zid native
integrations. Core's `browser_service.inspect_target_url` is NOT usable for gating (fetch failure is reported as
"PDPL FAIL / grade F", and it uses `verify=False`). S3-6 Drift Sentinel will reuse this probe for beacon checks.

**Files:** new `ops/hermes/storefront_probe.py` (pure logic + a thin headless runner), registration in
`ops/hermes/consultation.py`, policy entry in `tool-policy.yaml` (read-only tier, rate 6, concurrency 1,
global 2, timeout 90 s), tests with recorded fixtures (no live network in tests). Branch from the G1 branch.

**Tool:** `hermes_storefront_probe(store_url, gtm_public_id=None, ga4_measurement_id=None, meta_pixel_id=None)`
- https only; the host must be a public store domain (refuse google.com / googletagmanager.com / facebook.com
  hosts, localhost, IP literals and private ranges — SSRF guard; resolve and re-check after redirects).
- Headless load of the home page and one product page (first `/products/` link on Shopify, first product link
  otherwise), wait for network idle (cap 20 s per page), capture request URLs only (no bodies, no cookies stored).
- Extract from requests: GTM ids (`googletagmanager.com/gtm.js?id=`), GA4 ids (`/g/collect` `tid=`, `gtag/js?id=`),
  Meta pixel ids (`facebook.com/tr` `id=`), TikTok (`analytics.tiktok.com`), Snap (`tr.snapchat.com`).
  Consent: whether a `gcs`/`gcd` parameter is present on Google hits (consent mode signals), and whether any
  ads/analytics hit fired before the first consent-update event if detectable; otherwise report null, not false.
- Result JSON: `{status: "OK"|"UNKNOWN", reason?, pages:[{url, http_status}], gtm_ids_seen, ga4_ids_seen,
  meta_pixel_ids_seen, other_pixels, expected:{gtm_loaded, ga4_seen, pixel_seen} (true/false/null when not
  asked), duplicates:{gtm, ga4, meta} (ids seen more than once as distinct containers/tags), consent_signals_seen}`.
- Any load failure, timeout or bot wall → `status: "UNKNOWN"` with a reason code (`FETCH_FAILED`, `TIMEOUT`,
  `BOT_CHALLENGE`, `TLS_ERROR`, `BLOCKED_HOST`). Never report false/missing for something that was not observed
  because the page did not load. TLS verification stays on.
- No PII: strip query strings except the allowlisted id/tid/gcs/gcd params before returning.

**Tests:** fixture request logs for: Shopify theme snippet, Shopify app-embed/custom-pixel load, Salla native
integration, duplicate GTM, no GTM, fetch failure → UNKNOWN; SSRF guard cases; query-string stripping.
