# Hermes LLM Routing — NVIDIA → OpenRouter → Gemini

Decision record + implementation for Hermes Agent's multi-provider fallback on the
production VPS. Researched against hermes-agent docs/issues as of 2026-10 (sources at
the end). Items tagged `[VERIFY]` depend on behaviour I could not test from here.

---

## 1. Decision

**Hybrid: a local router as Hermes's only primary provider (Approach B), plus ONE native
Hermes fallback straight to Gemini as a break-glass in case the router itself is down.**
Don't use LiteLLM; use the ~600-line router in this folder (or harden your
`smart_router.py` to pass the same 27 tests).

### Why not native-only (Approach A)

Hermes's native fallback is good and actively maintained. It benches a provider until
its rate-limit reset, uses exponential cooldowns from 60 s to 4 h, rotates credential
pools, and restores the primary on each new message. But three documented properties
rule it out for a three-tier cascade:

| Requirement | Native Hermes | Router |
|---|---|---|
| NVIDIA → OpenRouter → Gemini **in one turn** | ❌ "Within a single turn, fallback activates at most once — if the fallback also fails, normal error handling takes over." A live OpenRouter failure after an NVIDIA failure never reaches Gemini. | ✅ unlimited hops within a 270 s deadline |
| Skip NVIDIA when the prompt won't fit | ❌ context-length errors aren't a fallback trigger; Hermes compresses to the primary's window instead | ✅ pre-flight size estimate; goes straight to Gemini |
| Treat a malformed tool call as a failure | ❌ only HTTP/transport errors | ✅ validates JSON args and tool names, then falls through |
| Network outage of the primary | ⚠️ open bug #120608 (v2026.8.31): timeouts or connection errors during credential resolution skip the fallback chain | ✅ timeouts and connection errors are normal failover |
| Stable model inside a tool loop | Per-turn restore, so the model can flip between turns | ✅ session sticks to its fallback provider for 10 min |
| NIM `reasoning_effort` | ❌ not sent to NIM (issue #19883, open) | ✅ `extra_body: {reasoning_effort: …}` |

### Why not LiteLLM

LiteLLM has the right features (`fallbacks`, `context_window_fallbacks`, cooldowns).
But on **24 March 2026, PyPI releases 1.82.7/1.82.8 were backdoored** (the TeamPCP
campaign) with a credential stealer that ran at interpreter start. This VPS holds the
the co-hosted app production secrets. A large dependency tree holding all three LLM keys on
that box is the wrong trade when the logic needed is about 600 auditable lines on
`starlette` + `httpx`. If you choose LiteLLM anyway: pin by hash, run it under its own
user, and keep it off this host.

### Why keep one native fallback

The router is a single point of failure. If it crashes or hangs, Hermes's native
`fallback_providers: [gemini]` answers directly. That is a different process, a
different code path, and Hermes's own Gemini integration, which handles thought
signatures natively.

```
Hermes ──► 127.0.0.1:47311 router ──► NVIDIA (tier 1)
   │                              ├─► OpenRouter (tier 2)
   │                              └─► Gemini (tier 3)
   └─(router down / 503)──► native fallback ──► Gemini direct (break-glass)
```

---

## 2. Q1 — Tool-calling consistency

**Protocol.** All three speak OpenAI `tools` / `tool_choice` / `tool_calls`. The
differences are per model and per host, not per API:

- **NVIDIA (hosted NIM API).** Tool calling is enabled per model family with a
  model-specific parser. NIM's tool-calling list covers Llama 3.1/3.2/3.3, Qwen2.5,
  Mistral, Nemotron (Nano/Super/Ultra, Nemotron 3), GPT-OSS 20B/120B, and DeepSeek
  V3.1/V3.2. **DeepSeek-R1 is not on it.** It is a reasoning model that emits long
  `<think>`/`reasoning_content` and is slow, so keep it out of the agent loop. Typical
  Llama 3.x failures: the tool call comes back as JSON or `<|python_tag|>` text in
  `content`, and parallel calls are weak. Qwen2.5-**Coder** is code-tuned, not
  tool-tuned; prefer Instruct or Qwen3 variants.
- **OpenRouter.** It normalises the format, but the *host* behind a model decides
  whether `tools` work at all, and hosts differ in quantisation, hence reliability.
  The config sets `require_parameters: true` so OpenRouter only routes to hosts that
  support every parameter you send. Many `:free` hosts drop out as a result.
- **Gemini (OpenAI-compatible endpoint).** Tools work. Gemini **3.x** requires each
  tool call's *thought signature* to be echoed back. Generic OpenAI clients drop it,
  which causes **HTTP 400 "Function call is missing a thought_signature"**, especially
  when **failing over to Gemini mid tool loop**, because the earlier calls were made by
  another model. The router caches Gemini's signatures by tool-call id, re-attaches
  them, and gives foreign calls Google's documented dummy
  (`skip_thought_signature_validator`). Enable this per model with
  `gemini_thought_signatures` (true for 3.x, false for 2.5; Hermes itself gates
  signatures to 3+). Gemini also accepts only a subset of JSON Schema in tool
  definitions; `strip_schema_keys` removes offenders.

**Model choice.** Your three candidates date from 2024 to early 2025. The 2026 NVIDIA
catalog has models trained for agentic tool use: Nemotron 3 Super (Hermes's own
documented NVIDIA example), GPT-OSS-120B, DeepSeek V3.2+, Qwen3 and Kimi K2. I expect
them to beat Llama-3.3-70B and Qwen2.5-Coder, with R1 last. **Don't pick on
reputation; measure:**

```bash
python3 tool_conformance.py --provider nvidia --runs 5 \
  --model meta/llama-3.3-70b-instruct --model qwen/qwen2.5-coder-32b-instruct \
  --model openai/gpt-oss-120b --model nvidia/nemotron-3-super-120b-a12b
```

The benchmark runs six agent-realistic scenarios: single call; nested object +
enum + array args (a GTM tag); Arabic arguments; parallel calls; final answer after a
tool result; and *not* calling a tool when none is needed. It grades against the JSON
schema and reports pass %, malformed count and p50 latency.
**Bar for Tier 1: ≥ 95 % pass and 0 malformed over 5 runs.** Re-run whenever the
catalog changes. Even so, the router validates every tool call in production, so a
malformed call becomes a failover rather than a crashed agent step.

---

## 3. Q2 — 429s, cooldowns, not hanging

Principle: **in an agent loop, fail over fast; never back off in place.** A 30-second
sleep on NVIDIA is 30 seconds of a stuck Wesam request when OpenRouter or Gemini could
answer now.

| Upstream result | Router action | Penalty on that provider |
|---|---|---|
| 429 | next tier immediately | cooldown = `Retry-After` / `X-RateLimit-Reset` (5 s–1 h); otherwise 30 s × 2ⁿ up to 15 min, ±15 % jitter |
| 402 | next tier | 1 h |
| 401 / 403 | next tier, `ERROR` log | 30 min (fix the key) |
| 5xx, 408, timeout, connection error | next tier | circuit breaker: 3 in a row → 60 s × 2ⁿ up to 15 min |
| 200 but malformed tool call, unknown tool, empty, tool call leaked into text | next tier | counts toward the breaker |
| 400/413/422 "context length" | next tier | **none** (request problem, not provider health) |
| other 4xx (e.g. schema rejected) | next tier | none |
| key env var empty | skipped before any call | none (`skipped:no_key` in `/healthz`) |

Cooling-down providers are **skipped without spending a request**. A local token
bucket (`rpm`, set just under each free tier: NVIDIA ~40 RPM, OpenRouter free ~20 RPM)
spills bursts to the next tier *before* the upstream sends a 429.

- **State.** Cooldowns, failure counts and daily token use go to
  `/var/lib/hermes-llm-router/state.json` with atomic writes, so a restart doesn't
  hammer a provider that is still rate-limited.
- **Hermes never hangs.** The cascade has a hard deadline (`overall_deadline_s: 270`).
  When everything fails, the router returns **503 + `Retry-After`** quickly. Hermes's
  native fallback treats 503 as a trigger, benches the router until `Retry-After`, and
  answers from Gemini direct. `[VERIFY]` Hermes's own HTTP timeout for the main
  provider must be **longer than 270 s**; otherwise lower `overall_deadline_s` below it.
- **Mid-task switches are safe.** Conversation state lives in Hermes; each request
  carries the full history. The router makes the history portable: it strips
  `reasoning_content`/`reasoning`, removes Gemini-only fields for other providers, and
  adds signatures for Gemini. Streaming is **buffered upstream** (always
  `stream:false`) and re-emitted as SSE, so a failure can never happen half-way
  through a streamed answer. Costs: no token-by-token typing in the CLI, and you lose
  prompt caching when a session switches provider. Sticky sessions keep both rare.
- **ToS.** Don't stack several free NVIDIA or OpenRouter accounts to multiply rate
  limits. NVIDIA's hosted free tier is meant for development; production use means
  paid or self-hosted NIM under NVIDIA AI Enterprise `[VERIFY]` current terms. Hermes
  credential pools are for legitimately separate keys.

---

## 4. Q3 — Context window escalation

Two layers, so nothing ever fails on NVIDIA first:

1. **Router pre-flight.** It estimates prompt tokens as UTF-8 bytes / 3.2 (pessimistic
   on purpose: Arabic is 2 bytes per character and tokenises poorly; about 1.7
   characters per token in practice). Any provider where `estimate + max_output >
   95 % × context_window` is skipped, so a 150k-token conversation goes **directly to
   Gemini**. If the estimate is still wrong and the provider returns a context-length
   400, the router moves on without penalising it.
2. **Hermes compression.** The router advertises `context_length: 200000` on
   `/v1/models`. Hermes compresses history as it nears that, so a session never grows
   into 1M-token Gemini calls, which are slow and billed per call. Prompts between
   NVIDIA's window (~128k) and 200k use Gemini; beyond 200k, Hermes summarises.
   Raise `advertised_context` only if you really want long-context Gemini sessions.

Point Hermes's compression model at Gemini directly (config below). Compression
requests are the largest prompts Hermes sends, and Gemini has the window for them.

---

## 5. Q4 — Configuration

### 5.1 Router (this folder)

| File | Purpose |
|---|---|
| `router.py` | the router (starlette + httpx; ~600 lines) |
| `providers.yaml` | tiers, models, windows, RPM, timeouts, OpenRouter routing |
| `hermes-llm-router.service` | systemd unit: DynamicUser, loopback 127.0.0.1:**47311**, 256 MB cap |
| `tool_conformance.py` | Q1 benchmark |
| `tests/` | 27 tests: every failover path, SSE, signatures, persistence, chaos |

Port **47311**, not 8642: 8642 is Hermes's own default API port (this VPS moved it to
49872), and scanners probe it. Install steps are in the service file header.

