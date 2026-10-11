"""Tests for the sentinel detector. Run: python -m pytest ops/hermes/sentinel -q

No subprocess, no network: the journal, /healthz, dispatch and the heartbeat are injected fakes, the clock is
injected. Files go to pytest's tmp_path.
"""

import json
import logging
import os
import runpy
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))  # ops/hermes, so the package imports as `sentinel`
from sentinel import detect as D  # noqa: E402

T0 = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)
GTM = "GTM-1234567"
CONTACTS = {GTM: {"email": "client-owner@shop.example.test", "store_name": "Store X", "lang": "ar"}}


# ---------------------------------------------------------------------------
# fakes and harness
# ---------------------------------------------------------------------------

def jl(served_by, *attempts):
    return json.dumps({"ts": "x", "served_by": served_by, "latency_ms": 5, "messages": 1, "tools": 0,
                       "attempts": list(attempts)})


def prov(name, cooldown_kind="", model_gone=False, budget=0, used=0):
    return {"name": name, "model": "m", "cooling_down_s": 60 if cooldown_kind else 0, "cooldown_kind": cooldown_kind,
            "model_gone": model_gone, "daily_request_budget": budget, "requests_today": used, "key_present": True}


def healthy(*providers, degraded=()):
    return (200, {"providers": list(providers) or [prov("nvidia"), prov("gemini")], "degraded": list(degraded)})


class Journal:
    def __init__(self):
        self.log = []
        self.cursors = []
        self.fail = False

    def add(self, *lines):
        self.log.extend(lines)

    def __call__(self, cursor):
        self.cursors.append(cursor)
        if self.fail:
            raise D.JournalError("boom")
        start = int(cursor.split("=")[1]) if cursor else 0
        return list(self.log[start:]), f"s={len(self.log)}"


class Dispatch:
    def __init__(self):
        self.calls = []
        self.raises = False
        self.client_status = []  # queue of statuses for the next client_email rows; default "sent"
        self.contacts_seen = []

    def __call__(self, notifications, contacts, now):
        if self.raises:
            raise RuntimeError("dispatch down")
        self.calls.append([(n["incident"]["incident_key"], n["event"]) for n in notifications])
        self.contacts_seen.append(contacts)
        rows = []
        for n in notifications:
            inc, ev = n["incident"], n["event"]
            key = inc["incident_key"]
            if ev in ("open", "reminder", "recovered"):
                rows.append({"incident_key": key, "channel": "telegram", "status": "sent"})
                if inc["severity"] == "CRIT":
                    rows.append({"incident_key": key, "channel": "admin_email", "status": "sent"})
            if inc["kind"] == "flow_stalled" and inc["severity"] == "CRIT":
                holding = ev in ("open", "client_holding")
                followup = ev in ("recovered", "client_followup") and inc.get("client_holding_sent") is True
                if holding or followup:
                    status = self.client_status.pop(0) if self.client_status else "sent"
                    rows.append({"incident_key": key, "channel": "client_email", "status": status})
        return rows

    def events(self, key):
        return [ev for call in self.calls for k, ev in call if k == key]

    def all_events(self):
        return [pair for call in self.calls for pair in call]


