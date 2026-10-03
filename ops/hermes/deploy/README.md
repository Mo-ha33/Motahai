# Deploying the Hermes blueprint to the VPS

`deploy_blueprint.sh` pushes `ops/hermes/home/` to `/home/deploy/.hermes/` and restarts
`hermes-mcp.service`. Run it **from your own machine** (WSL or Git Bash, from the repo
root). Your SSH key never leaves your machine.

## What gets installed

| Repo | VPS | On re-deploy |
|---|---|---|
| `home/SOUL.md` | `~/.hermes/SOUL.md` | replaced |
| `home/MEMORY.md` | `~/.hermes/MEMORY.md` | first time: **merged** (old memory preserved under a heading); later: live file kept, new version written as `MEMORY.blueprint.md` |
| `home/memories/client-ledger.md` | `~/.hermes/memories/client-ledger.md` | live file kept if it changed; new version written as `*.blueprint` |
| `home/memories/gtm-patterns.md` | `~/.hermes/memories/gtm-patterns.md` | same |
| `home/memories/incident-log.md` | `~/.hermes/memories/incident-log.md` | same |
| `home/skills/gtm-container-linter/` | `~/.hermes/skills/gtm-container-linter/` | replaced |

File names stay as they are in the repo, because `MEMORY.md` refers to them by name.
Nothing else in `~/.hermes` is touched: `.env`, `config.yaml`, `auth.json`, other
skills and the venv are left alone. `skills/README.md` is a design catalogue and is
not deployed.

## Run

```bash
# 1) see the plan; changes nothing
HERMES_HOST=<vps-ip> bash ops/hermes/deploy/deploy_blueprint.sh --dry-run

# 2) deploy
HERMES_HOST=<vps-ip> bash ops/hermes/deploy/deploy_blueprint.sh

# 3) undo (the exact command is printed at the end of every deploy)
HERMES_HOST=<vps-ip> bash ops/hermes/deploy/deploy_blueprint.sh --rollback /home/deploy/hermes-backups/blueprint-<ts>.tgz
```

Defaults: port 2222, user `deploy`, key `~/.ssh/id_ed25519`, service `hermes-mcp.service`.
Override with `HERMES_SSH_PORT`, `HERMES_SSH_USER`, `HERMES_SSH_KEY`, `HERMES_SERVICE`.
Other flags: `--memory merge|replace|keep` (default `merge`) and `--no-restart`.

If `deploy`'s sudo needs a password, the restart step prompts for it (`ssh -t`).

## Steps

1. **Preflight.** Local files exist and the linter runs locally. Over SSH: Hermes home
   and service exist; reports Python version, free disk, and which paths already exist.
2. **Backup.** Every path it will touch goes to `~/hermes-backups/blueprint-<ts>.tgz`,
   plus a manifest of what existed before (chmod 600).
3. **Upload** to `~/.hermes/.blueprint-staging-<ts>`.
4. **Self-test on the VPS.** The linter must run under the VPS Python and catch the
   planted defects in its fixture. pytest also runs if it is installed. If this fails,
   nothing is installed and nothing is restarted.
5. **Install** as in the table above. `chown` only on the deployed paths.
6. **Restart** `hermes-mcp.service`. It waits up to 30 s for `active` and shows status
   plus the last 30 log lines. If the service isn't active, it stops and prints the
   rollback command.
7. **Verify** that SHA-256 checksums of SOUL.md and the skill match the repo, and
   checks that something listens on `:58775`.

Tested in the authoring sandbox against a simulated VPS (fake `ssh`, `systemctl` and
`sudo` running the real remote scripts): dry run, first deploy (memory merged), a
re-deploy after Hermes had written new facts (kept), a failed restart (stops with the
rollback hint), and a rollback (byte-for-byte original state; unrelated skills
untouched). It has **not** been run against the real VPS.

## After deploying

- On the first deploy, read `MEMORY.md` and trim the "Preserved pre-blueprint memory"
  section into the index format. The script warns if the file is over 3.5 KB.
- Fill in the `<<REQUIRED>>` fields in `memories/client-ledger.md` (GTM public ID,
  account ID, domains), so the Auditor can run the first baseline lint.
- Run SOP Recipe 1 from Wesam. The reply should be one `hermes.verdict.v1` JSON block.
