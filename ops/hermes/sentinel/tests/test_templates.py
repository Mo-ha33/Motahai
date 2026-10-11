"""Tests for the sentinel alert and client-email templates. Run: python -m pytest tests -q"""

import re
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))
import templates as T  # noqa: E402


ARABIC_HOLDING = (
    "استلمنا طلبكم ونعمل الآن على استكمال فحص بنية التتبع الخاصة بمتجركم. "
    "نحتاج وقتاً إضافياً لإتمام المعالجة، وسنرسل لكم التقرير فور جاهزيته، "
    "وسنُطلعكم إن تأخر أكثر من المتوقع."
)
MARKDOWN_MARKERS = ("**", "```", "`", "*")
CLIENT_FORBIDDEN = re.compile(r"@|http|\d{8,}")

OPENED = "2026-10-10T10:00:00Z"
LATER = "2026-10-10T11:12:00Z"

KIND_CASES = [
    {"kind": "router_exhausted", "exhaustion_kind": "daily_quota", "scope": "router", "severity": "CRIT"},
    {"kind": "router_exhausted", "exhaustion_kind": "payment", "scope": "router", "severity": "CRIT"},
    {"kind": "router_exhausted", "exhaustion_kind": "auth", "scope": "router", "severity": "CRIT"},
    {"kind": "router_exhausted", "exhaustion_kind": "model_gone", "scope": "router", "severity": "CRIT"},
    {"kind": "router_exhausted", "exhaustion_kind": "rate_limited", "scope": "router", "severity": "CRIT"},
    {"kind": "router_exhausted", "exhaustion_kind": "mixed", "scope": "router", "severity": "CRIT"},
    {"kind": "router_unreachable", "scope": "router", "severity": "CRIT"},
    {"kind": "provider_degraded", "exhaustion_kind": "payment", "scope": "provider", "severity": "WARN",
     "providers": ["openrouter"]},
    {"kind": "budget_80", "scope": "provider", "severity": "WARN", "providers": ["gemini"]},
    {"kind": "flow_stalled", "scope": "container", "severity": "WARN", "container_id": "GTM-1234567",
     "last_successful_step": "publish", "client_notified": False},
    {"kind": "flow_stalled", "scope": "container", "severity": "WARN", "container_id": "GTM-1234567",
     "last_successful_step": "publish", "client_notified": True},
    {"kind": "sentinel_error", "scope": "container", "severity": "CRIT"},
]


def _incident(base: dict, *, recovered: bool) -> dict:
    inc = dict(base)
    default_key = "router:exhausted" if base["kind"] == "router_exhausted" else base["kind"]
    inc.setdefault("incident_key", default_key)
    inc["providers"] = inc.get("providers", ["nvidia", "openrouter", "gemini"])
    inc["opened_at"] = OPENED
    inc["timestamp"] = LATER if recovered else OPENED
    inc["state"] = "resolved" if recovered else "open"
    inc["count"] = 3
    inc["store_name"] = None
    inc["client_notified"] = inc.get("client_notified")
    return inc


ROUTER_EXHAUSTED_DAILY = _incident(KIND_CASES[0], recovered=False)


@pytest.mark.parametrize("base", KIND_CASES)
@pytest.mark.parametrize("recovered", [False, True])
def test_every_kind_renders_within_limits(base, recovered):
    inc = _incident(base, recovered=recovered)
    text = T.admin_telegram(inc, recovered=recovered)
    assert text.strip()
    assert len(text) <= 3500
    for marker in MARKDOWN_MARKERS:
        assert marker not in text, marker
    subject, body = T.admin_email(inc, recovered=recovered)
    assert len(subject) <= 120
    assert body.startswith(text)
    assert body.endswith(T.EMAIL_FOOTER)
    for marker in MARKDOWN_MARKERS:
        assert marker not in subject and marker not in body, marker


def test_router_exhausted_daily_quota_headline_and_action():
    text = T.admin_telegram(ROUTER_EXHAUSTED_DAILY)
    lines = text.splitlines()
    assert lines[0] == "[CRIT] Hermes: all LLM tiers exhausted (daily_quota)"
    assert "wait for the provider's daily reset or add a tier" in text
    assert "Key: router:exhausted" in text
    assert "Scope: router" in text
    assert "Providers: nvidia, openrouter, gemini" in text
    assert "Opened: 2026-10-10T10:00:00Z" in text
    assert "Count: 3" in text


