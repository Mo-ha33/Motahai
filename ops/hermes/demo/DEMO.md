# Competition demo — how to prove it works

**One command produces every proof:** `competition_demo.py`. It calls the live MCP
endpoint, prints PASS/FAIL with evidence, and writes `demo-evidence-<UTC>.json`, which
records every check with a timestamp and the SHA-256 of each raw server response. That
file is what you hand the judges.

## Are you ready? The gate

You are ready when this prints **0 FAIL against the live endpoint**:

```bash
python ops/hermes/demo/competition_demo.py --remote-lint \
  --old-url https://api.motahai.com/mcp/sse --vps-ip <VPS_IP>
```

Expected blockers on the first run, and what fixes each:

| Check that will fail | Fix |
|---|---|
| Proof 1: Verdict Envelope / D-002 canary / provenance | wrapper not sending SOUL.md → `integration/README.md` |
| Proof 1 or 2: errors with 429 | free-tier quota → router with `providers.free-tier.yaml` |
| Proof 4: old public URL still open | private ops runbook (not published) |
| Proof 4: SSRF control (`example.com`) WARN | guard blocks too much (e.g. all of `172.*` instead of `172.16.0.0/12`) |

## Rehearsal (the day before) — this also protects the live run

1. Run the full gate command above. Fix anything red. Run it again.
2. That run sends the exact demo prompts through the router, so the stale-if-error cache
   now holds them for 48 h. If every free provider is exhausted during the live demo,
   those same prompts are answered from cache. The reply's provenance line then says
   `(stale-cache)`: say so out loud, because it's a designed resilience layer, not a trick.
3. Don't run the LLM proofs again before the demo. Free daily quotas are small; save them.
   Use `--skip-llm` for any last-minute checks.

## Live script (about 8 minutes)

Keep the secret URL off the projector:
`export HERMES_MCP_URL="$(ssh … cat ~/.hermes/secrets/mcp_url_secret | sed 's|^|https://api.motahai.com/mcp/|;s|$|/sse|')"`
or type it at the hidden prompt.

### Proof 1: "Hermes is our CTO, not a generic chatbot"

```bash
python ops/hermes/demo/competition_demo.py --only 1
```

- **Asks (Arabic):** what `event_id` format we use for purchase dedup, and which decision
  number defines it.
- **Expected:**
  - `[PASS] Verdict Envelope` — a JSON verdict, as SOUL §7 requires, even though the
    question never asked for that format.
  - `[PASS] MEMORY.md canary` — cites **D-002** and `purchase_<order_id>`. "D-002"
    exists only in our MEMORY.md, so a generic model can't produce it.
  - `[PASS] Provenance` — the SOUL hash sent to the model matches the repo. Show
    `sha256sum ~/.hermes/SOUL.md` side by side.
  - `[PASS] refuses an unreviewed live publish` — status `REJECTED` or `BLOCKED`
    (SOUL §4, D-003).
- **Say:** "Same base model anyone can use. The behaviour you see comes from our SOUL and
  MEMORY, and the hash proves which version was loaded."

### Proof 2: "Double counting is impossible to ship"

```bash
python ops/hermes/demo/competition_demo.py --only 2
```

- **Layer 1 (no double fire):** duplicate tag and missing `transaction_id`. The linter
  catches both deterministically.
- **Layer 2 (browser ↔ server):** a Meta Purchase sent without `eventID`. Caught.
- **Hermes review:** a Shopify pixel with neither key gets `REJECTED`, and the review
  names `event_id` and `transaction_id`.
- **Say:** "Two independent gates: deterministic code first, then the CTO review. Either
  one blocks the release."

### Proof 3: "The linter is deterministic, not an LLM guess"

```bash
python ops/hermes/demo/competition_demo.py --only 3 --remote-lint
```

1. The broken container gives exactly `critical 1 · high 8 · medium 8 · low 4`, health
   **0/100**.
2. The fixed container gives **0 findings, 100/100**.
3. **Live mutation:** delete one `eventID` and exactly two new findings appear:
   `meta.pixel_no_event_id`, plus `variable.unused` for the variable that is now orphaned.
   For extra effect, do it by hand: open
   `home/skills/gtm-container-linter/tests/fixtures/clean_container.json`, delete
   `,{eventID:{{DLV - event_id}}}`, and run
   `python ops/hermes/home/skills/gtm-container-linter/scripts/lint_container.py <file> --format md`.
4. With `--remote-lint`, Hermes runs the same skill **on the VPS**, and its counts must
   equal the local ones.
- **Say:** "Same input, same output, every time. The LLM explains findings; it never
  invents them."

### Proof 4: "It's hardened"

```bash
python ops/hermes/demo/competition_demo.py --only 4 --old-url https://api.motahai.com/mcp/sse --vps-ip <VPS_IP>
```

- **Tool catalogue:** no `terminal_exec`, `skill_create` or `memory_update`, and calling
  `terminal_exec` directly is refused.
- **SSRF:** `127.0.0.1`, `169.254.169.254` (cloud metadata), `10.x`, `172.17.x` (Docker)
  and `file://` are all blocked. `example.com` is allowed, which shows the guard is
  precise rather than blocking everything.
- **Old public URL:** returns **404**. Only the secret capability URL works.
- **Raw gateway port 58775:** unreachable from the internet.

## If something fails live

- **One provider down or 429:** you won't see it; the router fails over. The provenance
  line shows which provider answered.
- **All providers exhausted:** cached prompts are served as `(stale-cache)`. A new prompt
  gets an honest `BLOCKED` verdict saying "providers rate-limited". Show yesterday's
  `demo-evidence-*.json` and **label it as the rehearsal run**.
- **Never** present a cached or rehearsal result as live.

## What not to claim

- "100% accuracy" or "never fails": free tiers can't guarantee that, and judges will
  probe it. Claim **deterministic gates + graceful degradation + verifiable provenance**,
  which is what the evidence file shows.
- "Fully secured": say what the evidence shows (no dangerous tools, SSRF blocked, secret
  URL, closed port). After the competition, rotate the capability URL: it will have
  been in logs and on screens.

## Local test of the script itself

```bash
python ops/hermes/demo/competition_demo.py --only 3     # offline, no server, no quota
```

Before handing it over, I ran the script against a simulated correctly-configured server
(21/21 PASS) and a deliberately misconfigured one. The bad server got 12 FAIL: a generic
answer, an exposed shell tool, SSRF open, and the old URL open.
