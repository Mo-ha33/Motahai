# Hermes Sentinel: Operator Runbook

For the person who receives the alerts and keeps the VPS running. You do not need to write code.
Every command below runs on the VPS, in a terminal, as the `deploy` user, unless it says `sudo`.

Placeholders are written as `<LIKE_THIS>`. Replace them with your real values. Never paste real
values into the repository, a chat, a ticket or a screenshot.

---

## 1. What it watches and why

The sentinel tells you when AI capacity is running out or stalling, while you are away from
the screen. It sends alerts to the admin on Telegram, and by email for critical alerts when email
is configured. It runs once a minute on its own timer.

It watches two LLM layers, and it sees them in different ways:

| Layer | What it is | How the sentinel knows |
|---|---|---|
| Wesam's model (the agent) | The agent that talks to clients and works through flows | **Inferred from silence.** The sentinel reads one flow-state file per GTM container, written by hermes-mcp. If an agent owes a step and its deadline passes (300 s by default), that is a `flow_stalled` alert. The sentinel never calls the model itself. |
| Hermes LLM router | The local router in front of the NVIDIA, OpenRouter and Gemini tiers | **Observed directly.** The sentinel reads the router's journal lines and its `/healthz` status page. |

The sentinel does **not** call any LLM. It keeps working when every model balance is $0,
which is exactly when you need it.

Names used in this runbook:

- `SENTINEL_HOME` = `/home/deploy/hermes-sentinel` (the code)
- `HERMES_SENTINEL_DIR` = `/home/deploy/.hermes/sentinel` (state: `flow/`, `contacts.json`, `silence.json`)
- The secrets file = `/home/deploy/.hermes/.env` (permissions `0600`)
- `ROUTER_URL` = the router's address. The default is `http://127.0.0.1:47311`, the port in `hermes-llm-router.service`.

---

## 2. One-time setup

### 2.1 Environment variables

Put these lines in `/home/deploy/.hermes/.env`. Do not add quotes unless a value contains spaces.

| Name | Required | What it is |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Token of the alert bot. Ask the developer; it is shown once by BotFather. |
| `ADMIN_TELEGRAM_CHAT_ID` | yes | The admin's chat id. See 2.2. |
| `ADMIN_ALERT_EMAIL` | for email | Where admin email alerts go, `<ADMIN_ALERT_EMAIL>` |
| `MOTAHAI_SMTP_HOST` | for email | Mail server name, `<SMTP_HOST>` |
| `MOTAHAI_SMTP_PORT` | for email | Usually `587` (STARTTLS) or `465` (TLS), `<SMTP_PORT>` |
| `MOTAHAI_SMTP_USER` | for email | Mail account name, `<SMTP_USER>` |
| `MOTAHAI_SMTP_PASSWORD` | for email | Mail password. Many providers need an app-specific password. `<SMTP_PASSWORD>` |
| `MOTAHAI_SMTP_FROM` | for email | Sender address, `<SMTP_FROM_ADDRESS>` |
| `ROUTER_API_KEY` | yes | The same key Hermes uses for the router. The sentinel uses it to read `/healthz`. It is the same value as in the router's own env file. |
| `ROUTER_URL` | no | The router's address. Default `http://127.0.0.1:47311`. Set it only if the router port changes. |
| `HERMES_SENTINEL_DIR` | no | Defaults to `/home/deploy/.hermes/sentinel`. If you change it, change the `ReadWritePaths=` line in the service file too. |
| `SENTINEL_FLOW_DEADLINE_S` | no | Seconds an agent may owe a step before `flow_stalled`. Default `300`. |
| `SENTINEL_HEARTBEAT_URL` | no | Ping URL of a free healthchecks.io check, `<HEARTBEAT_URL>`. Treat it as a secret. See 2.6. |

If SMTP is not configured, email is skipped and the journal says so. Telegram still works.

To see which names are set, without showing any value, run:

