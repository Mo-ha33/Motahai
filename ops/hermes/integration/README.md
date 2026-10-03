# Wiring SOUL.md + the free-tier router into `hermes_mcp`

Two problems, one change:

| Problem today | Cause | Fix |
|---|---|---|
| `hermes_consult` dies with Gemini 429 | one free Gemini key, no failover | call the local **llm-router** (NVIDIA → Gemini ×2 → OpenRouter) |
| No proof SOUL.md is used | the wrapper may call Gemini directly without a system prompt | `soul_router_client.consult()` always sends SOUL.md + MEMORY.md and returns a **provenance line** with their SHA-256 |

## 1. Find out which path your wrapper uses (read-only)

```bash
grep -nE "generativelanguage|googleapis|genai|subprocess|hermes -z|SOUL|system_instruction" ~/hermes_mcp/*.py ~/hermes_sse_server.py
```

- **Direct Gemini** (`generativelanguage` / `genai` hits, no `SOUL`): SOUL is *not* being
  sent today. Use steps 2–4.
- **Hermes CLI** (`subprocess` / `hermes -z`): Hermes loads SOUL.md itself. Point Hermes at
  the router instead (`llm-router/README.md` §5.2) and skip step 3. You still get the
  failover, but not the provenance line.

## 2. Run the router with the zero-cost profile

Follow the install steps at the top of `llm-router/hermes-llm-router.service`, with one
change in the unit:

```ini
Environment=ROUTER_CONFIG=/opt/hermes-llm-router/providers.free-tier.yaml
```

`/etc/hermes-llm-router/router.env` (root:root 0600; edit with `sudoedit`, never `echo`):

```
ROUTER_API_KEY=<openssl rand -hex 24>
NVIDIA_API_KEY=...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
```

Check it: `curl -s -H "Authorization: Bearer <ROUTER_API_KEY>" http://127.0.0.1:47311/healthz`.
All four providers should show `key_present: true` and `cooling_down_s: 0`.

## 3. Drop the client into the wrapper

```bash
cp ops/hermes/integration/soul_router_client.py ~/hermes_mcp/
( umask 077; printf 'HERMES_ROUTER_KEY=%s\nHERMES_ROUTER_URL=http://127.0.0.1:47311/v1/chat/completions\n' \
    "<same ROUTER_API_KEY>" > ~/.hermes/router-client.env )
sudo systemctl edit hermes-mcp.service      # add:  [Service]  EnvironmentFile=/home/deploy/.hermes/router-client.env
```

In the executor, where `hermes_consult` currently calls Gemini:

```python
from soul_router_client import RouterExhaustedError, RouterUnavailableError, consult

def run_consult(prompt: str, context: str | None = None, **_ignored) -> str:
    try:
        r = consult(prompt, context=context)
        return f"{r['text']}\n\n{r['provenance']}"
    except RouterExhaustedError:
        return ("```json\n{\"status\": \"BLOCKED\", \"decision\": \"All LLM providers are rate-limited; "
                "retry later.\", \"next_agent\": \"lead-orchestrator\"}\n```")
    except RouterUnavailableError:
        return existing_direct_gemini_call(prompt, context)      # keep today's path as break-glass
```

`hermes_audit_code` uses the same function. Pass the code as context:

```python
def run_audit(code: str, file_path: str | None = None, focus_areas: list[str] | None = None, **_):
    prompt = (f"Audit this code for: {', '.join(focus_areas or ['security', 'deduplication'])}. "
              f"File: {file_path or 'n/a'}. Reply with hermes.verdict.v1.")
    return run_consult(prompt, context=f"```\n{code}\n```")
```

The `reasoning_effort` argument is accepted and ignored: not every free provider supports
it, and forwarding it would make the router skip those providers.

Restart and test: `git -C ~/hermes_mcp commit -am "route consult via llm-router + SOUL"`,
then `sudo systemctl restart hermes-mcp.service`.

## 4. Verify

```bash
sha256sum ~/.hermes/SOUL.md | cut -c1-16      # must equal the "SOUL sha256:" in every consult reply
journalctl -u hermes-llm-router -n 5 -o cat   # one JSON line per call: served_by, attempts
```

Then run the demo script's proof 1 (`ops/hermes/demo/DEMO.md`).

Tests: `python -m pytest tests -q` (5 tests, against a real local HTTP server).