### 5.2 Hermes `~/.hermes/config.yaml` (recommended hybrid)

```yaml
# Router as a named custom provider. Don't name it nvidia/openrouter/gemini:
# names that collide with a vendor break model routing (hermes-agent issue #123997).
providers:
  llm-router:
    api: "http://127.0.0.1:47311/v1"
    key_env: "HERMES_ROUTER_KEY"          # = ROUTER_API_KEY, in ~/.hermes/.env
    transport: "chat_completions"
    default_model: "hermes-auto"
    context_length: 200000                # = advertised_context in providers.yaml

# Select it as the main model. Easiest: run `hermes model` and pick llm-router, or
# in a session: /model custom:llm-router:hermes-auto — then `hermes config get model`
# to see exactly what your version wrote.  [VERIFY] the key layout below on your build.
model:
  provider: "custom:llm-router"
  default: "hermes-auto"
  context_length: 200000

# Break-glass: only fires if the router is down or returns 503.
fallback_providers:
  - provider: gemini
    model: gemini-2.5-flash               # needs GEMINI_API_KEY in ~/.hermes/.env

# Compression goes straight to Gemini (largest prompts; independent of router health).
auxiliary:
  compression:
    provider: gemini
    model: gemini-2.5-flash
```

`~/.hermes/.env` (chmod 600): `HERMES_ROUTER_KEY=<same value as ROUTER_API_KEY>` and
`GEMINI_API_KEY=…`. Hermes then holds **only** the router key and the break-glass Gemini
key; the NVIDIA and OpenRouter keys live in the router's root-only env file.