```bash
cut -d= -f1 /home/deploy/.hermes/.env
```

Check the file permissions. The output must start with `-rw-------`:

```bash
ls -l /home/deploy/.hermes/.env
```

If it does not, run `chmod 600 /home/deploy/.hermes/.env`.

### 2.2 Get the admin's Telegram chat id

The Bot API cannot message a phone number. The admin must open the bot first.

1. The admin opens the alert bot in Telegram and presses **Start** (or sends `/start`).
2. On the VPS, load the token into the shell without printing it, then ask Telegram for the updates:

   ```bash
   set -a; . /home/deploy/.hermes/.env; set +a
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"
   ```

3. In the output, find `"chat":{"id": <NUMBER>, ...}`. That number is `ADMIN_TELEGRAM_CHAT_ID`.
   Group chats have negative numbers. Keep the minus sign.

Notes:

- If the result is `"result":[]`, the admin has not messaged the bot yet. Repeat step 1.
- If you see `409 Conflict`, the bot has a webhook set. Tell the developer. Do not change it yourself.
- The output contains the names of people who wrote to the bot. Do not paste it anywhere.

### 2.3 Check the SMTP server

This check only proves that the mail server answers and its TLS certificate is valid. It does
not send anything. The real end-to-end test is drill (d).

For port 587 (STARTTLS):

```bash
set -a; . /home/deploy/.hermes/.env; set +a
openssl s_client -starttls smtp -connect "$MOTAHAI_SMTP_HOST:$MOTAHAI_SMTP_PORT" -crlf </dev/null 2>/dev/null | head -n 5
```

For port 465 (TLS from the start), use `openssl s_client -connect "$MOTAHAI_SMTP_HOST:$MOTAHAI_SMTP_PORT" </dev/null`.

Expected: a line starting with `220` (the server greeting). If you get `Connection refused`, `Name or service not known`
or nothing at all, the host or port is wrong, or the VPS cannot reach the mail server.

### 2.4 Create `contacts.json` (client emails)

Client emails are sent only to contacts listed in this file. Each entry is keyed by the client's
GTM container ID (`GTM-XXXXXXX`). Create the file in the state directory:

```bash
install -d -m 0700 /home/deploy/.hermes/sentinel
nano /home/deploy/.hermes/sentinel/contacts.json
chmod 600 /home/deploy/.hermes/sentinel/contacts.json
```

Format (JSON, double quotes, no trailing commas):

```json
{
  "GTM-XXXXXXX": {
    "email": "<CLIENT_EMAIL>",
    "store_name": "<STORE_NAME>",
    "lang": "ar",
    "tz": "Asia/Riyadh"
  }
}
```

- `email` is the address that receives client messages.
- `store_name` is the store's display name.
- `lang` is the language code for the client message, for example `ar`.
- `tz` is the client's timezone as an IANA name. It is optional and defaults to `Asia/Riyadh`. Client
  messages are sent only between 09:00 and 20:00 in this timezone. Outside those hours the sentinel
  waits and sends them later, when the window opens. An unknown name falls back to the default.

If a GTM container has no entry, no client email is sent. The admin alert says
`client not notified: no contact on file`. That is the correct behaviour, not an error. Add the entry
if the client should be notified.

Check that the file is valid JSON before you rely on it:

```bash
python3 -m json.tool /home/deploy/.hermes/sentinel/contacts.json >/dev/null && echo "valid JSON"
```

### 2.5 Install the code and the timer

Run these once. The package folder `ops/hermes/sentinel` from the repository must end up at
`SENTINEL_HOME/sentinel/`, so that `SENTINEL_HOME/sentinel/__main__.py` exists. Copy the files
from a checkout made by the developer. Do not edit code on the server.

