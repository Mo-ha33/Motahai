"""Tests for the sentinel alert dispatcher. Run: python -m pytest tests -q

Mocked transports only: httpx.MockTransport for Telegram, a fake SMTP class for email. The autouse
fixture makes any real HTTP or SMTP connection fail the test. Sleeps are injected and recorded.
"""

import copy
import json
import logging
import smtplib
import sys
from pathlib import Path

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).parents[2]))  # ops/hermes, so the package imports as `sentinel`
from sentinel import dispatch as D  # noqa: E402
from sentinel import templates as T  # noqa: E402


ADMIN_EMAIL = "ops-admin@example.test"
CLIENT_EMAIL = "client-owner@shop.example.test"
BOT_TOKEN = "123456:SECRET-TOKEN-XYZ"
SMTP_PASSWORD = "PW-SECRET-987"
CHAT_ID = "987654321"

ENV_FULL = {
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "ADMIN_TELEGRAM_CHAT_ID": CHAT_ID,
    "ADMIN_ALERT_EMAIL": ADMIN_EMAIL,
    "MOTAHAI_SMTP_HOST": "smtp.example.test",
    "MOTAHAI_SMTP_PORT": "587",
    "MOTAHAI_SMTP_USER": "mailer@example.test",
    "MOTAHAI_SMTP_PASSWORD": SMTP_PASSWORD,
    "MOTAHAI_SMTP_FROM": "noreply@example.test",
}
SMTP_VARS = ("MOTAHAI_SMTP_HOST", "MOTAHAI_SMTP_PORT", "MOTAHAI_SMTP_USER", "MOTAHAI_SMTP_PASSWORD", "MOTAHAI_SMTP_FROM")

CONTACTS = {"GTM-1234567": {"email": CLIENT_EMAIL, "store_name": "Store X", "lang": "ar"}}

ROUTER_CRIT = {
    "incident_key": "router:exhausted", "severity": "CRIT", "state": "open", "kind": "router_exhausted",
    "exhaustion_kind": "daily_quota", "scope": "router", "container_id": None, "last_successful_step": None,
    "providers": ["nvidia", "gemini"], "http_status": 503, "opened_at": "2026-10-11T10:00:00Z",
    "timestamp": "2026-10-11T10:00:00Z", "count": 1,
}
BUDGET_WARN = {
    "incident_key": "provider:gemini:budget80", "severity": "WARN", "state": "open", "kind": "budget_80",
    "exhaustion_kind": None, "scope": "provider", "container_id": None, "last_successful_step": None,
    "providers": ["gemini"], "http_status": None, "opened_at": "2026-10-11T10:00:00Z",
    "timestamp": "2026-10-11T10:00:00Z", "count": 1,
}
FLOW_CRIT = {
    "incident_key": "flow:GTM-1234567:7", "severity": "CRIT", "state": "open", "kind": "flow_stalled",
    "exhaustion_kind": None, "scope": "container", "container_id": "GTM-1234567",
    "last_successful_step": "publish", "providers": [], "http_status": None,
    "opened_at": "2026-10-11T10:00:00Z", "timestamp": "2026-10-11T10:00:00Z", "count": 1,
}


def note(incident, event):
    return {"incident": copy.deepcopy(incident), "event": event}


def recovered_of(incident):
    inc = copy.deepcopy(incident)
    inc["state"] = "resolved"
    inc["timestamp"] = "2026-10-11T11:30:00Z"
    return inc


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeTelegram:
    """Each entry is an HTTP status code, or "down" to raise a connect error that carries the URL."""

    def __init__(self, outcomes=("ok",)):
        self.outcomes = list(outcomes)
        self.urls = []
        self.payloads = []

    def _next(self):
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    def handle(self, request):
        self.urls.append(str(request.url))
        self.payloads.append(json.loads(request.content))
        outcome = self._next()
        if outcome == "down":
            raise httpx.ConnectError(f"could not connect to {request.url}")
        status = 200 if outcome == "ok" else outcome
        return httpx.Response(status, json={"ok": status == 200})

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    @property
    def texts(self):
        return [p["text"] for p in self.payloads]