### 5.3 Native-only alternative (Approach A), if you decide against the router

```yaml
model:
  provider: nvidia
  default: nvidia/nemotron-3-super-120b-a12b
  context_length: 131072          # Hermes compresses to this — no escalation to Gemini
fallback_providers:               # one hop per turn: Gemini is reached only if OpenRouter
  - provider: openrouter          # is already benched when NVIDIA fails
    model: openai/gpt-oss-120b
  - provider: gemini
    model: gemini-2.5-flash
provider_routing:                 # OpenRouter
  require_parameters: true
  data_collection: deny
fallback:
  min_switch_reset_seconds: 0
auxiliary:
  compression:
    provider: gemini
    model: gemini-2.5-flash
```

`auth.json` is **managed by Hermes**. Don't hand-edit it. Keys go in `~/.hermes/.env`
(`NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`), and Hermes records pool
entries with `"source": "env:NVIDIA_API_KEY"`. Use the interactive `hermes auth`
wizard. Avoid `hermes auth add … --api-key <key>`, which writes the key into shell
history. Inspect with `hermes auth list`, and clear cooldowns with
`hermes auth reset <provider>`.

---

## 5.4 Zero-cost profile and stale-if-error

`providers.free-tier.yaml` uses free tiers only, in this order: NVIDIA (~40 RPM) →
Gemini Flash → Gemini Flash-Lite (separate per-model daily quotas) → OpenRouter `:free`
(its own multi-model fallback inside one request).