```bash
install -d -m 0755 /home/deploy/hermes-sentinel
# copy the package folder from the checkout to /home/deploy/hermes-sentinel/sentinel/
python3 -m venv /home/deploy/hermes-sentinel/.venv
# only if the package ships a requirements file:
# /home/deploy/hermes-sentinel/.venv/bin/pip install -r <REQUIREMENTS_FILE>

install -d -m 0700 /home/deploy/.hermes/sentinel
install -d -m 0700 /home/deploy/.hermes/sentinel/flow
```

Install the two unit files. They come from `ops/hermes/sentinel/deploy/` in the checkout:

```bash
sudo install -m 0644 -o root -g root /home/deploy/hermes-sentinel/sentinel/deploy/hermes-sentinel.service /etc/systemd/system/
sudo install -m 0644 -o root -g root /home/deploy/hermes-sentinel/sentinel/deploy/hermes-sentinel.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-sentinel.timer
```

Do **not** enable `hermes-sentinel.service` by itself. The timer starts it.

### 2.6 Optional: heartbeat (watch the sentinel itself)

If the sentinel dies, nothing tells you. A heartbeat fixes that. Create a free check on
healthchecks.io with a period of 2 minutes and a grace time of 5 minutes. Put its ping URL in
`SENTINEL_HEARTBEAT_URL`. The sentinel pings it at the end of each successful pass. If the
pings stop, the service sends you a message.

### 2.7 Verify the setup

```bash
systemctl list-timers hermes-sentinel.timer --no-pager
```

You should see a `NEXT` time within a minute, and a `LEFT` time under a minute.

```bash
journalctl -u hermes-sentinel -n 30 --no-pager
```

After the first pass you should see a line for the pass and no error. A pass with no open incidents
is silent. No message in Telegram is the correct result when everything is healthy.

Check the router status page:

```bash
set -a; . /home/deploy/.hermes/.env; set +a
ROUTER="${ROUTER_URL:-http://127.0.0.1:47311}"
curl -s -H "Authorization: Bearer $ROUTER_API_KEY" "$ROUTER/healthz"
```

You should see one entry per provider, with `"key_present": true` and `"cooling_down_s": 0`.

---

## 3. Alert reference

Severity decides the channels:

- **CRIT**: Telegram **and** email when opened, every 6 hours while it stays open, and on RECOVERED.
- **WARN**: Telegram only. Email is used only if Telegram fails.

| Kind | Severity | What it means | First action |
|---|---|---|---|
| `router_exhausted` | CRIT | The router has no tier left that can answer. Every provider is cooling down, over its daily budget, or failing. Requests get `503` with `router_exhausted`. | Run the status check in 2.7. Look at `cooldown_kind`, `requests_today` against `daily_request_budget`, and `last_error` for each provider. If the cause is a daily quota, nothing needs fixing until the quota resets at Pacific midnight. Do not change keys in a panic. |
| `router_unreachable` | CRIT | The sentinel could not reach the router's `/healthz` on two passes in a row. The service is down, restarting, or the key is wrong. A single failed pass does not alert. | Run `systemctl status hermes-llm-router --no-pager`. If it is stopped, run `sudo systemctl restart hermes-llm-router`. If it keeps stopping, send the last 50 journal lines to the developer: `journalctl -u hermes-llm-router -n 50 --no-pager`. |
| `provider_degraded` | CRIT | One provider is failing: authentication errors (`401`/`403`), server errors, or timeouts. | Look at the journal for that provider's name: `journalctl -u hermes-llm-router -n 200 --no-pager \| grep <PROVIDER_NAME>`. A `401` or `403` means the key is wrong or expired, so ask the developer to rotate it. Server errors and timeouts usually clear by themselves. |
| `flow_stalled` | CRIT | An agent owes a step and has missed its deadline (300 s by default). The client may be waiting. The incident key is `flow:<GTM-ID>:<seq>`. | Open the flow file `HERMES_SENTINEL_DIR/flow/<GTM-ID>.json` named in the message. Check that Wesam's agent and hermes-mcp are running: `systemctl status hermes-mcp --no-pager`. If the message says `client not notified: no contact on file`, see 2.4. |
| `budget_80` | WARN | A provider has used 80% of its daily request budget. It is a warning only. There is no RECOVERED message for this kind. | Nothing is urgent. The budget stops traffic before the provider's own `429`. Plan heavy work for after the Pacific-midnight reset. |
| `sentinel_error` | WARN | The sentinel itself failed a pass, for example it could not read the journal or a state file. | Run `journalctl -u hermes-sentinel -n 100 --no-pager`. See section 6 for the common causes. |
| RECOVERED | follows the kind | An open incident has cleared. Its kind is named in the message. CRIT RECOVERED goes to Telegram and email. | No action. It is not a new problem. |

