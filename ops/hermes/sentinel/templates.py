"""Static alert and client-email templates for the Hermes token-exhaustion sentinel.

Deterministic, plain text only (no Markdown, no HTML). Pure stdlib. No LLM, no
I/O, no network. Everything rendered comes from the incident dict or from the
explicit arguments; nothing else is ever included (no prompts, keys or PII).
"""

from __future__ import annotations

from datetime import datetime

TELEGRAM_LIMIT = 3500
SUBJECT_LIMIT = 120
EMAIL_FOOTER = "Sent automatically by hermes-sentinel. No LLM was used to write this message."

# Owner-approved Arabic holding text. Do not edit a single character.
ARABIC_HOLDING_TEXT = (
    "استلمنا طلبكم ونعمل الآن على استكمال فحص بنية التتبع الخاصة بمتجركم. "
    "نحتاج وقتاً إضافياً لإتمام المعالجة، وسنرسل لكم التقرير فور جاهزيته، "
    "وسنُطلعكم إن تأخر أكثر من المتوقع."
)

_MAX_FIELD = 300
_MAX_PROVIDERS = 20
_TRUNC_SUFFIX = "\n[truncated]"

_KINDS = (
    "router_exhausted",
    "router_unreachable",
    "provider_degraded",
    "budget_80",
    "flow_stalled",
    "sentinel_error",
)
_EXHAUSTION_KINDS = ("rate_limited", "daily_quota", "payment", "auth", "model_gone", "mixed")

_ACTION_BY_EXHAUSTION = {
    "daily_quota": "wait for the provider's daily reset or add a tier",
    "payment": "top up the provider balance",
    "auth": "rotate the provider API key",
    "model_gone": "replace the model in providers.yaml",
}
_DEFAULT_ACTION = "check the router journal"

_AR_KIND = {
    "router_exhausted": "جميع نماذج اللغة مستنفدة، وأدوات طارق التي تحتاج نموذجاً تفشل.",
    "router_unreachable": "خدمة موجّه النماذج غير متاحة.",
    "provider_degraded": "تم تعطيل أحد المزودين مؤقتاً.",
    "budget_80": "أحد المزودين بلغ 80% من ميزانيته اليومية.",
    "flow_stalled": "توقف سير العمل لدى طارق بانتظار الخطوة التالية.",
    "sentinel_error": "تعذّر على الحارس قراءة مدخلاته.",
}
_AR_UNKNOWN = "تنبيه غير معروف."


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _inc(incident) -> dict:
    return incident if isinstance(incident, dict) else {}


def _clean(value, limit: int = _MAX_FIELD) -> str:
    """Render a value as one plain line: no Markdown markers, no control chars."""
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:  # pragma: no cover - defensive, str() of odd objects
        return ""
    text = text.replace("`", "").replace("*", "")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(limit - 3, 0)].rstrip() + "..."
    return text


def _known(value, choices) -> str | None:
    """Return value if it is one of the known choices, else None."""
    return value if isinstance(value, str) and value in choices else None


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _duration(opened_at, timestamp) -> str | None:
    start = _parse_ts(opened_at)
    end = _parse_ts(timestamp)
    if start is None or end is None:
        return None
    try:
        seconds = int((end - start).total_seconds())
    except (TypeError, OverflowError):  # naive vs aware datetimes, etc.
        return None
    if seconds < 0:
        return None
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


_DEFAULT_SEVERITY = {
    "router_exhausted": "CRIT",
    "router_unreachable": "CRIT",
    "provider_degraded": "CRIT",
    "flow_stalled": "CRIT",
    "budget_80": "WARN",
    "sentinel_error": "WARN",
}


def _severity(inc: dict) -> str:
    kind = _known(inc.get("kind"), _KINDS)
    fallback = _DEFAULT_SEVERITY.get(kind, "WARN")
    return _clean(inc.get("severity"), 12).upper() or fallback


# ---------------------------------------------------------------------------
# admin text
# ---------------------------------------------------------------------------

def _title(kind, exhaustion) -> str:
    if kind == "router_exhausted":
        return f"all LLM tiers exhausted ({exhaustion})" if exhaustion else "all LLM tiers exhausted"
    if kind == "router_unreachable":
        return "LLM router unreachable"
    if kind == "provider_degraded":
        return f"provider degraded ({exhaustion})" if exhaustion else "provider degraded"
    if kind == "budget_80":
        return "provider at 80% of daily budget"
    if kind == "flow_stalled":
        return "Tariq flow stalled"
    if kind == "sentinel_error":
        return "sentinel error"
    return "unrecognized alert"