- **Gemini's daily quota.** Gemini signals an exhausted daily quota in the response body
  (`RetryInfo.retryDelay` / "retry in 8h"). The router reads that and benches the model
  until the reset (capped at 24 h) instead of re-probing it every few minutes.
- **`stale_if_error_s`.** When *every* provider is exhausted at once, an identical earlier
  request is answered from cache with `x-router-cache: stale`. A new request still gets
  an honest 503. It is off unless configured; the free-tier profile sets 48 h.
- **Privacy.** Free tiers may train on prompts, so don't route real client data through
  this profile.

## 6. Verification protocol (production)

Run in order. Every step lists the exact expected evidence. **Stop at the first
mismatch.**

```bash
# helper: call the router without printing the key
rq() { sudo bash -c 'set -a; . /etc/hermes-llm-router/router.env; set +a; curl -s "$@" -H "Authorization: Bearer $ROUTER_API_KEY"' _ "$@"; }
ask() { rq -D - -o /tmp/r.json http://127.0.0.1:47311/v1/chat/completions -H 'Content-Type: application/json' \
  -d "{\"model\":\"hermes-auto\",\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}" | grep -iE '^HTTP|x-router-provider|retry-after'; }
logs() { journalctl -u hermes-llm-router -n 5 --no-pager -o cat; }
```