Open CRIT incidents repeat every 6 hours until they clear. A repeat means the problem is still open, not that it happened again.

If Telegram fails, the sentinel retries once after 5 seconds. If that fails too, it sends the alert by
email. If both fail, the failure is written to the journal and the pass still succeeds. A failed
channel does not produce a `sentinel_error`, so check the journal if you expected a message and
got nothing.

---

## 4. Silencing and un-silencing

Silencing stops the messages for an incident. Detection keeps running, so the incident is still
tracked. Use silencing only for planned work or a known issue, and always with an end time.

The silence file is `/home/deploy/.hermes/sentinel/silence.json`. Each key is an incident key, or a
prefix of one. An incident matches a key when its incident key is equal to the key, or starts with the
key. So `provider_degraded` silences every provider incident, and `flow:GTM-7K2Q9XM` silences every
stall for that GTM container, whatever its seq. The value is the end time in UTC, in ISO 8601 format,
ending with `Z`.

Example: silence all `budget_80` warnings until 18:00 UTC on 12 October 2026, all provider
degradation until 06:00 UTC the same day, and one container's flow stall until 09:00 UTC the same day.

```json
{
  "budget_80": "2026-10-12T18:00:00Z",
  "provider_degraded": "2026-10-12T06:00:00Z",
  "flow:GTM-7K2Q9XM": "2026-10-12T09:00:00Z"
}
```

To set an end time six hours from now, run:

```bash
date -u -d "+6 hours" +%Y-%m-%dT%H:%M:%SZ
```

To un-silence, delete that line from the file, or set its time to an earlier moment. Then check
that the file is still valid JSON:

```bash
python3 -m json.tool /home/deploy/.hermes/sentinel/silence.json >/dev/null && echo "valid JSON"
```

---

## 5. Drills

Run drills when the team has time to watch. Do not run drills during a live client session. Each
drill has a precondition, the steps, the expected result, and a cleanup. **Do the cleanup even if
the drill fails.**

Drill (a) needs a router build that has the drill fault `429daily`. That fault is in commit
`87caa5b` on branch `fix/free-tier-router-gtm-hardening`, and in any later build. Check before you
start: the `/healthz` output must contain `"requests_today"`. If it does not, stop and ask the developer.
Drills (b), (c) and (d) work with any build.

Before any drill, load the router key into your shell, and keep the terminal open for the whole drill:

```bash
set -a; . /home/deploy/.hermes/.env; set +a
ROUTER="${ROUTER_URL:-http://127.0.0.1:47311}"
```

Ping the router (this sends one request; a successful one reaches a real provider):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$ROUTER/v1/chat/completions" \
  -H "Authorization: Bearer $ROUTER_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"hermes-auto","messages":[{"role":"user","content":"drill ping"}]}'
