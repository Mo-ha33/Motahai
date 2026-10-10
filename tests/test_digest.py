"""
test_digest.py — S1-4 Sunday Signal Hygiene digest.

Hand-built dataset, tenant timezone Africa/Cairo (UTC+3 on the send date, DST). Send date = Sunday 2026-10-11:
  report week     = 2026-10-04 00:00 .. 2026-10-10 23:59:59 Cairo   (W* orders)
  matured cohort  = 2026-09-27 00:00 .. 2026-10-03 23:59:59 Cairo   (C* orders)
Expected values below are derived by hand from the dataset (arithmetic shown), not from the code under test.
"""

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import scheduler as sched_mod
from src.ameen_workforce.db import (
    CapiEvent, Digest, Incident, JobRun, Order, OrderStatusEvent, create_tenant, init_db, make_engine
)
from src.ameen_workforce.digest import (
    LogOnlySender, build_digest, digest_periods, digest_subject, q_creatives, render_digest_html,
    render_digest_text, send_weekly_digests, sender_from_env
)

CAIRO = ZoneInfo("Africa/Cairo")
SEND_DATE = date(2026, 10, 11)  # a Sunday
REFUSED_PHRASE_EN = "Refused COD value kept out of the delivered signal"
REFUSED_PHRASE_AR = "قيمة طلبات الدفع عند الاستلام المرفوضة التي استُبعدت من إشارة التسليم"
EMAIL_HASH = "e" * 64
PHONE_HASH = "f" * 64


def cairo(month, day, hour=12, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=CAIRO).astimezone(timezone.utc)


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()
    engine.dispose()


_counter = {"n": 0}


def make_order(session, tenant, placed, *, cod=True, value=100.0, status="pending", confirmed=False,
               order_total=None, ad_id=None, fbc=None, utm_source=None, shipped=False, platform_time=True):
    _counter["n"] += 1
    order = Order(
        tenant_id=tenant.id, platform_order_id=f"o{_counter['n']}", is_cod=cod, value=value, currency=tenant.currency,
        current_status=status, order_total=order_total, ad_id=ad_id, fbc=fbc, utm_source=utm_source,
        created_at_platform=placed if platform_time else None, created_at=placed,
        confirmed_at=placed if confirmed else None, email_hash=EMAIL_HASH, phone_hash=PHONE_HASH,
    )
    session.add(order)
    session.flush()
    if shipped:
        session.add(OrderStatusEvent(order_id=order.id, status="shipped", received_at=placed))
    session.commit()
    return order


def make_event(session, tenant, order, name, status, when, flags=None):
    session.add(CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=name, event_id=f"{name}_{order.id}",
                          status=status, created_at=when, sent_at=when if status == "sent" else None,
                          quality_flags=flags))
    session.commit()


@pytest.fixture
def tenant(session):
    return create_tenant(session, name="Acme Store", platform="shopify", shop_domain="acme.myshopify.com",
                         mode="live", digest_email="owner@acme.example")


@pytest.fixture
def other_tenant(session):
    return create_tenant(session, name="Other Store", platform="salla", shop_domain="777", mode="live")