class SMTPState:
    """outcomes: "ok" or "down". fail_to: recipients whose mail always fails."""

    def __init__(self, outcomes=("ok",), fail_to=()):
        self.outcomes = list(outcomes)
        self.fail_to = set(fail_to)
        self.attempts = 0
        self.messages = []
        self.logins = []
        self.starttls_calls = 0

    def factory(self, host, port, timeout=None):
        return _FakeSMTP(self, host, port, timeout)


class _FakeSMTP:
    def __init__(self, state, host, port, timeout):
        self.state = state
        self.host, self.port, self.timeout = host, port, timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return None

    def starttls(self, context=None):
        self.state.starttls_calls += 1

    def login(self, user, password):
        self.state.logins.append(user)

    def send_message(self, message):
        state = self.state
        state.attempts += 1
        outcome = state.outcomes.pop(0) if len(state.outcomes) > 1 else state.outcomes[0]
        if outcome == "down" or message["To"] in state.fail_to:
            raise smtplib.SMTPServerDisconnected(f"auth failed for {SMTP_PASSWORD} to {message['To']}")
        state.messages.append(message)


def run(notifications, contacts=CONTACTS, *, tg=None, smtp=None):
    tg = tg or FakeTelegram()
    smtp = smtp or SMTPState()
    sleeps = []
    results = D.dispatch(
        notifications,
        contacts,
        sleep=sleeps.append,
        http_transport=tg.transport,
        smtp_factory=smtp.factory,
    )
    return results, tg, smtp, sleeps


def key_results(results):
    return [(r["channel"], r["status"]) for r in results]