```

The provider names are whatever `/healthz` lists, for example `nvidia`, `openrouter`, `gemini`. Use the
names your build shows, not the ones in this runbook.

### Drill (a): every tier exhausted (expect 1 CRIT, then 1 RECOVERED)

**Precondition:** the router is running and healthy. Note the provider names from `/healthz`.

1. Turn on drill mode. This creates a drop-in file, then restarts the router:

   ```bash
   sudo mkdir -p /etc/systemd/system/hermes-llm-router.service.d
   sudo tee /etc/systemd/system/hermes-llm-router.service.d/drill.conf >/dev/null <<'EOF'
   [Service]
   Environment=ROUTER_ENABLE_CHAOS=1
   Environment=ROUTER_CHAOS_FILE=/var/lib/hermes-llm-router/chaos.json
   EOF
   sudo systemctl daemon-reload
   sudo systemctl restart hermes-llm-router
   ```

2. Write the fault for every provider name from `/healthz`. The example has three names. Replace them
   with yours:

   ```bash
   echo '{"nvidia":"429daily","openrouter":"429daily","gemini":"429daily"}' | sudo tee /var/lib/hermes-llm-router/chaos.json >/dev/null
   ```

3. Send three pings with the command above. Each should print `503`.
4. Wait 2 minutes.

**Expected:** exactly **one** CRIT `router_exhausted` message in Telegram and **one** email to
`ADMIN_ALERT_EMAIL`. The three pings and later passes add no new message.

**Cleanup:**

```bash
# 1. stop drill mode
sudo rm -f /etc/systemd/system/hermes-llm-router.service.d/drill.conf
sudo systemctl daemon-reload
sudo rm -f /var/lib/hermes-llm-router/chaos.json
# 2. clear the bench the drill created (the 429daily fault benches providers until Pacific midnight)
sudo systemctl stop hermes-llm-router
sudo rm -f /var/lib/hermes-llm-router/state.json
sudo systemctl start hermes-llm-router
# 3. confirm drill mode is off and no provider is benched
systemctl cat hermes-llm-router | grep -c ROUTER_ENABLE_CHAOS    # expected: 0
curl -s -H "Authorization: Bearer $ROUTER_API_KEY" "$ROUTER/healthz"   # expected: "cooling_down_s": 0 for every provider
# 4. one real ping, which should return 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$ROUTER/v1/chat/completions" \
  -H "Authorization: Bearer $ROUTER_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"hermes-auto","messages":[{"role":"user","content":"drill cleanup"}]}'
