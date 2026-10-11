"""Alert dispatcher for the Hermes token-exhaustion sentinel (T3).

``dispatch(notifications, contacts)`` sends the admin alerts (Telegram, email) and, for a CRIT
``flow_stalled`` incident, the client emails. Every message text comes from ``templates`` (C3).
No LLM, no Core package import. Stdlib + httpx only.

Routing:
- Admin, CRIT (open / reminder / recovered): Telegram AND email.
- Admin, WARN: Telegram only; email only when Telegram did not send (failed or not configured).
- budget_80 recovered: nothing.
- Client (flow_stalled CRIT only): holding email on "open", follow-up on "recovered", never on
  "reminder"; only when the contacts file has an entry for the incident's container_id.

Logging: only incident_key, channel and outcome, plus the exception TYPE name on a failed attempt.
Never a token, a password, an address, a URL or a message body. The function never raises.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import templates as T

logger = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TIMEOUT_S = 10.0
SMTP_TIMEOUT_S = 30.0
RETRY_DELAY_S = 5.0

SENT = "sent"
FAILED = "failed"
NOT_CONFIGURED = "skipped:not_configured"
NO_CONTACT = "skipped:no_contact"

CH_TELEGRAM = "telegram"
CH_ADMIN_EMAIL = "admin_email"
CH_CLIENT_EMAIL = "client_email"

EVENTS = ("open", "reminder", "recovered")
DEFAULT_LANG = "ar"

_warned_not_configured: set = set()


# ---------------------------------------------------------------------------
# log hygiene
# ---------------------------------------------------------------------------

_BOT_PATH = re.compile(r"/bot[^/\s\"'?]+")


class _RedactBotToken(logging.Filter):
    """httpx logs every request URL at INFO, and the Telegram URL carries the bot token. Redact it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, keep it but never leak
            return True
        if "/bot" in message:
            record.msg = _BOT_PATH.sub("/bot<redacted>", message)
            record.args = ()
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactBotToken())


def _not_configured(channel: str, detail: str) -> None:
    """Log that a channel is skipped, once per process."""
    if channel in _warned_not_configured:
        return
    _warned_not_configured.add(channel)
    logger.warning("channel=%s not configured (%s); skipping", channel, detail)


def _record(results: List[Dict[str, str]], key: str, channel: str, status: str) -> None:
    results.append({"incident_key": key, "channel": channel, "status": status})
    logger.info("incident=%s channel=%s outcome=%s", key, channel, status)


# ---------------------------------------------------------------------------
# configuration (read at call time, so the sentinel process picks up .env changes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Smtp:
    host: str
    port: int
    sender: str
    user: Optional[str]
    password: Optional[str]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _telegram_config() -> Optional[tuple]:
    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("ADMIN_TELEGRAM_CHAT_ID")
    return (token, chat_id) if token and chat_id else None


def _smtp_config() -> Optional[_Smtp]:
    host = _env("MOTAHAI_SMTP_HOST")
    sender = _env("MOTAHAI_SMTP_FROM")
    if not host or not sender:
        return None
    try:
        port = int(_env("MOTAHAI_SMTP_PORT") or "587")
    except ValueError:
        _not_configured("smtp", "MOTAHAI_SMTP_PORT is not a number")
        return None
    return _Smtp(
        host=host,
        port=port,
        sender=sender,
        user=_env("MOTAHAI_SMTP_USER") or None,
        password=os.environ.get("MOTAHAI_SMTP_PASSWORD") or None,
    )


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------

def _attempt(once: Callable[[], bool], sleep: Callable[[float], None]) -> bool:
    """Run once; on failure wait RETRY_DELAY_S and run exactly one more time."""
    if once():
        return True
    sleep(RETRY_DELAY_S)
    return once()


def _send_telegram(key: str, text: str, deps: "_Deps") -> str:
    cfg = _telegram_config()
    if cfg is None:
        _not_configured(CH_TELEGRAM, "TELEGRAM_BOT_TOKEN or ADMIN_TELEGRAM_CHAT_ID unset")
        return NOT_CONFIGURED
    token, chat_id = cfg
    url = TELEGRAM_URL.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}

    def once() -> bool:
        try:
            with httpx.Client(timeout=TELEGRAM_TIMEOUT_S, transport=deps.http_transport) as client:
                response = client.post(url, json=payload)
        except Exception as exc:
            logger.warning("incident=%s channel=%s attempt=%s", key, CH_TELEGRAM, type(exc).__name__)
            return False
        if response.is_success:
            return True
        logger.warning("incident=%s channel=%s attempt=http_%s", key, CH_TELEGRAM, response.status_code)
        return False

    return SENT if _attempt(once, deps.sleep) else FAILED


def _deliver_email(key: str, channel: str, smtp: _Smtp, to: str, subject: str, body: str, deps: "_Deps") -> str:
    def once() -> bool:
        try:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = smtp.sender
            message["To"] = to
            message.set_content(body)  # text/plain, UTF-8 (Arabic)
            with deps.smtp_factory(smtp.host, smtp.port, timeout=SMTP_TIMEOUT_S) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                if smtp.user:
                    server.login(smtp.user, smtp.password or "")
                server.send_message(message)
            return True
        except Exception as exc:
            logger.warning("incident=%s channel=%s attempt=%s", key, channel, type(exc).__name__)
            return False

    return SENT if _attempt(once, deps.sleep) else FAILED