def _explain(inc: dict, kind, exhaustion) -> list[str]:
    action = _ACTION_BY_EXHAUSTION.get(exhaustion, _DEFAULT_ACTION)
    if kind == "router_exhausted":
        return [f"Hermes cannot reach any model; Tariq tool calls that need an LLM are failing. Action: {action}."]
    if kind == "router_unreachable":
        return ["The LLM router service is down or its /healthz check is failing. Action: systemctl status hermes-llm-router."]
    if kind == "provider_degraded":
        return [f"One provider has been benched. Action: {action}."]
    if kind == "budget_80":
        return ["A provider has used at least 80% of its daily request budget. Action: watch it and add a tier before the budget runs out."]
    if kind == "flow_stalled":
        container = _clean(inc.get("container_id"), 40)
        step = _clean(inc.get("last_successful_step"), 120)
        where = f" for container {container}" if container else ""
        after = f" after step {step}" if step else ""
        lines = [
            f"Tariq was owed the next step{where}{after} and has been silent for over 5 minutes. "
            "Likely cause: Wesam's own model is failing. Action: check the Wesam conversation."
        ]
        # client_notified is tri-state: True = sent, False = no contact on file,
        # "failed" = contact exists but the send failed or SMTP is not configured.
        notified = inc.get("client_notified")
        if notified is False:
            lines.append("Client not notified: no contact on file.")
        elif notified is True:
            lines.append("Client holding email sent.")
        elif notified == "failed":
            lines.append("Client holding email FAILED to send — contact the client manually.")
        return lines
    if kind == "sentinel_error":
        return ["The sentinel itself could not read its inputs. Action: check the sentinel logs and its input files."]
    return ["An alert this template does not recognize was raised. Action: check the sentinel logs."]


def _arabic_line(kind, recovered: bool) -> str:
    text = _AR_KIND.get(kind, _AR_UNKNOWN)
    return f"تم التعافي: {text}" if recovered else text


def _facts(inc: dict, recovered: bool) -> list[str]:
    rows: list[str] = []

    def add(label: str, value) -> None:
        text = _clean(value)
        if text:
            rows.append(f"{label}: {text}")

    add("Key", inc.get("incident_key"))
    add("Scope", inc.get("scope"))

    providers = inc.get("providers")
    if isinstance(providers, (list, tuple)):
        names = [_clean(p, 60) for p in providers]
        names = [n for n in names if n]
        if names:
            shown = ", ".join(names[:_MAX_PROVIDERS])
            extra = len(names) - _MAX_PROVIDERS
            rows.append(f"Providers: {shown}" + (f" (+{extra} more)" if extra > 0 else ""))
    elif providers is not None:
        add("Providers", providers)

    add("Container", inc.get("container_id"))
    add("Last step", inc.get("last_successful_step"))
    add("Opened", inc.get("opened_at"))
    if recovered:
        add("Recovered", inc.get("timestamp"))
    add("Count", inc.get("count"))
    return rows


def _is_recovered(inc: dict, recovered) -> bool:
    return bool(recovered) or inc.get("state") == "resolved"


def _admin_parts(incident, recovered) -> tuple[str, str, str]:
    """Return (headline, body, cause) for an admin alert."""
    inc = _inc(incident)
    is_rec = _is_recovered(inc, recovered)
    kind = _known(inc.get("kind"), _KINDS)
    exhaustion = _known(inc.get("exhaustion_kind"), _EXHAUSTION_KINDS)
    title = _title(kind, exhaustion)

    if is_rec:
        duration = _duration(inc.get("opened_at"), inc.get("timestamp"))
        head_tail = f" after {duration}" if duration else ""
        headline = f"[RECOVERED] Hermes: {title}{head_tail}"
        tag = "RECOVERED"
    else:
        headline = f"[{_severity(inc)}] Hermes: {title}"
        tag = _severity(inc)

    lines = [headline, _arabic_line(kind, is_rec), ""]
    lines.extend(_explain(inc, kind, exhaustion))
    facts = _facts(inc, is_rec)
    if facts:
        lines.append("")
        lines.extend(facts)

    body = "\n".join(lines)
    if len(body) > TELEGRAM_LIMIT:
        body = body[: TELEGRAM_LIMIT - len(_TRUNC_SUFFIX)].rstrip() + _TRUNC_SUFFIX
    return headline, body, f"[{tag}] Hermes sentinel: {title}"


def _fallback_text() -> str:
    return "[WARN] Hermes: alert could not be rendered. Check the sentinel logs."


def admin_telegram(incident: dict, recovered: bool = False) -> str:
    """Plain-text admin alert for Telegram. Always <= 3500 characters."""
    try:
        return _admin_parts(incident, recovered)[1]
    except Exception:
        return _fallback_text()


def admin_email(incident: dict, recovered: bool = False) -> tuple[str, str]:
    """Return (subject, body) for the admin email. Subject <= 120 characters."""
    try:
        _, body, cause = _admin_parts(incident, recovered)
    except Exception:
        return ("[WARN] Hermes sentinel: alert could not be rendered", _fallback_text() + "\n\n" + EMAIL_FOOTER)
    subject = cause
    if len(subject) > SUBJECT_LIMIT:
        subject = subject[: SUBJECT_LIMIT - 3].rstrip() + "..."
    return subject, body + "\n\n" + EMAIL_FOOTER