@pytest.fixture
def dataset(session, tenant, other_tenant):
    """Tenant A hand-built data plus noise belonging to tenant B (must never leak into A's numbers)."""
    o = {}
    # --- report week (2026-10-04 .. 2026-10-10 Cairo) ---
    o["W1"] = make_order(session, tenant, cairo(10, 4, 0, 30), cod=True, value=100, confirmed=True)
    o["W2"] = make_order(session, tenant, cairo(10, 6), cod=False, value=200, confirmed=True)
    o["W3"] = make_order(session, tenant, cairo(10, 8, 10), cod=True, value=300)
    o["W4"] = make_order(session, tenant, cairo(10, 10, 23, 59), cod=True, value=400)  # last minute of the week
    o["W5"] = make_order(session, tenant, cairo(10, 5), cod=False, value=50, platform_time=False)  # created_at only
    # just outside the report week on both sides (the first one belongs to the cohort, the second is next week)
    make_order(session, tenant, cairo(10, 11, 0, 30), cod=True, value=999, confirmed=True)
    # --- matured cohort (2026-09-27 .. 2026-10-03 Cairo) ---
    o["C1"] = make_order(session, tenant, cairo(9, 27, 0, 5), cod=True, value=100, order_total=100,
                         status="delivered", ad_id="ad1")
    o["C2"] = make_order(session, tenant, cairo(9, 29), cod=True, value=200, order_total=200,
                         status="shipped", fbc="fb.1.123.abc", shipped=True)
    o["C3"] = make_order(session, tenant, cairo(9, 30), cod=False, value=300, order_total=300,
                         status="paid", utm_source="facebook")
    o["C4"] = make_order(session, tenant, cairo(10, 1), cod=True, value=0, order_total=150,
                         status="cancelled", ad_id="ad1", shipped=True)             # refused after dispatch
    o["C5"] = make_order(session, tenant, cairo(10, 1, 9), cod=True, value=0, order_total=250,
                         status="cancelled")                                         # cancelled BEFORE dispatch
    o["C6"] = make_order(session, tenant, cairo(10, 2), cod=True, value=120, order_total=120,
                         status="shipped", shipped=True)                             # in transit; failed event row
    o["C7"] = make_order(session, tenant, cairo(10, 2, 15), cod=True, value=0, order_total=80,
                         status="refunded", ad_id="ad2", shipped=True)               # refused after dispatch
    o["C8"] = make_order(session, tenant, cairo(10, 3, 23, 59), cod=False, value=450, order_total=500,
                         status="delivered")                                         # prepaid, partial refund
    o["C10"] = make_order(session, tenant, cairo(10, 3), cod=False, value=0, order_total=90,
                          status="cancelled", shipped=True)                          # prepaid: not a COD refusal
    # outside the cohort: a day too old, and delivered orders from the report week
    make_order(session, tenant, cairo(9, 26, 23, 59), cod=True, value=7777, order_total=7777, status="delivered")
    # --- events ---
    make_event(session, tenant, o["W1"], "ConfirmedOrder", "sent", cairo(10, 4, 1), flags="missing_user_agent")
    make_event(session, tenant, o["W2"], "ConfirmedOrder", "sent", cairo(10, 6, 1))
    make_event(session, tenant, o["W3"], "ConfirmedOrder", "shadow", cairo(10, 8, 11))
    make_event(session, tenant, o["W4"], "ConfirmedOrder", "failed", cairo(10, 10, 23, 59))   # not counted
    make_event(session, tenant, o["W5"], "ConfirmedOrder", "sent", cairo(10, 3, 23, 0))         # previous week
    make_event(session, tenant, o["C2"], "DeliveredPurchase", "sent", cairo(10, 6, 12), flags="missing_event_source_url")
    make_event(session, tenant, o["C3"], "DeliveredPurchase", "late_delivery", cairo(10, 7))
    make_event(session, tenant, o["C1"], "DeliveredPurchase", "late_delivery", cairo(9, 30))   # previous week
    make_event(session, tenant, o["C6"], "DeliveredPurchase", "failed", cairo(10, 5))           # not delivered
    # --- incidents ---
    session.add_all([
        Incident(tenant_id=tenant.id, kind="capi_failures", severity="high", detected_at=cairo(9, 1)),
        Incident(tenant_id=tenant.id, kind="webhook_gap", severity="low", detected_at=cairo(10, 7),
                 resolved_at=cairo(10, 8)),
        Incident(tenant_id=tenant.id, kind="token_expired", severity="high", detected_at=cairo(10, 9)),
        Incident(tenant_id=tenant.id, kind="old_resolved", severity="low", detected_at=cairo(8, 1),
                 resolved_at=cairo(8, 2)),
        # 21:30 UTC on Oct 10 is 00:30 Oct 11 in Cairo: next week for this tenant
        Incident(tenant_id=tenant.id, kind="after_midnight", severity="low",
                 detected_at=datetime(2026, 10, 10, 21, 30, tzinfo=timezone.utc), resolved_at=cairo(10, 11, 5)),
        Incident(tenant_id=other_tenant.id, kind="other_tenant_incident", severity="high", detected_at=cairo(10, 8)),
    ])
    session.commit()
    # --- tenant B noise: same windows, big numbers ---
    nb = make_order(session, other_tenant, cairo(10, 6), cod=True, value=5000, confirmed=True, ad_id="adB")
    cb = make_order(session, other_tenant, cairo(9, 30), cod=True, value=9000, order_total=9000, status="delivered",
                    ad_id="adB", shipped=True)
    make_order(session, other_tenant, cairo(10, 1), cod=True, value=0, order_total=4000, status="cancelled", shipped=True)
    make_event(session, other_tenant, nb, "ConfirmedOrder", "sent", cairo(10, 6), flags="x")
    make_event(session, other_tenant, cb, "DeliveredPurchase", "late_delivery", cairo(10, 7))
    return o