```

Note: `state.json` also holds the daily counters. Delete it only at the end of a drill. Never delete it
on a production router outside a drill.

Wait 2 minutes. **Expected:** exactly **one** RECOVERED message for `router_exhausted` in Telegram
and one email.

### Drill (b): router stopped (expect 1 router_unreachable after two failed passes)

**Precondition:** drill (a) cleanup is finished and the router is healthy.

1. Stop the router:

   ```bash
   sudo systemctl stop hermes-llm-router
   ```

2. Wait about 3 minutes. Hermes falls back to its own direct Gemini path, so keep the drill short.

**Expected:** no message after the first failed pass. After the second failed pass, about 2 minutes
after the stop, one CRIT `router_unreachable` arrives in Telegram and by email.

**Cleanup:**

```bash
sudo systemctl start hermes-llm-router
curl -s -H "Authorization: Bearer $ROUTER_API_KEY" "$ROUTER/healthz" >/dev/null && echo "router up"
```

Wait 2 minutes. **Expected:** one RECOVERED message for `router_unreachable` in Telegram and by email.

### Drill (c): flow stall (expect 1 CRIT flow_stalled, then RECOVERED)

**Precondition:** none beyond the normal setup. The drill uses the made-up GTM ID `GTM-DRL0001`, so no
real client is involved.

1. Create the flow file. It is named after the GTM ID and has the documented fields. `owed_by` is
   `agent` and `deadline` is in the past, so the step is overdue as soon as the file exists:

   ```bash
   cat > /home/deploy/.hermes/sentinel/flow/GTM-DRL0001.json <<'EOF'
   {
     "container": "GTM-DRL0001",
     "step": "drill-step-1",
     "outcome": "pending",
     "owed_by": "agent",
     "deadline": "2026-10-10T12:00:00Z",
     "updated_at": "2026-10-10T11:50:00Z",
     "seq": 1
   }
   EOF
   chmod 600 /home/deploy/.hermes/sentinel/flow/GTM-DRL0001.json
   ```

   `deadline` is written only when `owed_by` is `agent`. The date is an example in the past. Use any
   past UTC time.

2. Wait 2 minutes.

**Expected:** exactly **one** CRIT `flow_stalled` message in Telegram and by email. Its incident key is
`flow:GTM-DRL0001:1`.

3. Show the agent has moved on. Write the file again with `seq` set to 2, `owed_by` set to `none`,
   and no `deadline` (a deadline is only written for `agent`):

   ```bash
   cat > /home/deploy/.hermes/sentinel/flow/GTM-DRL0001.json <<'EOF'
   {
     "container": "GTM-DRL0001",
     "step": "drill-step-2",
     "outcome": "done",
     "owed_by": "none",
     "updated_at": "2026-10-10T12:30:00Z",
     "seq": 2
   }
   EOF
   ```

4. Wait 2 minutes.

**Expected:** one RECOVERED message for `flow_stalled` in Telegram and by email, and no new CRIT.

**Cleanup:**

```bash
rm -f /home/deploy/.hermes/sentinel/flow/GTM-DRL0001.json
```

### Drill (d): Telegram blocked, email instead

This drill needs a trigger, so it stops the router, and it needs SMTP to be configured.

**Precondition:** SMTP works (2.3), `ADMIN_ALERT_EMAIL` is set, and the router is running.
**Write the real `ADMIN_TELEGRAM_CHAT_ID` down in your password manager first.**

1. Change the chat id to a wrong value, `0`. Open the file with `nano`, change the line, and save:

   ```bash
   nano /home/deploy/.hermes/.env
   ```

   The line becomes `ADMIN_TELEGRAM_CHAT_ID=0`.

2. Stop the router:

   ```bash
   sudo systemctl stop hermes-llm-router
   ```

3. Wait about 3 minutes, long enough for the second failed pass.

**Expected:** no Telegram message. The journal shows the Telegram failure, one retry, and the email
fallback: `journalctl -u hermes-sentinel -n 50 --no-pager`. The `router_unreachable` CRIT arrives by
**email** at `ADMIN_ALERT_EMAIL`. The pass itself succeeds. If no email arrives, see section 6.

**Cleanup:**

1. Put the real chat id back in `/home/deploy/.hermes/.env` with `nano`. Then check that the wrong value is gone.
   The check prints a count, not the value:

   ```bash
   grep -c '^ADMIN_TELEGRAM_CHAT_ID=0$' /home/deploy/.hermes/.env    # expected: 0
   ```

2. Start the router:

   ```bash
   sudo systemctl start hermes-llm-router
   ```

3. Wait 2 minutes. **Expected:** the RECOVERED message arrives by email and in Telegram again.

---

## 6. Troubleshooting

### No alerts at all

1. Is the timer running? Look at the `NEXT` column:

   ```bash
   systemctl list-timers hermes-sentinel.timer --no-pager
   ```

   If the row is missing or `NEXT` says `n/a`, enable it: `sudo systemctl enable --now hermes-sentinel.timer`.

2. Did the last passes fail? Look at the journal:

   ```bash
   journalctl -u hermes-sentinel -n 50 --no-pager
   ```

3. Is the variable name set? Run `cut -d= -f1 /home/deploy/.hermes/.env` and check the names
   against section 2.1.

4. Is there a silence? Run `cat /home/deploy/.hermes/sentinel/silence.json`.

5. Is there a real problem? If the status check in 2.7 is healthy and no drill is running, silence
   is the correct result. If you still doubt it, check the heartbeat dashboard. Its last ping should be
   within the last few minutes.

### Duplicate alerts

1. Check that only one timer runs in the whole fleet. Run this on each VPS:

   ```bash
   systemctl list-timers --all --no-pager | grep -i sentinel
   ```

   A second copy anywhere (an old unit file, or a second server) sends its own alerts. Disable it there.

2. Do not run `python -m sentinel` by hand outside systemd. Only start a pass with
   `sudo systemctl start hermes-sentinel.service`, so systemd never runs two passes at once.

3. A CRIT that repeats every 6 hours is by design while it stays open. Check whether the timestamps are 6 hours apart.

4. A warning that flips on and off (flapping) means a provider is at the edge of its limit. Fix the
   cause, or silence the kind for a set time (section 4). Silencing does not fix anything.

### Journal permission denied

The journal line says something like `No journal files were opened due to insufficient permissions`
or `Failed to open journal`.

1. Check the group names and the user:

   ```bash
   getent group systemd-journal
   id deploy
   ```

2. If `systemd-journal` does not exist on this distro, use `adm` instead. Edit the unit
   (`sudo nano /etc/systemd/system/hermes-sentinel.service`), change the `SupplementaryGroups=` line
   to `SupplementaryGroups=adm`, then run `sudo systemctl daemon-reload`.

3. Start one pass through systemd and read its log:
   `sudo systemctl start hermes-sentinel.service && journalctl -u hermes-sentinel -n 20 --no-pager`.

### Telegram alerts not arriving

1. Check the journal for the retry and the email fallback lines:
   `journalctl -u hermes-sentinel -n 100 --no-pager`. A failed channel is logged, and does not show as `sentinel_error`.
2. Check `ADMIN_TELEGRAM_CHAT_ID` against section 2.2. Check `TELEGRAM_BOT_TOKEN` is set (`cut -d= -f1`).
3. A CRIT should still reach you by email if Telegram fails. If neither arrives, check SMTP below.

### SMTP not configured

Telegram still works, but no email arrives. Check:

1. Each `MOTAHAI_SMTP_*` name and `ADMIN_ALERT_EMAIL` is set (section 2.1, `cut -d= -f1`).
2. The server answers (section 2.3).
3. The password is the mail account's app-specific password, not the normal login password.
4. The journal does not show an authentication error: `journalctl -u hermes-sentinel -n 100 --no-pager`.

### sentinel_error

The message says what failed. The journal has the details. Typical causes: the state directory is
missing or not writable (check section 2.5 and the `ReadWritePaths=` line), the journal cannot
be read (see above), or `contacts.json` or `silence.json` is not valid JSON (run the `json.tool`
checks in sections 2.4 and 4).

---

## 7. Privacy: what is and is never in an alert

Alerts go to Telegram and sometimes to email. The contract below is what the sentinel must follow.
If you ever see something that breaks it, tell the developer at once.

**An alert can contain:**

- the incident kind, its severity and the time it started
- the provider name (for example `nvidia`) and counts such as `requests_today` against the budget
- for a stalled flow, the GTM container ID and the seq (for example `flow:GTM-DRL0001:1`)
- the end time of a silence, when one applies
- the line `client not notified: no contact on file`, when a container has no entry in `contacts.json`

**An alert never contains:**

- API keys, bot tokens, SMTP passwords, the heartbeat URL, or any value from `.env`
- the text of a prompt, a reply, or any client message
- a client's email address or store name in a Telegram message
- IP addresses, SSH ports or server hostnames
- the full journal lines of the router, unless you read them yourself on the VPS

**Client emails** are sent only to the `email` in an entry of `contacts.json`, for the matching GTM
container. Nothing else sends mail to clients. Keep the state directory at `0700` and the files in it at `0600`.

**Do not share screenshots or pasted output** of `getUpdates`, the contents of `.env`, or journal output
without first checking it for tokens, passwords, chat ids and addresses.

---

## 8. Updating the code

Ask the developer for the new package. Copy it over `SENTINEL_HOME/sentinel/`, then run
`sudo systemctl daemon-reload` if a unit file changed. The timer picks up the new code on its next pass.
Never edit code on the server.