# ---------------------------------------------------------------------------
# client emails
# ---------------------------------------------------------------------------

def _is_english(lang) -> bool:
    return isinstance(lang, str) and lang.strip().lower() == "en"


def _store(store_name) -> str:
    return _clean(store_name, 120)


_AR_SIGNOFF = "مع التحية،\nفريق مُتاح AI"
_EN_SIGNOFF = "Best regards,\nThe Motahai AI team"

_EN_HOLDING = (
    "We have received your request and are now working on completing the check of your "
    "store's tracking setup. We need additional time to finish processing. We will send "
    "you the report as soon as it is ready, and we will let you know if it takes longer "
    "than expected."
)
_EN_FOLLOWUP = (
    "Thank you for your patience. Processing of your request has resumed, and we will "
    "send you the report as soon as it is ready."
)
_AR_FOLLOWUP = "شكراً لصبركم. استأنفنا معالجة طلبكم، وسنرسل لكم التقرير فور جاهزيته."


def _greeting(lang_en: bool, store: str) -> str:
    if lang_en:
        return f"Hello {store} team," if store else "Hello,"
    return f"فريق متجر {store}،" if store else "مرحباً،"


def client_holding_email(lang: str = "ar", store_name: str | None = None) -> tuple[str, str]:
    """Holding email sent to the client while the report is delayed."""
    en = _is_english(lang)
    store = _store(store_name)
    if en:
        subject = "Update on your store's tracking check"
        body = f"{_greeting(True, store)}\n\n{_EN_HOLDING}\n\n{_EN_SIGNOFF}"
    else:
        subject = "تحديث بخصوص فحص التتبع لمتجركم"
        body = f"{_greeting(False, store)}\n\n{ARABIC_HOLDING_TEXT}\n\n{_AR_SIGNOFF}"
    return subject, body


def client_followup_email(lang: str = "ar", store_name: str | None = None) -> tuple[str, str]:
    """Follow-up sent to the client after the processing has recovered."""
    en = _is_english(lang)
    store = _store(store_name)
    if en:
        subject = "Update: your tracking check has resumed"
        body = f"{_greeting(True, store)}\n\n{_EN_FOLLOWUP}\n\n{_EN_SIGNOFF}"
    else:
        subject = "تحديث: استئناف فحص التتبع لمتجركم"
        body = f"{_greeting(False, store)}\n\n{_AR_FOLLOWUP}\n\n{_AR_SIGNOFF}"
    return subject, body


# ---------------------------------------------------------------------------
# activity telemetry (T6): one plain-text Telegram summary per sentinel pass
# ---------------------------------------------------------------------------

TELEMETRY_MAX_LINES = 25


def _latency_text(latency_ms) -> str:
    try:
        ms = max(0, int(latency_ms))
    except (TypeError, ValueError):
        return ""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _window_text(window_s) -> str:
    if not isinstance(window_s, (int, float)) or isinstance(window_s, bool) or window_s <= 0:
        return ""
    seconds = int(window_s)
    if seconds < 120:
        return f" (last {max(10, round(seconds / 10) * 10)} s)"
    return f" (last {seconds // 60} min)"


def telemetry_summary(events, total=None, window_s=None) -> str:
    """Plain-text summary of Hermes tool calls, always <= 3500 characters.

    `events` are validated telemetry events ({"tool", "outcome", "latency_ms", "subject"}), oldest first. `total` is the
    number of calls the message stands for (>= len(events) when older events were dropped by the backlog cap). At most
    TELEMETRY_MAX_LINES lines are listed, then "...and N more". Only the four fields above are ever rendered."""
    try:
        events = [e for e in (events or []) if isinstance(e, dict)]
        count = total if isinstance(total, int) and not isinstance(total, bool) and total >= len(events) else len(events)
        lines = [f"Hermes activity{_window_text(window_s)}: {count} call{'s' if count != 1 else ''}",
                 f"نشاط هيرمس: {count} استدعاء", ""]
        for event in events[:TELEMETRY_MAX_LINES]:
            parts = [_clean(event.get("tool"), 64) or "unknown", _clean(event.get("subject"), 80),
                     _clean(event.get("outcome"), 60), _latency_text(event.get("latency_ms"))]
            lines.append("• " + "  ".join(part for part in parts if part))
        extra = count - min(len(events), TELEMETRY_MAX_LINES)
        if extra > 0:
            lines.append(f"…and {extra} more")
        body = "\n".join(lines)
        if len(body) > TELEGRAM_LIMIT:
            body = body[: TELEGRAM_LIMIT - len(_TRUNC_SUFFIX)].rstrip() + _TRUNC_SUFFIX
        return body
    except Exception:
        return "Hermes activity: summary could not be rendered."
