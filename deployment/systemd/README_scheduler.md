# Conversion scheduler (motahai-scheduler)

The scheduler dispatches settled `DeliveredPurchase` events to Meta CAPI. Nothing sends them unless this daemon
runs (or the in-app loop is enabled). Production runs it as its own systemd service:

```
python -m ameen_workforce.scheduler          # daemon: one cycle every 15 minutes until SIGTERM/SIGINT
python -m ameen_workforce.scheduler --check  # monitoring: exit 0 if fresh, 1 if no send cycle in 45 minutes
```

Each cycle runs `send_due_events`, `retry_failed_events`, `purge_expired_checkout_context`,
`merge_pending_captures` (skipped and recorded as skipped when the capture module is absent), then a heartbeat.
Every job writes a row to `job_runs`.

## Why 15 minutes

A `DeliveredPurchase` becomes due at delivery + settlement window, capped at placed + 6.5 days - 1 hour (the
safety margin before Meta's cutoff). A 15-minute cycle sends an event at most about 15 minutes after it is due,
which keeps well inside that hour. Running less often would eat into the margin.

## Why not in the web app

`service.py` runs uvicorn with `--workers 2`. Each worker that started an in-app loop would send the same events.
The in-app loop (`MOTAHAI_RUN_SCHEDULER_IN_APP=1`) is off by default and is meant for single-process
development only. The daemon is the production mode.

## Install (one instance only)

```bash
sudo useradd --system --home-dir /var/lib/motahai --shell /usr/sbin/nologin motahai
sudo install -d -o root -g motahai -m 750 /etc/motahai
# /etc/motahai/core.env must hold MOTAHAI_FERNET_KEY and DATABASE_URL, for example:
#   DATABASE_URL=sqlite:////var/lib/motahai/motahai.db
sudo chown root:motahai /etc/motahai/core.env && sudo chmod 640 /etc/motahai/core.env
sudo cp deployment/systemd/motahai-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now motahai-scheduler
journalctl -u motahai-scheduler -f
```

Check the heartbeat (the same env file must be loaded):

```bash
sudo -u motahai bash -c 'set -a; . /etc/motahai/core.env; set +a; \
  PYTHONPATH=/opt/ameen-workforce/src /opt/ameen-workforce/venv/bin/python -m ameen_workforce.scheduler --check'
```

Wire `--check` into the uptime monitor. A non-zero exit means no successful send cycle in 45 minutes (three
missed cycles).

## Notes

- The web gateway and the daemon must share one `DATABASE_URL`. A SQLite file that the root-run web service
  creates under `/opt/ameen-workforce` is not writable by `motahai` under `ProtectSystem=strict`. Move the database
  into `/var/lib/motahai` and point both services at it.
- Stop with `systemctl stop motahai-scheduler`. The running cycle finishes first (`TimeoutStopSec=120s`).
- Keep exactly one scheduler running. A second daemon would race the first on the same due events. Do not
  enable the in-app loop alongside the daemon either.