def test_recovered_headline_includes_duration():
    inc = _incident(KIND_CASES[0], recovered=True)
    text = T.admin_telegram(inc, recovered=True)
    assert text.splitlines()[0] == "[RECOVERED] Hermes: all LLM tiers exhausted (daily_quota) after 1h 12m"
    subject, _ = T.admin_email(inc, recovered=True)
    assert subject.startswith("[RECOVERED] Hermes sentinel: ")


def test_recovered_without_parseable_timestamps_omits_duration():
    inc = {"kind": "router_exhausted", "exhaustion_kind": "auth", "opened_at": "not a date", "timestamp": LATER}
    text = T.admin_telegram(inc, recovered=True)
    assert text.splitlines()[0] == "[RECOVERED] Hermes: all LLM tiers exhausted (auth)"


def test_flow_stalled_facts_and_client_line():
    text = T.admin_telegram(_incident(KIND_CASES[9], recovered=False))
    assert "Tariq was owed the next step for container GTM-1234567 after step publish" in text
    assert "Wesam" in text
    assert "Client not notified: no contact on file." in text
    assert "Container: GTM-1234567" in text
    assert "Last step: publish" in text

    sent = T.admin_telegram(_incident(KIND_CASES[10], recovered=False))
    assert "Client holding email sent." in sent
    assert "Client not notified" not in sent


def test_flow_stalled_client_notified_none_says_nothing_about_client():
    inc = _incident(KIND_CASES[9], recovered=False)
    inc["client_notified"] = None
    assert "Client" not in T.admin_telegram(inc)


def test_provider_degraded_action_by_exhaustion():
    inc = _incident(KIND_CASES[7], recovered=False)
    text = T.admin_telegram(inc)
    assert text.splitlines()[0] == "[WARN] Hermes: provider degraded (payment)"
    assert "top up the provider balance" in text
    assert "Providers: openrouter" in text


def test_missing_and_none_fields_never_raise():
    cases = [
        {},
        None,
        "not a dict",
        {"kind": None, "severity": None, "providers": None, "count": None},
        {"kind": "something_new", "exhaustion_kind": "lunar", "severity": 42},
        {"kind": "router_exhausted", "exhaustion_kind": None, "providers": "single-string"},
        {"kind": ["unhashable"], "exhaustion_kind": {"x": 1}, "state": ["resolved"]},
        {"kind": "flow_stalled", "container_id": None, "last_successful_step": None, "client_notified": None},
    ]
    for inc in cases:
        for recovered in (False, True):
            text = T.admin_telegram(inc, recovered=recovered)
            assert isinstance(text, str) and text
            assert len(text) <= 3500
            subject, body = T.admin_email(inc, recovered=recovered)
            assert isinstance(subject, str) and len(subject) <= 120
            assert isinstance(body, str) and body.endswith(T.EMAIL_FOOTER)


def test_unknown_kind_uses_generic_wording():
    text = T.admin_telegram({"kind": "mystery", "severity": "WARN"})
    assert text.splitlines()[0] == "[WARN] Hermes: unrecognized alert"
    assert "does not recognize" in text


def test_unknown_exhaustion_kind_is_not_echoed_as_action():
    text = T.admin_telegram({"kind": "router_exhausted", "exhaustion_kind": "lunar"})
    assert text.splitlines()[0] == "[CRIT] Hermes: all LLM tiers exhausted"
    assert "check the router journal" in text


def test_missing_severity_defaults_by_kind():
    assert T.admin_telegram({"kind": "budget_80"}).startswith("[WARN] Hermes:")
    assert T.admin_telegram({"kind": "router_exhausted"}).startswith("[CRIT] Hermes:")


def test_telegram_stays_within_limit_for_oversized_inputs():
    inc = {
        "kind": "flow_stalled",
        "providers": [f"provider-{i}" for i in range(5000)],
        "last_successful_step": "x" * 10000,
        "incident_key": "k" * 5000,
        "container_id": "GTM-1234567",
        "client_notified": True,
    }
    assert len(T.admin_telegram(inc)) <= 3500


def test_truncation_branch_appends_marker(monkeypatch):
    monkeypatch.setattr(T, "TELEGRAM_LIMIT", 200)
    text = T.admin_telegram({"kind": "flow_stalled", "client_notified": True, "container_id": "GTM-1234567"})
    assert len(text) <= 200
    assert text.endswith("[truncated]")


def test_long_fields_are_clipped_but_short_ones_are_not_altered():
    inc = {"kind": "sentinel_error", "last_successful_step": "a" * 1000}
    text = T.admin_telegram(inc)
    assert "a" * 1000 not in text
    assert "a" * 297 + "..." in text