# ---------------------------------------------------------------------------
# channel handlers
# ---------------------------------------------------------------------------

@dataclass
class _Deps:
    sleep: Callable[[float], None]
    http_transport: Optional[httpx.BaseTransport]
    smtp_factory: Callable[..., Any]


def _send_admin(key: str, incident: dict, recovered: bool, crit: bool, deps: _Deps, results: list) -> None:
    telegram_status = _send_telegram(key, T.admin_telegram(incident, recovered=recovered), deps)
    _record(results, key, CH_TELEGRAM, telegram_status)
    if crit or telegram_status != SENT:
        _send_admin_email(key, incident, recovered, deps, results)


def _send_admin_email(key: str, incident: dict, recovered: bool, deps: _Deps, results: list) -> None:
    to = _env("ADMIN_ALERT_EMAIL")
    smtp = _smtp_config()
    if not to or smtp is None:
        _not_configured(CH_ADMIN_EMAIL, "ADMIN_ALERT_EMAIL or MOTAHAI_SMTP_HOST/MOTAHAI_SMTP_FROM unset")
        _record(results, key, CH_ADMIN_EMAIL, NOT_CONFIGURED)
        return
    subject, body = T.admin_email(incident, recovered=recovered)
    status = _deliver_email(key, CH_ADMIN_EMAIL, smtp, to, subject, body, deps)
    _record(results, key, CH_ADMIN_EMAIL, status)


def _client_contact(incident: dict, contacts) -> Optional[dict]:
    if not isinstance(contacts, dict):
        return None
    container = incident.get("container_id")
    if not isinstance(container, str) or not container:
        return None
    entry = contacts.get(container)
    if not isinstance(entry, dict):
        return None
    email = entry.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    return entry


def _send_client(key: str, incident: dict, event: str, contacts, deps: _Deps, results: list) -> bool:
    """Send the client holding (open) or follow-up (recovered) email. Returns client_notified."""
    entry = _client_contact(incident, contacts)
    if entry is None:
        _record(results, key, CH_CLIENT_EMAIL, NO_CONTACT)
        return False
    smtp = _smtp_config()
    if smtp is None:
        _not_configured(CH_CLIENT_EMAIL, "MOTAHAI_SMTP_HOST or MOTAHAI_SMTP_FROM unset")
        _record(results, key, CH_CLIENT_EMAIL, NOT_CONFIGURED)
        return False

    lang = entry.get("lang")
    lang = lang if isinstance(lang, str) and lang.strip() else DEFAULT_LANG
    store = entry.get("store_name")
    store = store if isinstance(store, str) else None
    if event == "open":
        subject, body = T.client_holding_email(lang=lang, store_name=store)
    else:
        subject, body = T.client_followup_email(lang=lang, store_name=store)

    status = _deliver_email(key, CH_CLIENT_EMAIL, smtp, entry["email"].strip(), subject, body, deps)
    _record(results, key, CH_CLIENT_EMAIL, status)
    return status == SENT


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _key_of(notification) -> str:
    incident = notification.get("incident") if isinstance(notification, dict) else None
    if isinstance(incident, dict) and incident.get("incident_key") is not None:
        return str(incident.get("incident_key"))
    return "unknown"


def _process(notification: dict, contacts, deps: _Deps, results: list) -> None:
    event = notification.get("event")
    incident = dict(notification.get("incident") or {})
    key = _key_of(notification)
    if event not in EVENTS:
        logger.warning("incident=%s outcome=unknown_event", key)
        return

    kind = incident.get("kind")
    crit = str(incident.get("severity") or "").upper() == "CRIT"
    recovered = event == "recovered"

    if kind == "budget_80" and recovered:
        return

    if kind == "flow_stalled" and crit and event in ("open", "recovered"):
        incident["client_notified"] = _send_client(key, incident, event, contacts, deps, results)

    _send_admin(key, incident, recovered, crit, deps, results)


def dispatch(
    notifications,
    contacts,
    *,
    sleep: Callable[[float], None] = time.sleep,
    http_transport: Optional[httpx.BaseTransport] = None,
    smtp_factory: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, str]]:
    """Send the notifications. Returns one {"incident_key", "channel", "status"} per attempted channel.

    ``sleep``, ``http_transport`` and ``smtp_factory`` exist for tests (no real waits, no network).
    Never raises. Caller's incident dicts are not mutated.
    """
    deps = _Deps(
        sleep=sleep,
        http_transport=http_transport,
        smtp_factory=smtp_factory or smtplib.SMTP,
    )
    results: List[Dict[str, str]] = []
    for notification in notifications or []:
        try:
            _process(notification, contacts, deps, results)
        except Exception as exc:
            logger.error("incident=%s outcome=dispatch_error error=%s", _key_of(notification), type(exc).__name__)
    return results