# --- periods ---------------------------------------------------------------------------------------------------------

def test_periods_follow_tenant_timezone_and_dates():
    p = digest_periods(SEND_DATE, "Africa/Cairo")
    assert (p.week_start, p.week_end) == (date(2026, 10, 4), date(2026, 10, 10))
    assert (p.cohort_start, p.cohort_end) == (date(2026, 9, 27), date(2026, 10, 3))
    assert p.week == (cairo(10, 4, 0, 0), cairo(10, 11, 0, 0))
    assert p.cohort == (cairo(9, 27, 0, 0), cairo(10, 4, 0, 0))


# --- metrics ---------------------------------------------------------------------------------------------------------

def test_week_orders_cod_share_and_confirmation_rate(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    # W1..W5 placed in the week (the 00:30 Oct 11 order is excluded); COD = W1, W3, W4; confirmed = W1, W2
    assert d.orders_placed == 5
    assert d.cod_orders == 3 and d.cod_share == pytest.approx(3 / 5)
    assert d.confirmed_orders == 2 and d.confirmation_rate == pytest.approx(2 / 5)


def test_matured_cohort_delivered_rates(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    # cohort = C1..C8 + C10 (9 orders); delivered = C1 (status), C2 (sent event), C3 (late_delivery event), C8 (status)
    # C6 has only a failed event and is NOT delivered; C4/C5/C7/C10 are cancelled/refunded
    assert d.cohort_orders == 9
    assert d.cohort_delivered == 4 and d.delivered_rate == pytest.approx(4 / 9)
    # ad-driven (ad_id, fbc or utm_source): C1, C2, C3, C4, C7 = 5, delivered among them = C1, C2, C3
    assert d.cohort_ad_orders == 5
    assert d.cohort_ad_delivered == 3 and d.ad_delivered_rate == pytest.approx(3 / 5)


def test_refused_cod_value_uses_audiences_definition(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    # C4 (150) and C7 (80): COD, shipped event, then cancelled/refunded. C5 never shipped; C10 is prepaid.
    assert d.refused_count == 2
    assert d.refused_value == pytest.approx(230.0)


def test_vanity_roas_inflation(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    # gross value of all 9 cohort orders = 100+200+300+150+250+120+80+500+90 = 1790
    # net delivered value = C1 100 + C2 200 + C3 300 + C8 450 (net of partial refund) = 1050
    assert d.reported_value == pytest.approx(1790.0)
    assert d.delivered_value == pytest.approx(1050.0)
    assert d.vanity_ratio == pytest.approx(1790 / 1050)
    html = render_digest_html(d, "en")
    assert "1.7×" in html


def test_signal_health_counts(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    # in the week: ConfirmedOrder sent W1 (flagged), W2; shadow W3; (W4 failed and W5 previous week are excluded)
    assert (d.confirmed_events_sent, d.confirmed_events_shadow) == (2, 1)
    # DeliveredPurchase sent C2 (flagged); C6 failed excluded; late_delivery: C3 in the week, C1 previous week
    assert (d.delivered_events_sent, d.delivered_events_shadow) == (1, 0)
    assert d.signal_events_total == 4 and d.signal_events_flagged == 2
    assert d.flagged_share == pytest.approx(0.5)
    assert d.late_delivery_count == 1


def test_incidents_unresolved_and_week(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    assert [(i.kind, i.on) for i in d.unresolved_incidents] == [
        ("capi_failures", date(2026, 9, 1)), ("token_expired", date(2026, 10, 9))]
    assert [(i.kind, i.on) for i in d.week_incidents] == [
        ("webhook_gap", date(2026, 10, 7)), ("token_expired", date(2026, 10, 9))]
    text = render_digest_text(d, "en")
    assert "capi_failures" in text and "webhook_gap" in text
    assert "other_tenant_incident" not in text and "after_midnight" not in text and "old_resolved" not in text


def test_no_incidents_line(session, tenant):
    assert "No incidents" in render_digest_text(build_digest(session, tenant.id, SEND_DATE), "en")
    assert "لا توجد حوادث" in render_digest_text(build_digest(session, tenant.id, SEND_DATE), "ar")


# --- isolation / empty ---------------------------------------------------------------------------------------------------

def test_tenant_b_numbers_do_not_leak_into_a_and_vice_versa(session, tenant, other_tenant, dataset):
    a = build_digest(session, tenant.id, SEND_DATE)
    b = build_digest(session, other_tenant.id, SEND_DATE)
    assert a.orders_placed == 5 and b.orders_placed == 1
    assert (b.cohort_orders, b.cohort_delivered) == (2, 1)
    assert b.reported_value == pytest.approx(13000.0) and b.delivered_value == pytest.approx(9000.0)
    assert (b.refused_count, b.refused_value) == (1, pytest.approx(4000.0))
    assert (b.confirmed_events_sent, b.signal_events_flagged, b.late_delivery_count) == (1, 1, 1)
    assert [i.kind for i in b.unresolved_incidents] == ["other_tenant_incident"]
    assert all("adB" != c.ad_id for c in a.creatives_top)


def test_empty_tenant_renders_no_data_yet_not_zeros(session, tenant):
    d = build_digest(session, tenant.id, SEND_DATE)
    assert d.cod_share is None and d.confirmation_rate is None and d.delivered_rate is None
    assert d.vanity_ratio is None and d.flagged_share is None
    en = render_digest_text(d, "en")
    assert en.count("no data yet") >= 5
    assert "0.0%" not in en and "0.00" not in en and "0 (" not in en
    ar = render_digest_text(d, "ar")
    assert ar.count("لا توجد بيانات بعد") >= 5
    assert "0.0%" not in ar and "0.00" not in ar
    assert "Creative scorecard" not in en


def test_vanity_ratio_is_na_when_nothing_delivered(session, tenant):
    make_order(session, tenant, cairo(9, 30), cod=True, value=100, order_total=100, status="shipped", shipped=True)
    d = build_digest(session, tenant.id, SEND_DATE)
    assert d.cohort_orders == 1 and d.delivered_value == 0 and d.vanity_ratio is None
    en = render_digest_text(d, "en")
    assert "Inflation factor: n/a" in en and "×" not in en
    assert "غير متاح" in render_digest_text(d, "ar")


# --- creatives -----------------------------------------------------------------------------------------------------------

def test_creative_scorecard_threshold_and_ranking(session, tenant):
    # ad k (k=1..7): 10 cohort orders, k delivered at 10 each -> delivered value k*10
    for k in range(1, 8):
        for i in range(10):
            delivered = i < k
            make_order(session, tenant, cairo(9, 28), cod=True, value=10 if delivered else 0, order_total=10,
                       status="delivered" if delivered else "cancelled", ad_id=f"ad{k}")
    for i in range(9):  # below the threshold: never shown, even if it would rank first
        make_order(session, tenant, cairo(9, 28), cod=True, value=1000, status="delivered", ad_id="small")
    d = build_digest(session, tenant.id, SEND_DATE)
    assert [c.ad_id for c in d.creatives_top] == ["ad7", "ad6", "ad5", "ad4", "ad3"]
    assert [c.ad_id for c in d.creatives_bottom] == ["ad1", "ad2"]  # worst first, no overlap with the top five
    top = d.creatives_top[0]
    assert (top.orders, top.delivered, top.delivered_value) == (10, 7, 70.0)
    assert top.delivery_rate == pytest.approx(0.7)
    html = render_digest_html(d, "en")
    assert "ad7" in html and "small" not in html and "Creative scorecard" in html


def test_creative_section_omitted_below_threshold(session, tenant):
    for i in range(9):
        make_order(session, tenant, cairo(9, 28), value=10, status="delivered", ad_id="adX")
    d = build_digest(session, tenant.id, SEND_DATE)
    assert q_creatives(session, tenant.id, d.periods.cohort) == []
    assert d.creatives_top == [] and d.creatives_bottom == []
    for lang in ("en", "ar"):
        assert "adX" not in render_digest_text(d, lang)
    assert "Creative scorecard" not in render_digest_text(d, "en")


# --- rendering -------------------------------------------------------------------------------------------------------------

FORBIDDEN = ("saved", "saving", "وفّر", "وفر", "توفير")


def test_refused_phrase_exact_and_no_savings_claims(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    for fn in (render_digest_html, render_digest_text):
        en, ar = fn(d, "en"), fn(d, "ar")
        assert REFUSED_PHRASE_EN in en
        assert REFUSED_PHRASE_AR in ar
        for text in (en, ar):
            lowered = text.lower()
            assert not any(word in lowered for word in FORBIDDEN), text
    assert "230.00 EGP" in render_digest_text(d, "en")


def test_arabic_is_default_and_rtl_and_email_safe(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    html = render_digest_html(d)  # default language
    assert 'dir="rtl"' in html and 'lang="ar"' in html
    assert 'dir="ltr"' in render_digest_html(d, "en")
    for markup in (html, render_digest_html(d, "en")):
        assert "<table" in markup and "style=" in markup
        assert "<img" not in markup and "<script" not in markup and "<link" not in markup
        assert "http://" not in markup and "https://" not in markup and "url(" not in markup
    assert "ملخص" in render_digest_text(d)  # text default is Arabic too
    assert "1.7×" in html and "ads_read" in html


def test_shadow_tenant_wording(session, tenant, dataset):
    shadow = create_tenant(session, name="Shadow Shop", platform="shopify", shop_domain="shadow.myshopify.com",
                           mode="shadow")
    o = make_order(session, shadow, cairo(10, 6), cod=True, value=10, confirmed=True)
    make_event(session, shadow, o, "ConfirmedOrder", "shadow", cairo(10, 6))
    d = build_digest(session, shadow.id, SEND_DATE)
    en, ar = render_digest_text(d, "en"), render_digest_text(d, "ar")
    assert "ConfirmedOrder events that would have been sent: 1" in en
    assert "DeliveredPurchase events that would have been sent: 0" in en
    assert "events sent" not in en
    assert "Shadow mode" in en and "كان سيتم إرسالها" in ar
    # a live tenant uses the plain "sent" wording
    live = render_digest_text(build_digest(session, tenant.id, SEND_DATE), "en")
    assert "ConfirmedOrder events sent: 2" in live and "would have been sent: 1" in live  # its one shadow row too


def test_no_pii_in_output(session, tenant, dataset):
    d = build_digest(session, tenant.id, SEND_DATE)
    for lang in ("en", "ar"):
        for out in (render_digest_html(d, lang), render_digest_text(d, lang), digest_subject(d, lang)):
            assert "@" not in out and "owner" not in out
            assert EMAIL_HASH not in out and PHONE_HASH not in out
            assert not re.search(r"\b\d{10,}\b", out)  # no phone-like numbers
    assert "o1" not in render_digest_text(d, "en").split()  # platform order ids are never shown


# --- sending -------------------------------------------------------------------------------------------------------------

SUNDAY_LOCAL_DUE = datetime(2026, 10, 11, 8, 30, tzinfo=timezone.utc)  # Cairo/Dubai: 11:30 / 12:30; New York: 04:30


class RecordingSender:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = fail_for

    def send(self, *, to, subject, html, text):
        if to in self.fail_for:
            raise RuntimeError(f"smtp 550 rejected {to}")
        self.calls.append({"to": to, "subject": subject, "html": html, "text": text})


@pytest.fixture
def fleet(session):
    cairo_t = create_tenant(session, name="Cairo", platform="shopify", shop_domain="c.myshopify.com",
                            timezone="Africa/Cairo", digest_email="cairo@x.example")
    ny = create_tenant(session, name="NY", platform="shopify", shop_domain="n.myshopify.com",
                       timezone="America/New_York", digest_email="ny@x.example")
    dubai = create_tenant(session, name="Dubai", platform="shopify", shop_domain="d.myshopify.com",
                          timezone="Asia/Dubai", digest_email="dubai@x.example", language="en")
    no_email = create_tenant(session, name="NoEmail", platform="shopify", shop_domain="e.myshopify.com")
    inactive = create_tenant(session, name="Off", platform="shopify", shop_domain="f.myshopify.com",
                             digest_email="off@x.example", active=False)
    return {"cairo": cairo_t, "ny": ny, "dubai": dubai, "no_email": no_email, "inactive": inactive}


def test_sends_once_per_tenant_per_week_respecting_timezone(session, fleet):
    sender = RecordingSender()
    counts = send_weekly_digests(session, SUNDAY_LOCAL_DUE, sender)
    assert sorted(c["to"] for c in sender.calls) == ["cairo@x.example", "dubai@x.example"]  # NY is 04:30; others skipped
    assert counts["sent"] == 2 and counts["not_due"] == 1 and counts["no_recipient"] == 1
    rows = session.scalars(select(Digest).order_by(Digest.tenant_id)).all()
    assert [(r.tenant_id, r.week_start, r.channel) for r in rows] == [
        (fleet["cairo"].id, date(2026, 10, 4), "email"), (fleet["dubai"].id, date(2026, 10, 4), "email")]
    assert all(r.sent_at == SUNDAY_LOCAL_DUE for r in rows)
    by_to = {c["to"]: c for c in sender.calls}
    assert 'lang="ar"' in by_to["cairo@x.example"]["html"]       # default language
    assert 'lang="en"' in by_to["dubai@x.example"]["html"]       # tenant language

    again = send_weekly_digests(session, SUNDAY_LOCAL_DUE + timedelta(minutes=15), sender)
    assert len(sender.calls) == 2 and again["sent"] == 0 and again["already_sent"] == 2

    # New York becomes due later in the day and is sent exactly once
    later = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)  # 10:00 in New York (UTC-4)
    send_weekly_digests(session, later, sender)
    send_weekly_digests(session, later + timedelta(hours=1), sender)
    assert sorted(c["to"] for c in sender.calls) == ["cairo@x.example", "dubai@x.example", "ny@x.example"]


def test_sunday_ten_am_local_boundary_and_other_days(session, fleet):
    sender = RecordingSender()
    only = fleet["cairo"]
    session.query(type(only)).filter(type(only).id != only.id).update({"digest_email": None})
    session.commit()
    # Cairo is UTC+3 on these dates: 06:59 UTC = 09:59 local (not yet), 07:00 UTC = 10:00 local
    assert send_weekly_digests(session, datetime(2026, 10, 11, 6, 59, tzinfo=timezone.utc), sender)["sent"] == 0
    assert send_weekly_digests(session, datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc), sender)["sent"] == 0  # Sat/Sun
    assert send_weekly_digests(session, datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc), sender)["sent"] == 0    # Monday
    assert not sender.calls
    assert send_weekly_digests(session, datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc), sender)["sent"] == 1
    # the following Sunday is a new report week
    assert send_weekly_digests(session, datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc), sender)["sent"] == 1
    assert [r.week_start for r in session.scalars(select(Digest).order_by(Digest.week_start))] == [
        date(2026, 10, 4), date(2026, 10, 11)]


def test_sender_error_records_incident_and_other_tenants_still_sent(session, fleet):
    sender = RecordingSender(fail_for=("cairo@x.example",))
    counts = send_weekly_digests(session, SUNDAY_LOCAL_DUE, sender)
    assert counts["failed"] == 1 and counts["sent"] == 1
    assert [c["to"] for c in sender.calls] == ["dubai@x.example"]
    incidents = session.scalars(select(Incident).where(Incident.kind == "digest_failed")).all()
    assert len(incidents) == 1 and incidents[0].tenant_id == fleet["cairo"].id
    assert incidents[0].detail == "week_start=2026-10-04: RuntimeError"
    assert "x.example" not in incidents[0].detail and "550" not in incidents[0].detail  # no recipient / SMTP text
    assert session.scalar(select(Digest).where(Digest.tenant_id == fleet["cairo"].id)) is None
    # retried on the next tick (no digests row) without piling up incidents
    send_weekly_digests(session, SUNDAY_LOCAL_DUE + timedelta(minutes=15), sender)
    assert len(session.scalars(select(Incident).where(Incident.kind == "digest_failed")).all()) == 1
    # once the sender recovers the digest goes out
    recovered = RecordingSender()
    assert send_weekly_digests(session, SUNDAY_LOCAL_DUE + timedelta(minutes=30), recovered)["sent"] == 1
    assert [c["to"] for c in recovered.calls] == ["cairo@x.example"]


def test_log_only_sender_records_without_content(session, fleet, caplog):
    sender = LogOnlySender()
    send_weekly_digests(session, SUNDAY_LOCAL_DUE, sender)
    assert {s["to"] for s in sender.sent} == {"cairo@x.example", "dubai@x.example"}
    assert all(set(s) == {"to", "subject"} for s in sender.sent)


def test_sender_from_env(monkeypatch):
    monkeypatch.delenv("MOTAHAI_SMTP_HOST", raising=False)
    monkeypatch.delenv("MOTAHAI_SMTP_FROM", raising=False)
    assert sender_from_env() is None
    monkeypatch.setenv("MOTAHAI_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MOTAHAI_SMTP_FROM", "digest@example.com")
    assert sender_from_env() is not None


# --- scheduler job ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def memory_db():
    engine = make_engine("sqlite://")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_tick_runs_digest_job(memory_db, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "src.ameen_workforce.capture", None)
    with memory_db() as s:
        create_tenant(s, name="Cairo", platform="shopify", shop_domain="c.myshopify.com", digest_email="c@x.example")
    sender = LogOnlySender()
    summary = await sched_mod.run_scheduler_tick(session_factory=memory_db, now=SUNDAY_LOCAL_DUE, digest_sender=sender)
    job = summary["jobs"]["send_weekly_digests"]
    assert job["ok"] and job["counts"]["sent"] == 1 and len(sender.sent) == 1
    summary = await sched_mod.run_scheduler_tick(session_factory=memory_db, now=SUNDAY_LOCAL_DUE + timedelta(minutes=15),
                                                 digest_sender=sender)
    assert summary["jobs"]["send_weekly_digests"]["counts"]["already_sent"] == 1 and len(sender.sent) == 1
    with memory_db() as s:
        runs = s.scalars(select(JobRun).where(JobRun.job_name == "send_weekly_digests")).all()
        assert len(runs) == 2 and all(r.ok for r in runs)
        assert s.scalar(select(Digest.id)) is not None


@pytest.mark.asyncio
async def test_scheduler_digest_job_is_skipped_without_smtp(memory_db, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "src.ameen_workforce.capture", None)
    monkeypatch.delenv("MOTAHAI_SMTP_HOST", raising=False)
    monkeypatch.delenv("MOTAHAI_SMTP_FROM", raising=False)
    with memory_db() as s:
        create_tenant(s, name="Cairo", platform="shopify", shop_domain="c.myshopify.com", digest_email="c@x.example")
    summary = await sched_mod.run_scheduler_tick(session_factory=memory_db, now=SUNDAY_LOCAL_DUE)
    assert summary["jobs"]["send_weekly_digests"]["counts"] == {"skipped": "digest_sender_not_configured"}
    with memory_db() as s:
        assert s.scalar(select(Digest.id)) is None  # nothing marked as sent


def test_create_tenant_validates_language(session):
    with pytest.raises(ValueError):
        create_tenant(session, name="X", platform="shopify", shop_domain="x.myshopify.com", language="fr")