| # | Do | Expect |
|---|---|---|
| 0 | `cd /opt/hermes-llm-router && venv/bin/python -m pytest tests -q` | `27 passed` |
| 1 | `rq http://127.0.0.1:47311/healthz` | 3 providers, `key_present: true`, `cooling_down_s: 0` |
| 2 | `venv/bin/python tool_conformance.py --runs 5` (keys exported from router.env) | Tier 1 ≥ 95 %, 0 malformed; otherwise change the model |
| 3 | `ask "baseline 1"` | `200`, `x-router-provider: nvidia` |
| 4 | **Enable drill mode** (below), write `{"nvidia":429}` to the chaos file, `ask "drill 2"` | `200`, `openrouter`; log shows `nvidia:rate_limited` |
| 5 | write `{}`, `ask "drill 3"` | still `openrouter`, log `nvidia:skipped:cooldown` (no request spent) |
| 6 | write `{"openrouter":503}`, `ask "drill 4"` | `200`, `gemini` |
| 7 | write `{"openrouter":503,"gemini":"timeout"}`, `ask "drill 5"` | `503` + `retry-after`; body lists every attempt |
| 8 | write `{"nvidia":"malformed"}`, then in **Hermes** ask: *"Use the terminal tool to run `uname -r` and tell me the kernel"* | answer arrives; log `nvidia:invalid_output` → `openrouter:ok` |
| 9 | **Gemini mid tool loop.** Write `{}`; in Hermes start a multi-step task (*"list /var/log, then show the 3 largest files"*); after the first tool call write `{"nvidia":503,"openrouter":503}` | task completes on `gemini`, **no 400 thought_signature** in logs. If 400: set `gemini_thought_signatures: true` (or the opposite for 2.5) and repeat |
| 10 | `sudo systemctl restart hermes-llm-router`; `rq …/healthz` | cooldowns from steps 4–9 still counting down (persisted) |
| 11 | `sudo systemctl stop hermes-llm-router`; ask Hermes anything | Hermes answers via native **gemini** fallback (break-glass); then `start` |
| 12 | **Disable drill mode**, restart; `rq …/healthz` | `ROUTER_ENABLE_CHAOS` absent; after cooldowns expire, `ask` → `nvidia` |

**Drill mode** is off by default, so faults can never be injected in production by
accident:

```bash
sudo systemctl edit hermes-llm-router      # add:
#   [Service]
#   Environment=ROUTER_ENABLE_CHAOS=1
#   Environment=ROUTER_CHAOS_FILE=/var/lib/hermes-llm-router/chaos.json
sudo systemctl restart hermes-llm-router
echo '{"nvidia":429}' | sudo tee /var/lib/private/hermes-llm-router/chaos.json   # DynamicUser state path
# faults: <HTTP status> | "timeout" | "malformed" | "context"; {} = none
# afterwards: sudo systemctl revert hermes-llm-router && sudo systemctl restart hermes-llm-router
```

Faults are injected **inside the router**; nothing is sent upstream and no quota is
spent. The same drill passed locally against a real uvicorn process before this was
committed (steps 3–8 equivalents).

### Ongoing monitoring

One JSON line per request (`served_by`, `attempts`, latency; never message content):

```bash
journalctl -u hermes-llm-router --since "1 hour ago" -o cat | grep -c '"served_by": null'      # exhausted → alert if > 0
journalctl -u hermes-llm-router --since "1 hour ago" -o cat | grep -o '"served_by": "[a-z]*"' | sort | uniq -c
```

Alert when Tier 1 serves < 70 % of traffic for an hour (free tier exhausted, or a key
or model problem), or on any `router_exhausted`.

### Firewall

The `HERMES_EGRESS` rules in `../README.md` §D.0 already allow loopback port 47311. If
you applied an earlier copy of those rules, add it before the REJECT lines:
`iptables -I HERMES_EGRESS 4 -o lo -p tcp --dport 47311 -j ACCEPT`

---

## Sources

- Hermes fallback providers: `website/docs/user-guide/features/fallback-providers.md` in NousResearch/hermes-agent
- Hermes credential pools: `website/docs/user-guide/features/credential-pools.md`
- Hermes providers (nvidia, gemini, OpenRouter routing, custom `providers:`): `website/docs/integrations/providers.md`
- hermes-agent issues #120608 (fallback skipped on unreachable primary, open), #17929 (single-key pool init error, closed), #19883 (NIM reasoning_effort, open), #123997 (custom provider name collision); PR #124090 (Gemini signatures gated to 3+)
- NVIDIA NIM for LLMs — function calling / supported models: docs.nvidia.com/nim/large-language-models/
- Gemini 3 thought-signature 400s with OpenAI-compatible clients: discuss.ai.google.dev thread "OpenAI API compatibility broken due to thought_signature"; vectorize-io/hindsight #5122 (failover to Gemini mid tool loop)
- LiteLLM PyPI compromise (March 2026): Datadog Security Labs, Trend Micro write-ups