def to_of(message):
    return message["To"]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("real network call in a unit test")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(smtplib.SMTP, "__init__", boom)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in list(ENV_FULL) + ["MOTAHAI_SMTP_HOST"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV_FULL.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def _reset_once_logging():
    D._warned_not_configured.clear()
    yield
    D._warned_not_configured.clear()


def _clear_smtp(monkeypatch):
    for name in SMTP_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# routing: admin channels
# ---------------------------------------------------------------------------

def test_constants_match_spec():
    assert D.TELEGRAM_TIMEOUT_S == 10.0
    assert D.RETRY_DELAY_S == 5.0


def test_crit_open_sends_telegram_and_admin_email():
    results, tg, smtp, sleeps = run([note(ROUTER_CRIT, "open")])
    assert key_results(results) == [("telegram", "sent"), ("admin_email", "sent")]
    assert sleeps == []
    assert tg.urls == [f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"]
    assert tg.payloads[0]["chat_id"] == CHAT_ID
    assert tg.payloads[0]["disable_web_page_preview"] is True
    assert "[CRIT]" in tg.texts[0]
    assert [to_of(m) for m in smtp.messages] == [ADMIN_EMAIL]
    assert smtp.starttls_calls == 1
    assert smtp.logins == ["mailer@example.test"]


def test_crit_reminder_and_recovered_go_to_both_channels():
    results, tg, smtp, _ = run([note(ROUTER_CRIT, "reminder"), note(recovered_of(ROUTER_CRIT), "recovered")])
    assert [r["status"] for r in results] == ["sent"] * 4
    assert [r["channel"] for r in results] == ["telegram", "admin_email", "telegram", "admin_email"]
    assert "[RECOVERED]" in tg.texts[1]
    assert len(smtp.messages) == 2


def test_warn_is_telegram_only_when_telegram_works():
    results, tg, smtp, sleeps = run([note(BUDGET_WARN, "open")])
    assert key_results(results) == [("telegram", "sent")]
    assert len(tg.texts) == 1
    assert smtp.attempts == 0
    assert sleeps == []


def test_warn_email_only_when_telegram_fails():
    results, tg, smtp, sleeps = run([note(BUDGET_WARN, "open")], tg=FakeTelegram(["down"]))
    assert key_results(results) == [("telegram", "failed"), ("admin_email", "sent")]
    assert tg.urls and len(tg.urls) == 2  # first attempt and one retry
    assert sleeps == [5.0]  # telegram retry only; email went out on the first try
    assert smtp.attempts == 1


def test_telegram_retry_succeeds_so_no_email_for_warn():
    results, tg, smtp, sleeps = run([note(BUDGET_WARN, "open")], tg=FakeTelegram(["down", "ok"]))
    assert key_results(results) == [("telegram", "sent")]
    assert sleeps == [5.0]
    assert smtp.attempts == 0


def test_crit_email_still_sent_when_telegram_down():
    results, _, smtp, _ = run([note(ROUTER_CRIT, "open")], tg=FakeTelegram(["down"]))
    assert key_results(results) == [("telegram", "failed"), ("admin_email", "sent")]
    assert [to_of(m) for m in smtp.messages] == [ADMIN_EMAIL]


def test_both_channels_down_logs_and_does_not_raise(caplog):
    with caplog.at_level(logging.DEBUG):
        results, tg, smtp, sleeps = run(
            [note(ROUTER_CRIT, "open")],
            tg=FakeTelegram(["down"]),
            smtp=SMTPState(["down"]),
        )
    assert key_results(results) == [("telegram", "failed"), ("admin_email", "failed")]
    assert sleeps == [5.0, 5.0]  # telegram retry, email retry
    assert tg.urls and len(tg.urls) == 2
    assert smtp.attempts == 2
    assert "channel=telegram outcome=failed" in caplog.text
    assert "channel=admin_email outcome=failed" in caplog.text


def test_email_retry_succeeds_on_second_attempt():
    results, _, smtp, sleeps = run([note(ROUTER_CRIT, "open")], smtp=SMTPState(["down", "ok"]))
    assert key_results(results) == [("telegram", "sent"), ("admin_email", "sent")]
    assert sleeps == [5.0]
    assert smtp.attempts == 2
    assert len(smtp.messages) == 1


def test_budget_80_recovered_sends_nothing():
    results, tg, smtp, sleeps = run([note(recovered_of(BUDGET_WARN), "recovered")])
    assert results == []
    assert tg.urls == []
    assert smtp.attempts == 0
    assert sleeps == []


def test_budget_80_open_is_warn_telegram_only():
    results, _, smtp, _ = run([note(BUDGET_WARN, "reminder")])
    assert key_results(results) == [("telegram", "sent")]
    assert smtp.attempts == 0


def test_admin_email_is_utf8_with_arabic_body():
    _, _, smtp, _ = run([note(ROUTER_CRIT, "open")], tg=FakeTelegram(["down"]))
    message = smtp.messages[0]
    assert message.get_content_charset() == "utf-8"
    assert "جميع نماذج اللغة" in message.get_content()


# ---------------------------------------------------------------------------
# client emails (flow_stalled CRIT only)
# ---------------------------------------------------------------------------

def test_client_holding_on_open_then_followup_on_recovered():
    results, tg, smtp, _ = run([note(FLOW_CRIT, "open")])
    assert key_results(results) == [
        ("client_email", "sent"), ("telegram", "sent"), ("admin_email", "sent"),
    ]
    holding_subject, holding_body = T.client_holding_email(lang="ar", store_name="Store X")
    client_msgs = [m for m in smtp.messages if to_of(m) == CLIENT_EMAIL]
    assert len(client_msgs) == 1
    assert client_msgs[0]["Subject"] == holding_subject
    assert T.ARABIC_HOLDING_TEXT in client_msgs[0].get_content()
    assert "Client holding email sent." in tg.texts[0]

    results2, tg2, smtp2, _ = run([note(recovered_of(FLOW_CRIT), "recovered")])
    assert ("client_email", "sent") in key_results(results2)
    followup_subject, _ = T.client_followup_email(lang="ar", store_name="Store X")
    client_msgs2 = [m for m in smtp2.messages if to_of(m) == CLIENT_EMAIL]
    assert len(client_msgs2) == 1
    assert client_msgs2[0]["Subject"] == followup_subject
    assert "[RECOVERED]" in tg2.texts[0]


def test_client_not_emailed_on_reminder():
    results, _, smtp, _ = run([note(FLOW_CRIT, "reminder")])
    assert all(r["channel"] != "client_email" for r in results)
    assert [to_of(m) for m in smtp.messages] == [ADMIN_EMAIL]


def test_client_email_is_one_per_incident_per_event():
    notes = [note(FLOW_CRIT, "open"), note(FLOW_CRIT, "reminder"), note(FLOW_CRIT, "reminder")]
    results, _, smtp, _ = run(notes)
    client_rows = [r for r in results if r["channel"] == "client_email"]
    assert len(client_rows) == 1
    assert len([m for m in smtp.messages if to_of(m) == CLIENT_EMAIL]) == 1


def test_no_contact_skips_client_and_admin_text_says_so():
    results, tg, smtp, _ = run([note(FLOW_CRIT, "open")], contacts={})
    assert ("client_email", "skipped:no_contact") in key_results(results)
    assert "client not notified: no contact on file" in tg.texts[0].lower()
    assert [to_of(m) for m in smtp.messages] == [ADMIN_EMAIL]


def test_contacts_none_is_treated_as_no_contact():
    results, _, _, _ = run([note(FLOW_CRIT, "open")], contacts=None)
    assert ("client_email", "skipped:no_contact") in key_results(results)


def test_contact_without_email_is_no_contact():
    contacts = {"GTM-1234567": {"store_name": "Store X", "lang": "ar"}}
    results, _, smtp, _ = run([note(FLOW_CRIT, "open")], contacts=contacts)
    assert ("client_email", "skipped:no_contact") in key_results(results)
    assert all(to_of(m) != CLIENT_EMAIL for m in smtp.messages)


def test_client_send_failure_is_reported_as_not_notified():
    smtp = SMTPState(fail_to={CLIENT_EMAIL})
    results, tg, _, sleeps = run([note(FLOW_CRIT, "open")], smtp=smtp)
    assert ("client_email", "failed") in key_results(results)
    assert "Client holding email sent." not in tg.texts[0]
    assert sleeps == [5.0]  # client email retried once; Telegram and admin email went out first try


def test_client_not_emailed_for_warn_flow_stall():
    warn_flow = dict(FLOW_CRIT, severity="WARN")
    results, _, _, _ = run([note(warn_flow, "open")])
    assert all(r["channel"] != "client_email" for r in results)


def test_client_language_defaults_to_arabic_and_english_is_honoured():
    contacts = {"GTM-1234567": {"email": CLIENT_EMAIL, "store_name": "Store X"}}
    _, _, smtp, _ = run([note(FLOW_CRIT, "open")], contacts=contacts)
    client = [m for m in smtp.messages if to_of(m) == CLIENT_EMAIL][0]
    assert T.ARABIC_HOLDING_TEXT in client.get_content()

    contacts_en = {"GTM-1234567": {"email": CLIENT_EMAIL, "store_name": "Store X", "lang": "en"}}
    _, _, smtp_en, _ = run([note(FLOW_CRIT, "open")], contacts=contacts_en)
    client_en = [m for m in smtp_en.messages if to_of(m) == CLIENT_EMAIL][0]
    assert "Hello Store X team," in client_en.get_content()


# ---------------------------------------------------------------------------
# config: missing channels are skipped, logged once, never raise
# ---------------------------------------------------------------------------

def test_no_telegram_config_falls_back_to_email_for_warn(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    results, tg, smtp, _ = run([note(BUDGET_WARN, "open")])
    assert key_results(results) == [("telegram", "skipped:not_configured"), ("admin_email", "sent")]
    assert tg.urls == []
    assert len(smtp.messages) == 1


def test_no_smtp_env_skips_email_without_raising(monkeypatch):
    _clear_smtp(monkeypatch)
    results, tg, smtp, _ = run([note(ROUTER_CRIT, "open")])
    assert key_results(results) == [("telegram", "sent"), ("admin_email", "skipped:not_configured")]
    assert smtp.attempts == 0


def test_no_smtp_env_and_telegram_down_does_not_raise(monkeypatch):
    _clear_smtp(monkeypatch)
    results, _, smtp, _ = run([note(ROUTER_CRIT, "open")], tg=FakeTelegram(["down"]))
    assert key_results(results) == [("telegram", "failed"), ("admin_email", "skipped:not_configured")]
    assert smtp.attempts == 0


def test_no_admin_email_address_skips_admin_email(monkeypatch):
    monkeypatch.delenv("ADMIN_ALERT_EMAIL")
    results, _, smtp, _ = run([note(ROUTER_CRIT, "open")])
    assert ("admin_email", "skipped:not_configured") in key_results(results)
    assert smtp.attempts == 0


def test_no_smtp_env_skips_client_email(monkeypatch):
    _clear_smtp(monkeypatch)
    results, _, smtp, _ = run([note(FLOW_CRIT, "open")])
    assert ("client_email", "skipped:not_configured") in key_results(results)
    assert smtp.attempts == 0


def test_invalid_smtp_port_is_treated_as_not_configured(monkeypatch):
    monkeypatch.setenv("MOTAHAI_SMTP_PORT", "not-a-port")
    results, _, smtp, _ = run([note(ROUTER_CRIT, "open")])
    assert ("admin_email", "skipped:not_configured") in key_results(results)
    assert smtp.attempts == 0


def test_missing_channel_is_logged_once_per_process(monkeypatch, caplog):
    _clear_smtp(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            results, _, _, _ = run([note(ROUTER_CRIT, "open")])
            assert ("admin_email", "skipped:not_configured") in key_results(results)
    warnings = [r for r in caplog.records if "channel=admin_email not configured" in r.getMessage()]
    assert len(warnings) == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# ---------------------------------------------------------------------------
# results and robustness
# ---------------------------------------------------------------------------

def test_result_shape_and_statuses():
    results, _, _, _ = run([note(FLOW_CRIT, "open")], tg=FakeTelegram(["down"]), smtp=SMTPState(fail_to={CLIENT_EMAIL}))
    allowed = {"sent", "failed", "skipped:not_configured", "skipped:no_contact"}
    channels = {"telegram", "admin_email", "client_email"}
    assert results
    for row in results:
        assert set(row) == {"incident_key", "channel", "status"}
        assert row["incident_key"] == "flow:GTM-1234567:7"
        assert row["channel"] in channels
        assert row["status"] in allowed


def test_malformed_notifications_never_raise():
    garbage = [
        None,
        "not-a-dict",
        {"event": "open", "incident": None},
        {"event": "bogus", "incident": {}},
        {"incident": {"severity": "CRIT", "kind": "router_exhausted"}, "event": "open"},
    ]
    results, _, _, _ = run(garbage)
    assert isinstance(results, list)


def test_empty_notifications_return_empty_list():
    results, tg, smtp, _ = run([])
    assert results == []
    assert tg.urls == [] and smtp.attempts == 0


def test_caller_incident_is_not_mutated():
    before = copy.deepcopy(FLOW_CRIT)
    run([{"incident": FLOW_CRIT, "event": "open"}])
    assert FLOW_CRIT == before
    assert "client_notified" not in FLOW_CRIT


def test_client_notified_flag_reflected_in_admin_text():
    _, tg_yes, _, _ = run([note(FLOW_CRIT, "open")])
    assert "Client holding email sent." in tg_yes.texts[0]
    _, tg_no, _, _ = run([note(FLOW_CRIT, "open")], contacts={})
    assert "client not notified: no contact on file" in tg_no.texts[0].lower()


# ---------------------------------------------------------------------------
# privacy: secrets and addresses never reach logs or results
# ---------------------------------------------------------------------------

def test_token_password_and_addresses_never_logged_or_returned(caplog):
    notes = [note(FLOW_CRIT, "open"), note(ROUTER_CRIT, "open"), note(FLOW_CRIT, "recovered")]
    with caplog.at_level(logging.DEBUG):
        results, _, _, _ = run(notes, tg=FakeTelegram(["down"]), smtp=SMTPState(["down"]))
    captured = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    rendered = repr(results)
    for secret in (BOT_TOKEN, "SECRET-TOKEN-XYZ", SMTP_PASSWORD, ADMIN_EMAIL, CLIENT_EMAIL, "ops-admin", "client-owner"):
        assert secret not in captured
        assert secret not in rendered


def test_httpx_request_log_has_bot_token_redacted(caplog):
    with caplog.at_level(logging.DEBUG):
        run([note(BUDGET_WARN, "open")])
    assert "SECRET-TOKEN-XYZ" not in caplog.text
    assert "bot<redacted>" in caplog.text


def test_failure_logs_carry_type_names_only(caplog):
    with caplog.at_level(logging.DEBUG):
        run([note(ROUTER_CRIT, "open")], tg=FakeTelegram(["down"]), smtp=SMTPState(["down"]))
    assert "ConnectError" in caplog.text
    assert "SMTPServerDisconnected" in caplog.text
    assert "could not connect" not in caplog.text
    assert "auth failed" not in caplog.text