def test_markdown_and_newlines_in_inputs_are_neutralised():
    inc = {"kind": "flow_stalled", "last_successful_step": "**bold**\n```code```", "container_id": "GTM-7654321"}
    text = T.admin_telegram(inc)
    for marker in MARKDOWN_MARKERS:
        assert marker not in text
    assert "bold" in text and "code" in text


def test_admin_email_subject_capped_at_120():
    inc = {"kind": "router_exhausted", "exhaustion_kind": "daily_quota", "severity": "CRIT"}
    subject, _ = T.admin_email(inc)
    assert subject == "[CRIT] Hermes sentinel: all LLM tiers exhausted (daily_quota)"
    subject, _ = T.admin_email({"kind": "x" * 500, "severity": "CRIT"})
    assert len(subject) <= 120


def test_admin_email_body_is_telegram_text_plus_footer():
    inc = ROUTER_EXHAUSTED_DAILY
    subject, body = T.admin_email(inc)
    assert body == T.admin_telegram(inc) + "\n\n" + (
        "Sent automatically by hermes-sentinel. No LLM was used to write this message."
    )


def test_arabic_holding_text_is_verbatim():
    assert T.ARABIC_HOLDING_TEXT == ARABIC_HOLDING
    for store in (None, "متجر تجريبي"):
        subject, body = T.client_holding_email("ar", store)
        assert ARABIC_HOLDING in body
        assert subject == "تحديث بخصوص فحص التتبع لمتجركم"
        assert "فريق مُتاح AI" in body


def test_arabic_holding_greeting_uses_store_name():
    _, body = T.client_holding_email("ar", "متجر تجريبي")
    assert body.startswith("فريق متجر متجر تجريبي،")
    _, neutral = T.client_holding_email("ar")
    assert neutral.startswith("مرحباً،")


def test_english_holding_is_modest_and_english():
    subject, body = T.client_holding_email("en", "Demo Store")
    assert subject == "Update on your store's tracking check"
    assert body.startswith("Hello Demo Store team,")
    assert ARABIC_HOLDING not in body
    lowered = body.lower()
    assert "engineer" not in lowered
    assert "minutes" not in lowered
    assert "within" not in lowered


def test_unknown_or_missing_lang_falls_back_to_arabic():
    for lang in ("fr", "", None, "EN-us", 5):
        subject, body = T.client_holding_email(lang)
        assert ARABIC_HOLDING in body, lang
        subject, body = T.client_followup_email(lang)
        assert "استأنفنا" in body, lang


def test_english_lang_is_case_insensitive():
    _, body = T.client_holding_email(" EN ")
    assert "We have received your request" in body


def test_followup_templates():
    subject, body = T.client_followup_email("ar")
    assert subject == "تحديث: استئناف فحص التتبع لمتجركم"
    assert "استأنفنا معالجة طلبكم" in body
    subject, body = T.client_followup_email("en", "Demo Store")
    assert subject == "Update: your tracking check has resumed"
    assert "Processing of your request has resumed" in body
    assert body.startswith("Hello Demo Store team,")


def test_client_templates_contain_no_contact_or_url_or_long_digits():
    outputs = []
    for store in (None, "Demo Store"):
        for lang in ("ar", "en"):
            outputs.append(T.client_holding_email(lang, store))
            outputs.append(T.client_followup_email(lang, store))
    for subject, body in outputs:
        for text in (subject, body):
            assert not CLIENT_FORBIDDEN.search(text), text


def test_client_store_name_is_the_only_free_text_passed_through():
    _, body = T.client_holding_email("en", "Acme 12345678")
    assert "Acme 12345678" in body
    _, body = T.client_holding_email("en", None)
    assert not re.search(r"\d{8,}", body)


def test_client_store_name_markdown_is_stripped():
    _, body = T.client_holding_email("en", "**Acme**")
    assert "**" not in body and "Acme" in body


def test_determinism():
    for base in KIND_CASES:
        for recovered in (False, True):
            inc = _incident(base, recovered=recovered)
            assert T.admin_telegram(inc, recovered) == T.admin_telegram(dict(inc), recovered)
            assert T.admin_email(inc, recovered) == T.admin_email(dict(inc), recovered)
    assert T.client_holding_email("ar", "S") == T.client_holding_email("ar", "S")
    assert T.client_followup_email("en") == T.client_followup_email("en")


def test_no_contact_or_url_in_admin_facts_from_templates_themselves():
    for base in KIND_CASES:
        inc = _incident(base, recovered=False)
        for key in ("container_id", "last_successful_step"):
            inc.pop(key, None)
        text = T.admin_telegram(inc)
        assert "http" not in text
        assert "@" not in text