class Env:
    def __init__(self, tmp_path, monkeypatch=None):
        self.dir = tmp_path / "sentinel"
        self.now = T0
        self.journal = Journal()
        self.dispatch = Dispatch()
        self.healthz = healthy()
        self.heartbeats = []
        self.heartbeat_raises = False
        self.healthz_calls = 0
        self.cfg = D.Config(sentinel_dir=self.dir, router_url="http://router.invalid", router_key="k", heartbeat_url="")
        self.deps = D.Deps(now=lambda: self.now, read_journal=self.journal, fetch_healthz=self._healthz,
                           dispatch=self.dispatch, heartbeat=self._heartbeat)

    def _healthz(self):
        self.healthz_calls += 1
        if isinstance(self.healthz, Exception):
            raise self.healthz
        return self.healthz

    def _heartbeat(self, url):
        self.heartbeats.append(url)
        if self.heartbeat_raises:
            raise RuntimeError("hc down")

    def run(self, advance=timedelta(minutes=1), dry_run=False):
        result = D.run_pass(self.cfg, self.deps, dry_run=dry_run)
        self.now += advance
        return result

    def incidents(self):
        return json.loads((self.dir / "incidents.json").read_text(encoding="utf-8"))

    def state(self):
        return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))

    def write_flow(self, container=GTM, seq=1, owed_by="agent", deadline="2026-10-11T09:00:00Z", step="verify_access"):
        flow = self.dir / "flow"
        flow.mkdir(parents=True, exist_ok=True)
        record = {"container": container, "step": step, "outcome": "CONNECTED", "owed_by": owed_by,
                  "updated_at": "2026-10-11T08:55:00Z", "seq": seq}
        if owed_by == "agent":
            record["deadline"] = deadline
        (flow / f"{container}.json").write_text(json.dumps(record), encoding="utf-8")

    def write(self, name, payload):
        self.dir.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.dir / name).write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_logging():
    """main() configures logging globally (root handler, httpx level); do not leak that into other tests."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in levels.items():
        logging.getLogger(name).setLevel(lvl)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def router_kind(env_, healthz=None):
    """Runs one pass and returns the exhaustion_kind of router:exhausted."""
    if healthz is not None:
        env_.healthz = healthz
    env_.run()
    return env_.incidents()["router:exhausted"]["exhaustion_kind"]


# ---------------------------------------------------------------------------
# 1. exhaustion_kind derivation
# ---------------------------------------------------------------------------

def test_kind_rate_limited(env):
    env.journal.add(jl(None, "nvidia:rate_limited", "gemini:rate_limited"))
    assert router_kind(env) == "rate_limited"


def test_kind_rate_limited_from_cooldown_skips_without_healthz_detail(env):
    env.journal.add(jl(None, "nvidia:skipped:cooldown", "gemini:skipped:cooldown"))
    assert router_kind(env) == "rate_limited"


def test_kind_daily_quota_from_attempt_outcome(env):
    env.journal.add(jl(None, "nvidia:rate_limited:daily", "gemini:rate_limited:daily"))
    assert router_kind(env) == "daily_quota"


@pytest.mark.parametrize("skip", ["skipped:daily_requests", "skipped:daily_budget"])
def test_kind_daily_quota_from_daily_skips(env, skip):
    env.journal.add(jl(None, f"nvidia:{skip}", f"gemini:{skip}"))
    assert router_kind(env) == "daily_quota"


def test_kind_daily_quota_from_healthz_cooldown_kind(env):
    env.journal.add(jl(None, "nvidia:skipped:cooldown", "gemini:skipped:cooldown"))
    hz = healthy(prov("nvidia", "daily_quota"), prov("gemini", "daily_quota"))
    assert router_kind(env, hz) == "daily_quota"


def test_kind_rate_limited_attempt_refined_by_healthz_daily_quota(env):
    env.journal.add(jl(None, "nvidia:rate_limited", "gemini:rate_limited"))
    hz = healthy(prov("nvidia", "daily_quota"), prov("gemini", "daily_quota"))
    assert router_kind(env, hz) == "daily_quota"


def test_kind_payment(env):
    env.journal.add(jl(None, "nvidia:payment", "gemini:payment"))
    assert router_kind(env) == "payment"


def test_kind_auth(env):
    env.journal.add(jl(None, "nvidia:auth", "gemini:auth"))
    assert router_kind(env) == "auth"


def test_kind_model_gone_from_healthz_flag(env):
    env.journal.add(jl(None, "nvidia:skipped:cooldown", "gemini:skipped:cooldown"))
    hz = healthy(prov("nvidia", model_gone=True), prov("gemini", "model_gone"))
    assert router_kind(env, hz) == "model_gone"


def test_kind_model_gone_from_attempt_outcome(env):
    env.journal.add(jl(None, "nvidia:model_gone", "gemini:410"))
    assert router_kind(env) == "model_gone"


def test_kind_mixed_when_providers_disagree(env):
    env.journal.add(jl(None, "nvidia:rate_limited", "gemini:auth"))
    assert router_kind(env) == "mixed"


def test_kind_mixed_on_timeouts_and_missing_attempts(env, tmp_path):
    env.journal.add(jl(None, "nvidia:timeout", "gemini:network"))
    assert router_kind(env) == "mixed"
    other = Env(tmp_path / "b")
    other.journal.add(json.dumps({"served_by": None}))
    assert router_kind(other) == "mixed"


def test_neutral_skips_do_not_decide_the_kind(env):
    env.journal.add(jl(None, "nvidia:skipped:no_key", "openrouter:skipped:no_tools", "gemini:rate_limited:daily"))
    assert router_kind(env) == "daily_quota"


def test_router_incident_shape(env):
    env.journal.add(jl(None, "nvidia:rate_limited:daily", "gemini:rate_limited:daily"), jl(None, "gemini:payment"))
    env.run()
    inc = env.incidents()["router:exhausted"]
    assert inc["severity"] == "CRIT" and inc["state"] == "open" and inc["kind"] == "router_exhausted"
    assert inc["scope"] == "router" and inc["http_status"] == 503 and inc["count"] == 2
    assert inc["providers"] == ["nvidia", "gemini"]
    assert inc["container_id"] is None and inc["last_successful_step"] is None
    assert inc["opened_at"] == "2026-10-11T10:00:00Z"


def test_non_json_and_foreign_json_lines_are_ignored(env):
    env.journal.add("-- No entries --", "not json {", json.dumps({"hello": "world"}), json.dumps([1, 2]), jl("nvidia"))
    env.run()
    assert not (env.dir / "incidents.json").read_text(encoding="utf-8").count("router:exhausted")
    assert env.dispatch.calls == []


# ---------------------------------------------------------------------------
# 2. drills: 429daily on all tiers, payment, cooling providers
# ---------------------------------------------------------------------------

def test_429daily_all_tiers_one_crit_one_recovered_over_ten_passes(env):
    daily = jl(None, "nvidia:rate_limited:daily", "gemini:rate_limited:daily")
    env.healthz = healthy(prov("nvidia", "daily_quota"), prov("gemini", "daily_quota"))
    for _ in range(3):
        env.journal.add(daily)
        env.run()
    for _ in range(7):  # service returns
        env.journal.add(jl("gemini"))
        env.run()
    assert env.dispatch.events("router:exhausted") == ["open", "recovered"]
    assert env.incidents()["router:exhausted"]["exhaustion_kind"] == "daily_quota"
    assert env.incidents()["router:exhausted"]["state"] == "resolved"


def test_resolve_needs_two_served_passes_and_resets_on_new_failure(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    env.journal.add(jl("nvidia"))
    env.run()
    assert env.incidents()["router:exhausted"]["state"] == "open"
    env.journal.add(jl(None, "nvidia:rate_limited"))  # relapse resets the clean-pass counter
    env.run()
    env.journal.add(jl("nvidia"))
    env.run()
    assert env.incidents()["router:exhausted"]["state"] == "open"
    env.journal.add(jl("nvidia"))
    env.run()
    assert env.incidents()["router:exhausted"]["state"] == "resolved"
    assert env.dispatch.events("router:exhausted") == ["open", "recovered"]


def test_idle_pass_neither_resolves_nor_resets(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    env.journal.add(jl("nvidia"))
    env.run()
    env.run()  # idle
    assert env.incidents()["router:exhausted"]["state"] == "open"
    env.journal.add(jl("nvidia"))
    env.run()
    assert env.incidents()["router:exhausted"]["state"] == "resolved"


def test_payment_402_degrades_provider_with_payment(env):
    env.healthz = healthy(prov("nvidia", "payment"), prov("gemini"), degraded=["nvidia"])
    env.journal.add(jl("gemini", "nvidia:payment"))
    env.run()
    inc = env.incidents()["provider:nvidia:degraded"]
    assert inc["kind"] == "provider_degraded" and inc["severity"] == "CRIT" and inc["scope"] == "provider"
    assert inc["exhaustion_kind"] == "payment" and inc["providers"] == ["nvidia"]
    assert "router:exhausted" not in env.incidents()
    assert env.dispatch.all_events() == [("provider:nvidia:degraded", "open")]


def test_provider_degraded_model_gone_and_resolves_after_two_clean_passes(env):
    env.healthz = healthy(prov("nvidia", model_gone=True), prov("gemini"), degraded=["nvidia"])
    env.run()
    assert env.incidents()["provider:nvidia:degraded"]["exhaustion_kind"] == "model_gone"
    env.healthz = healthy()
    env.run()
    assert env.incidents()["provider:nvidia:degraded"]["state"] == "open"
    env.run()
    assert env.incidents()["provider:nvidia:degraded"]["state"] == "resolved"
    assert env.dispatch.events("provider:nvidia:degraded") == ["open", "recovered"]


def test_cooling_provider_while_others_serve_does_not_raise_router_exhausted(env):
    env.healthz = healthy(prov("gemini-flash", "daily_quota"), prov("gemini-flash-lite"), prov("nvidia"))
    for _ in range(3):
        env.journal.add(jl("nvidia", "gemini-flash:skipped:cooldown"))
        env.run()
    assert "router:exhausted" not in json.loads((env.dir / "incidents.json").read_text(encoding="utf-8"))
    assert env.dispatch.calls == []


# ---------------------------------------------------------------------------
# 3. router unreachable
# ---------------------------------------------------------------------------

def test_unreachable_needs_two_passes_then_recovers_immediately(env):
    env.healthz = ConnectionError("refused")
    env.run()
    assert env.dispatch.calls == []
    assert "router:unreachable" not in env.incidents()
    env.run()
    assert env.incidents()["router:unreachable"]["severity"] == "CRIT"
    env.run()  # still down: no duplicate
    env.healthz = healthy()
    env.run()
    assert env.dispatch.events("router:unreachable") == ["open", "recovered"]
    assert env.incidents()["router:unreachable"]["state"] == "resolved"


def test_non_200_and_bad_body_count_as_unreachable_and_one_good_pass_resets(env):
    env.healthz = (503, {"error": "x"})
    env.run()
    env.healthz = healthy()
    env.run()
    env.healthz = (200, "not a dict")
    env.run()
    assert "router:unreachable" not in env.incidents()  # the counter was reset in between
    env.run()
    assert env.incidents()["router:unreachable"]["state"] == "open"


# ---------------------------------------------------------------------------
# 4. budget80
# ---------------------------------------------------------------------------

def test_budget80_once_per_provider_per_day_and_no_recovered(env):
    env.now = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)  # 13:00 Pacific, Oct 11
    env.healthz = healthy(prov("gemini-flash-lite", budget=18, used=18), prov("nvidia", budget=100, used=50))
    for _ in range(5):
        env.run()
    assert env.dispatch.all_events() == [("provider:gemini-flash-lite:budget80", "open")]
    inc = env.incidents()["provider:gemini-flash-lite:budget80"]
    assert inc["severity"] == "WARN" and inc["kind"] == "budget_80" and inc["providers"] == ["gemini-flash-lite"]
    assert env.state()["budget_day"]["gemini-flash-lite"] == "2026-10-11"

    # day roll with the counter reset: dropped silently, no RECOVERED
    env.now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)  # 01:00 Pacific, Oct 12
    env.healthz = healthy(prov("gemini-flash-lite", budget=18, used=0))
    env.run()
    assert "provider:gemini-flash-lite:budget80" not in env.incidents()
    assert env.dispatch.all_events() == [("provider:gemini-flash-lite:budget80", "open")]


def test_budget80_alerts_again_on_a_new_day_if_still_high(env):
    env.now = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)
    env.healthz = healthy(prov("gemini-flash-lite", budget=18, used=15))
    env.run()
    env.now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    env.run()
    env.run()
    assert env.dispatch.events("provider:gemini-flash-lite:budget80") == ["open", "open"]


def test_budget_below_threshold_or_no_budget_is_quiet(env):
    env.healthz = healthy(prov("a", budget=10, used=7), prov("b", budget=0, used=999))
    env.run()
    assert env.dispatch.calls == []


# ---------------------------------------------------------------------------
# 5. flow stalls
# ---------------------------------------------------------------------------

def test_flow_stall_one_crit_then_recovered_on_seq_change(env):
    env.write_flow(seq=7)
    for _ in range(4):
        env.run()
    key = f"flow:{GTM}:7"
    assert env.dispatch.events(key) == ["open"]
    inc = env.incidents()[key]
    assert inc["kind"] == "flow_stalled" and inc["severity"] == "CRIT" and inc["scope"] == "container"
    assert inc["container_id"] == GTM and inc["last_successful_step"] == "verify_access"
    env.write_flow(seq=8, owed_by="none", step="cloud_publish")
    for _ in range(3):
        env.run()
    assert env.dispatch.events(key) == ["open", "recovered"]
    assert env.incidents()[key]["state"] == "resolved"


def test_flow_resolves_when_file_disappears(env):
    env.write_flow(seq=2)
    env.run()
    (env.dir / "flow" / f"{GTM}.json").unlink()
    env.run()
    assert env.dispatch.events(f"flow:{GTM}:2") == ["open", "recovered"]


def test_flow_new_seq_still_stalled_opens_a_new_incident(env):
    env.write_flow(seq=1)
    env.run()
    env.write_flow(seq=2)
    env.run()
    assert env.dispatch.all_events() == [(f"flow:{GTM}:1", "open"), (f"flow:{GTM}:1", "recovered"), (f"flow:{GTM}:2", "open")]


@pytest.mark.parametrize("owed", ["human", "client", "operator", "none"])
def test_no_alert_for_non_agent_owners(env, owed):
    env.write_flow(owed_by=owed)
    for _ in range(3):
        env.run()
    assert env.dispatch.calls == []


def test_no_alert_before_deadline(env):
    env.write_flow(deadline="2026-10-11T10:05:00Z")
    env.run()
    assert env.dispatch.calls == []
    env.now = T0 + timedelta(minutes=6)
    env.run()
    assert env.dispatch.events(f"flow:{GTM}:1") == ["open"]


def test_corrupt_flow_file_is_a_sentinel_error_for_that_file_and_others_still_work(env):
    env.write_flow(seq=3)
    (env.dir / "flow" / "GTM-BAD0001.json").write_text("{not json", encoding="utf-8")
    env.run()
    keys = {k for k, _ in env.dispatch.all_events()}
    assert keys == {f"flow:{GTM}:3", "sentinel:error:flow:GTM-BAD0001"}
    assert env.incidents()["sentinel:error:flow:GTM-BAD0001"]["severity"] == "WARN"
    assert env.incidents()["sentinel:error:flow:GTM-BAD0001"]["kind"] == "sentinel_error"
    env.run()
    assert len(env.dispatch.all_events()) == 2  # no repeat


def test_flow_agent_record_without_deadline_is_an_error_not_a_stall(env):
    (env.dir / "flow").mkdir(parents=True)
    (env.dir / "flow" / f"{GTM}.json").write_text(
        json.dumps({"container": GTM, "step": "s", "owed_by": "agent", "seq": 1}), encoding="utf-8")
    env.run()
    assert [k for k, _ in env.dispatch.all_events()] == [f"sentinel:error:flow:{GTM}"]


def test_stall_stays_open_while_its_file_is_temporarily_unreadable(env):
    env.write_flow(seq=5)
    env.run()
    (env.dir / "flow" / f"{GTM}.json").write_text("{partial", encoding="utf-8")
    env.run()
    assert env.incidents()[f"flow:{GTM}:5"]["state"] == "open"


def test_admin_incident_carries_no_client_details(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.run()
    blob = (env.dir / "incidents.json").read_text(encoding="utf-8")
    assert "client-owner" not in blob and "Store X" not in blob


# ---------------------------------------------------------------------------
# 6. reminders
# ---------------------------------------------------------------------------

def test_reminder_every_six_hours_for_open_crit_only(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.healthz = healthy(prov("gemini-flash-lite", budget=10, used=9))
    env.run(advance=timedelta(hours=5, minutes=59))      # pass at t0
    assert env.dispatch.all_events() == [("provider:gemini-flash-lite:budget80", "open"), ("router:exhausted", "open")]
    env.run(advance=timedelta(minutes=1))                # pass at t0+5h59: nothing yet
    assert len(env.dispatch.calls) == 1
    env.run(advance=timedelta(hours=5))                  # pass at t0+6h: reminder (budget WARN never reminds)
    assert env.dispatch.calls[-1] == [("router:exhausted", "reminder")]
    env.run(advance=timedelta(hours=1))                  # pass at t0+11h: quiet
    assert len(env.dispatch.calls) == 2
    env.run()                                            # pass at t0+12h: 6h since the reminder
    assert env.dispatch.calls[-1] == [("router:exhausted", "reminder")]
    assert len(env.dispatch.calls) == 3


# ---------------------------------------------------------------------------
# 7. silences
# ---------------------------------------------------------------------------

def _until(hours):
    return (T0 + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("pattern", ["router:exhausted", "router:", "router_exhausted"])
def test_silence_exact_prefix_and_kind_suppress_sends_but_not_detection(env, pattern):
    env.write("silence.json", {pattern: _until(2)})
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    env.run()
    assert env.dispatch.calls == []
    assert env.incidents()["router:exhausted"]["state"] == "open"  # detection continued
    env.now = T0 + timedelta(hours=3)                              # silence expired, still broken
    env.run()
    assert env.dispatch.events("router:exhausted") == ["open"]


def test_silence_prefix_covers_flow_incidents_of_one_container_only(env):
    other = "GTM-7777777"
    env.write("silence.json", {f"flow:{GTM}": _until(1)})
    env.write_flow(seq=4)
    env.write_flow(container=other, seq=9)
    env.run()
    assert env.dispatch.all_events() == [(f"flow:{other}:9", "open")]


def test_expired_silence_is_ignored(env):
    env.write("silence.json", {"router:exhausted": _until(-1), "other": "garbage"})
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    assert env.dispatch.events("router:exhausted") == ["open"]


def test_recovery_inside_silence_is_not_sent(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    env.write("silence.json", {"router:exhausted": _until(5)})
    for _ in range(2):
        env.journal.add(jl("nvidia"))
        env.run()
    assert env.dispatch.events("router:exhausted") == ["open"]
    assert env.incidents()["router:exhausted"]["state"] == "resolved"


# ---------------------------------------------------------------------------
# 8. client email retries
# ---------------------------------------------------------------------------

def test_client_holding_is_retried_after_outside_hours_and_stops_once_sent(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.dispatch.client_status = ["skipped:outside_hours", "skipped:outside_hours", "sent"]
    for _ in range(5):
        env.run()
    key = f"flow:{GTM}:1"
    assert env.dispatch.events(key) == ["open", "client_holding", "client_holding"]
    inc = env.incidents()[key]
    assert inc["client_holding_sent"] is True
    assert env.dispatch.contacts_seen[0] == CONTACTS


def test_client_holding_failed_is_throttled_to_ten_minutes(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.dispatch.client_status = ["failed", "sent"]
    env.run()                                  # open -> failed
    for _ in range(9):
        env.run()                              # 9 minutes: throttled
    assert env.dispatch.events(f"flow:{GTM}:1") == ["open"]
    env.run()                                  # 10 minutes after the attempt
    assert env.dispatch.events(f"flow:{GTM}:1") == ["open", "client_holding"]
    assert env.incidents()[f"flow:{GTM}:1"]["client_holding_sent"] is True


def test_no_client_retry_without_contact_or_when_not_configured(env):
    env.write_flow(seq=1)
    env.dispatch.client_status = ["skipped:no_contact"]
    for _ in range(3):
        env.run()
    assert env.dispatch.events(f"flow:{GTM}:1") == ["open"]
    other = "GTM-2222222"
    env.write("contacts.json", {other: CONTACTS[GTM]})
    env.write_flow(container=other, seq=1)
    env.dispatch.client_status = ["skipped:not_configured"]
    for _ in range(3):
        env.run()
    assert env.dispatch.events(f"flow:{other}:1") == ["open"]


def test_followup_sent_on_recovery_when_holding_was_sent(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.run()
    env.write_flow(seq=2, owed_by="none")
    env.run()
    env.run()
    inc = env.incidents()[f"flow:{GTM}:1"]
    assert env.dispatch.events(f"flow:{GTM}:1") == ["open", "recovered"]
    assert inc["client_followup_sent"] is True


def test_no_followup_events_when_holding_never_sent(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.dispatch.client_status = ["skipped:outside_hours"]
    env.run()
    env.write_flow(seq=2, owed_by="none")
    for _ in range(3):
        env.run()
    assert env.dispatch.events(f"flow:{GTM}:1") == ["open", "recovered"]  # no client_holding / client_followup after resolve


def test_followup_retried_after_outside_hours_then_given_up_after_24h(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.run()                                              # holding sent
    env.write_flow(seq=2, owed_by="none")
    env.dispatch.client_status = ["skipped:outside_hours", "skipped:outside_hours"]
    env.run()                                              # recovered; follow-up deferred
    env.run()                                              # retry 1 (deferred again)
    env.run()                                              # retry 2 (sent)
    env.run()
    key = f"flow:{GTM}:1"
    assert env.dispatch.events(key) == ["open", "recovered", "client_followup", "client_followup"]
    assert env.incidents()[key]["client_followup_sent"] is True


def test_followup_gives_up_after_24_hours(env):
    env.write("contacts.json", CONTACTS)
    env.write_flow(seq=1)
    env.run()
    env.write_flow(seq=2, owed_by="none")
    env.dispatch.client_status = ["skipped:outside_hours"] * 50
    env.run()                                              # recovered, deferred
    resolved_at = env.now
    env.now = resolved_at + timedelta(hours=23, minutes=59)
    env.run()
    assert env.dispatch.events(f"flow:{GTM}:1")[-1] == "client_followup"
    count = len(env.dispatch.calls)
    env.now = resolved_at + timedelta(hours=24, minutes=2)
    env.run()
    env.run()
    assert len(env.dispatch.calls) == count
    assert env.incidents()[f"flow:{GTM}:1"]["followup_given_up"] is True


def test_resolved_incidents_are_pruned_after_seven_days(env):
    env.write_flow(seq=1)
    env.run()
    env.write_flow(seq=2, owed_by="none")
    env.run()
    assert env.incidents()[f"flow:{GTM}:1"]["state"] == "resolved"
    env.now += timedelta(days=6, hours=23)
    env.run()
    assert f"flow:{GTM}:1" in env.incidents()
    env.now += timedelta(hours=2)
    env.run()
    assert f"flow:{GTM}:1" not in env.incidents()


# ---------------------------------------------------------------------------
# 9. persistence after dispatch
# ---------------------------------------------------------------------------

def test_dispatch_raising_does_not_advance_state_and_next_pass_resends(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.dispatch.raises = True
    result = env.run()
    assert result.persisted is False and result.exit_code == 0
    assert not (env.dir / "incidents.json").exists() and not (env.dir / "state.json").exists()
    env.dispatch.raises = False
    env.run()
    assert env.journal.cursors == [None, None]       # the cursor did not advance, the line was re-read
    assert env.dispatch.events("router:exhausted") == ["open"]
    assert env.state()["cursor"] == "s=1"


def test_open_is_not_marked_notified_when_dispatch_fails_midway_through_later_pass(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    env.dispatch.raises = True
    env.now += timedelta(hours=7)
    env.run()
    assert env.incidents()["router:exhausted"]["last_notified_at"] == "2026-10-11T10:00:00Z"
    env.dispatch.raises = False
    env.run()
    assert env.dispatch.events("router:exhausted") == ["open", "reminder"]


def test_files_are_atomic_and_private(env):
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.run()
    assert sorted(p.name for p in env.dir.iterdir()) == ["incidents.json", "state.json"]
    if os.name != "nt":
        assert (env.dir.stat().st_mode & 0o777) == 0o700
        for name in ("incidents.json", "state.json"):
            assert ((env.dir / name).stat().st_mode & 0o777) == 0o600


def test_state_dir_unwritable_exits_one(tmp_path, env):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    env.cfg = D.Config(sentinel_dir=blocker / "sub", router_url="http://x", router_key="k", heartbeat_url="")
    result = D.run_pass(env.cfg, env.deps)
    assert result.exit_code == 1
    assert D.main(["--once"], env.deps, {"HERMES_SENTINEL_DIR": str(blocker / "sub"), "ROUTER_API_KEY": "k"}) == 1


# ---------------------------------------------------------------------------
# 10. sentinel's own failures
# ---------------------------------------------------------------------------

def test_corrupt_state_raises_sentinel_error_and_recovers(env):
    env.write("state.json", "{{{{ garbage")
    env.run()
    assert env.dispatch.all_events() == [("sentinel:error", "open")]
    assert (env.dir / "state.json.corrupt").exists()
    assert env.state()["cursor"] is None or isinstance(env.state()["cursor"], str)
    env.run()
    assert env.incidents()["sentinel:error"]["state"] == "open"
    env.run()
    assert env.incidents()["sentinel:error"]["state"] == "resolved"
    assert env.dispatch.events("sentinel:error") == ["open", "recovered"]


def test_corrupt_incidents_file_is_replaced(env):
    env.write("incidents.json", '{"router:exhausted": "nope"}')
    env.run()
    assert env.dispatch.events("sentinel:error") == ["open"]
    assert (env.dir / "incidents.json.corrupt").exists()


def test_wrongly_typed_state_counts_as_corrupt(env):
    env.write("state.json", {"cursor": 12, "healthz_fail": "x"})
    env.run()
    assert env.dispatch.events("sentinel:error") == ["open"]


def test_unreadable_journal_is_a_sentinel_error_and_detection_continues(env):
    env.journal.fail = True
    env.healthz = healthy(prov("nvidia", "payment"), degraded=["nvidia"])
    result = env.run()
    assert result.exit_code == 0 and result.persisted
    keys = {k for k, _ in env.dispatch.all_events()}
    assert keys == {"sentinel:error", "provider:nvidia:degraded"}
    assert env.incidents()["sentinel:error"]["severity"] == "WARN"
    env.journal.fail = False
    env.run()
    env.run()
    assert env.dispatch.events("sentinel:error") == ["open", "recovered"]


def test_missing_router_key_is_a_sentinel_error_and_never_unreachable(env):
    env.cfg = D.Config(sentinel_dir=env.dir, router_url="http://x", router_key="", heartbeat_url="")
    for _ in range(3):
        env.run()
    assert env.healthz_calls == 0
    assert env.dispatch.all_events() == [("sentinel:error", "open")]
    assert "router:unreachable" not in env.incidents()


def test_corrupt_contacts_and_silence_files_are_sentinel_errors(env):
    env.write("contacts.json", "[1,2]")
    env.write("silence.json", "oops")
    env.run()
    assert env.dispatch.events("sentinel:error") == ["open"]


def test_unexpected_bug_never_crashes_main(tmp_path, env, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bug")

    monkeypatch.setattr(D, "run_pass", boom)
    assert D.main(["--once"], env.deps, {"HERMES_SENTINEL_DIR": str(env.dir)}) == 0


# ---------------------------------------------------------------------------
# 11. configuration
# ---------------------------------------------------------------------------

def test_router_key_prefers_api_key_and_falls_back_to_hermes_router_key(tmp_path):
    both = D.Config.from_env({"ROUTER_API_KEY": "a", "HERMES_ROUTER_KEY": "b"})
    assert both.router_key == "a"
    fallback = D.Config.from_env({"HERMES_ROUTER_KEY": " b "})
    assert fallback.router_key == "b"
    assert D.Config.from_env({}).router_key == ""


def test_router_url_default_ignores_hermes_router_url():
    cfg = D.Config.from_env({"HERMES_ROUTER_URL": "http://elsewhere:9/v1"})
    assert cfg.router_url == "http://127.0.0.1:47311"
    assert D.Config.from_env({"ROUTER_URL": "http://r:1/"}).router_url == "http://r:1"
    assert D.Config.from_env({}).sentinel_dir == Path("/home/deploy/.hermes/sentinel")


def test_fallback_key_is_what_healthz_sends(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"providers": []}

    import httpx

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    cfg = D.Config.from_env({"HERMES_ROUTER_KEY": "fallback-key"})
    assert D.make_fetch_healthz(cfg)() == (200, {"providers": []})
    assert seen == {"url": "http://127.0.0.1:47311/healthz", "headers": {"Authorization": "Bearer fallback-key"},
                    "timeout": 10.0}


# ---------------------------------------------------------------------------
# 12. no channel configured (real dispatcher, empty environment)
# ---------------------------------------------------------------------------

def test_pass_completes_and_persists_with_no_channel_configured(env, monkeypatch, caplog):
    for name in ("TELEGRAM_BOT_TOKEN", "ADMIN_TELEGRAM_CHAT_ID", "ADMIN_ALERT_EMAIL", "MOTAHAI_SMTP_HOST",
                 "MOTAHAI_SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)
    env.deps.dispatch = D.default_dispatch
    env.journal.add(jl(None, "nvidia:rate_limited"))
    with caplog.at_level(logging.INFO):
        result = env.run()
    assert result.exit_code == 0 and result.persisted
    assert env.incidents()["router:exhausted"]["state"] == "open"
    assert sum("no channel configured" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# 13. dry-run
# ---------------------------------------------------------------------------

def test_dry_run_sends_and_persists_nothing(tmp_path, capsys):
    env = Env(tmp_path)
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.write_flow(seq=2)
    before = sorted(p.name for p in env.dir.rglob("*"))
    cfg_env = {"HERMES_SENTINEL_DIR": str(env.dir), "ROUTER_API_KEY": "k", "SENTINEL_HEARTBEAT_URL": "http://hb.invalid/x"}
    code = D.main(["--once", "--dry-run"], env.deps, cfg_env)
    assert code == 0
    out = capsys.readouterr().out
    assert "router:exhausted CRIT open" in out and f"flow:{GTM}:2 CRIT open" in out
    assert env.dispatch.calls == [] and env.heartbeats == []
    assert sorted(p.name for p in env.dir.rglob("*")) == before
    assert not (env.dir / "incidents.json").exists() and not (env.dir / "state.json").exists()


def test_dry_run_does_not_create_the_state_dir(tmp_path):
    env = Env(tmp_path)
    D.run_pass(env.cfg, env.deps, dry_run=True)
    assert not env.dir.exists()


# ---------------------------------------------------------------------------
# 14. heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_called_only_on_success(env, caplog):
    env.cfg = D.Config(sentinel_dir=env.dir, router_url="http://x", router_key="k", heartbeat_url="http://hb.invalid/SECRET")
    env.run()
    assert env.heartbeats == ["http://hb.invalid/SECRET"]
    env.journal.add(jl(None, "nvidia:rate_limited"))
    env.dispatch.raises = True
    env.run()
    assert len(env.heartbeats) == 1
    env.dispatch.raises = False
    env.heartbeat_raises = True
    with caplog.at_level(logging.INFO):
        result = env.run()                          # heartbeat failure never fails the pass
    assert result.persisted and result.exit_code == 0
    assert len(env.heartbeats) == 2
    assert "SECRET" not in caplog.text and "hb.invalid" not in caplog.text


def test_heartbeat_not_called_when_url_unset_or_state_unwritable(env, tmp_path):
    env.run()
    assert env.heartbeats == []
    blocker = tmp_path / "f"
    blocker.write_text("x", encoding="utf-8")
    cfg = D.Config(sentinel_dir=blocker / "d", router_url="http://x", router_key="k", heartbeat_url="http://hb.invalid")
    D.run_pass(cfg, env.deps)
    assert env.heartbeats == []


# ---------------------------------------------------------------------------
# 15. journal wrapper, logging, entry point
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_read_journal_first_run_uses_since_and_parses_cursor():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _Proc('{"served_by": "a"}\n-- cursor: s=abc;i=1f;b=2\n')

    lines, cursor = D.read_journal(None, run=run)
    assert lines == ['{"served_by": "a"}'] and cursor == "s=abc;i=1f;b=2"
    assert calls[0][:2] == ["journalctl", "-u"] and "hermes-llm-router" in calls[0]
    assert "--since=-2min" in calls[0] and "--show-cursor" in calls[0] and "cat" in calls[0]


def test_read_journal_uses_after_cursor_and_falls_back_when_cursor_is_stale():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if any(a.startswith("--after-cursor") for a in argv):
            return _Proc("", returncode=1)
        return _Proc("-- cursor: s=new\n")

    lines, cursor = D.read_journal("s=old", run=run)
    assert "--after-cursor=s=old" in calls[0] and "--since=-2min" in calls[1]
    assert cursor == "s=new" and lines == []


def test_read_journal_raises_journal_error():
    def missing(argv, **kw):
        raise FileNotFoundError("journalctl")

    with pytest.raises(D.JournalError):
        D.read_journal(None, run=missing)
    with pytest.raises(D.JournalError):
        D.read_journal(None, run=lambda argv, **kw: _Proc("", returncode=1))

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired("journalctl", 1)

    with pytest.raises(D.JournalError):
        D.read_journal(None, run=timeout)


def test_logs_carry_no_tokens_emails_or_flow_content(env, caplog):
    env.write("contacts.json", CONTACTS)
    env.cfg = D.Config(sentinel_dir=env.dir, router_url="http://x", router_key="TOPSECRETKEY",
                       heartbeat_url="http://hb.invalid/SECRETPING")
    env.write_flow(seq=1)
    env.journal.add(jl(None, "nvidia:rate_limited"))
    with caplog.at_level(logging.DEBUG):
        env.run()
    text = caplog.text
    for forbidden in ("TOPSECRETKEY", "SECRETPING", "client-owner", "Store X", "hb.invalid"):
        assert forbidden not in text
    assert "sentinel pass" in text


def test_main_entry_point_module_runs_main(monkeypatch):
    called = []
    monkeypatch.setattr(D, "main", lambda *a, **k: called.append(1) or 0)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("sentinel", run_name="__main__")
    assert exc.value.code == 0 and called == [1]


def test_default_dispatch_passes_now_only_when_supported(monkeypatch):
    from sentinel import dispatch as dispatch_mod

    seen = {}

    def with_now(notifications, contacts, *, now=None):
        seen["now"] = now
        return []

    monkeypatch.setattr(dispatch_mod, "dispatch", with_now)
    D.default_dispatch([], {}, T0)
    assert seen["now"] == T0

    def without_now(notifications, contacts):
        seen["plain"] = True
        return []

    monkeypatch.setattr(dispatch_mod, "dispatch", without_now)
    D.default_dispatch([], {}, T0)
    assert seen["plain"] is True
