"""
digest.py — Sunday Signal Hygiene digest (S1-4).

One email per merchant per week with the numbers that show how clean the conversion signal is. Rules:
- EVERY number comes from a named SQL query function below (q_*) over real tables, scoped by tenant_id. There are no
  canned, simulated or default numbers. Missing data renders as "no data yet", never as a zero stated as fact.
- No PII: aggregates and ad ids only (the DB holds no raw customer data anyway; the digest never reads hashes).
- Never claims money or budget was "saved". The refused-COD line says exactly what the data shows.
- Shadow tenants: sends are phrased "would have been sent" (nothing reaches Meta in shadow mode).

Periods (tenant timezone; send_date = the Sunday the digest goes out):
  report week     [send_date - 7d, send_date)        Sunday 00:00 .. Saturday 23:59:59 before the send
  matured cohort  [send_date - 14d, send_date - 7d)  orders placed 8-14 days before the send date, so they have had at
                                                     least 7 days to reach delivery
  "placed" = coalesce(orders.created_at_platform, orders.created_at) (same convention as audiences.py).

Value conventions: orders.value is the NET collected value (total minus successful refunds, S2-4). Where a number must
mean "what was ordered / what the native Purchase reported" (vanity ROAS, refused COD value) we use the gross
coalesce(order_total, value); "delivered value" is the net orders.value.
"""

import html
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audiences import DELIVERED_STATUS, REFUSAL_STATUSES, SHIPPED_STATUSES
from .capi_service import CONFIRMED_EVENT_NAME, DELIVERED_EVENT_NAME
from .db import CapiEvent, Digest, Incident, Order, OrderStatusEvent, Tenant, utcnow

logger = logging.getLogger("ameen_workforce.digest")

DIGEST_HOUR_LOCAL = 10          # earliest local hour on Sunday the digest is sent
SUNDAY = 6                      # date.weekday()
DIGEST_CHANNEL = "email"
DIGEST_FAILED_KIND = "digest_failed"
LANGUAGES = ("ar", "en")
DEFAULT_LANGUAGE = "ar"
MIN_CREATIVE_ORDERS = 10        # an ad needs at least this many matured-cohort orders to be scored
CREATIVE_LIST_SIZE = 5
MAX_INCIDENT_LINES = 10
# A DeliveredPurchase CapiEvent in one of these statuses proves the order reached delivered (late_delivery = it did,
# but too late for Meta's 7-day window, so it was never sent).
DELIVERED_EVENT_STATUSES = ("sent", "shadow", "scheduled", "late_delivery")
SIGNAL_EVENT_STATUSES = ("sent", "shadow")
SIGNAL_EVENT_NAMES = (CONFIRMED_EVENT_NAME, DELIVERED_EVENT_NAME)

PLACED_AT = func.coalesce(Order.created_at_platform, Order.created_at)
GROSS_VALUE = func.coalesce(Order.order_total, Order.value)


# --- Periods ---------------------------------------------------------------------------------------------

def _tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


@dataclass(frozen=True)
class DigestPeriods:
    """UTC half-open windows [start, end) derived from tenant-local dates."""
    send_date: date
    week_start: date              # first day of the report week (Sunday)
    week_end: date                # last day of the report week (Saturday)
    cohort_start: date            # first day of the matured cohort
    cohort_end: date              # last day of the matured cohort
    week: Tuple[datetime, datetime]
    cohort: Tuple[datetime, datetime]


def digest_periods(send_date: date, tz_name: str) -> DigestPeriods:
    tz = _tz(tz_name)
    week_start = send_date - timedelta(days=7)
    cohort_start = send_date - timedelta(days=14)
    return DigestPeriods(
        send_date=send_date, week_start=week_start, week_end=send_date - timedelta(days=1),
        cohort_start=cohort_start, cohort_end=send_date - timedelta(days=8),
        week=(_local_midnight_utc(week_start, tz), _local_midnight_utc(send_date, tz)),
        cohort=(_local_midnight_utc(cohort_start, tz), _local_midnight_utc(week_start, tz)),
    )


# --- Query functions (each returns plain numbers; all tenant-scoped) ----------------------------------------

def _count(session: Session, stmt) -> int:
    return int(session.scalar(stmt) or 0)


def _delivered_clause(tenant_id: int):
    """An order counts as delivered: current_status delivered, OR a DeliveredPurchase CapiEvent proves it."""
    event_order_ids = select(CapiEvent.order_id).where(
        CapiEvent.tenant_id == tenant_id,
        CapiEvent.event_name == DELIVERED_EVENT_NAME,
        CapiEvent.status.in_(DELIVERED_EVENT_STATUSES),
    )
    return or_(Order.current_status == DELIVERED_STATUS, Order.id.in_(event_order_ids))


def _present(column):
    return and_(column.isnot(None), column != "")


def _ad_driven_clause():
    return or_(_present(Order.fbc), _present(Order.ad_id), _present(Order.utm_source))


def q_week_orders(session: Session, tenant_id: int, window: Tuple[datetime, datetime]) -> Dict[str, int]:
    """Orders placed in the report week: total, COD, and confirmed (confirmed_at set as of now)."""
    row = session.execute(
        select(
            func.count(Order.id),
            func.sum(case((Order.is_cod.is_(True), 1), else_=0)),
            func.sum(case((Order.confirmed_at.isnot(None), 1), else_=0)),
        ).where(Order.tenant_id == tenant_id, PLACED_AT >= window[0], PLACED_AT < window[1])
    ).one()
    return {"placed": int(row[0] or 0), "cod": int(row[1] or 0), "confirmed": int(row[2] or 0)}


def q_cohort_delivery(session: Session, tenant_id: int, window: Tuple[datetime, datetime],
                      ad_only: bool = False) -> Dict[str, float]:
    """
    Matured cohort: orders, how many reached delivered, net delivered value (sum orders.value over delivered), and the
    gross ordered value (sum coalesce(order_total, value) over ALL cohort orders = what native Purchase reported).
    ad_only restricts to ad-driven orders (fbc or ad_id or utm_source present).
    """
    delivered = _delivered_clause(tenant_id)
    stmt = select(
        func.count(Order.id),
        func.sum(case((delivered, 1), else_=0)),
        func.sum(case((delivered, Order.value), else_=0.0)),
        func.sum(GROSS_VALUE),
    ).where(Order.tenant_id == tenant_id, PLACED_AT >= window[0], PLACED_AT < window[1])
    if ad_only:
        stmt = stmt.where(_ad_driven_clause())
    row = session.execute(stmt).one()
    return {
        "orders": int(row[0] or 0), "delivered": int(row[1] or 0),
        "delivered_value": float(row[2] or 0.0), "gross_value": float(row[3] or 0.0),
    }


def q_refused_cod(session: Session, tenant_id: int, window: Tuple[datetime, datetime]) -> Dict[str, float]:
    """
    COD orders in the matured cohort refused after dispatch: same definition as audiences.py (a shipped status event in
    the history, then current_status cancelled/refunded). value = sum of gross coalesce(order_total, value).
    """
    shipped_order_ids = select(OrderStatusEvent.order_id).where(OrderStatusEvent.status.in_(SHIPPED_STATUSES))
    row = session.execute(
        select(func.count(Order.id), func.sum(GROSS_VALUE)).where(
            Order.tenant_id == tenant_id,
            Order.is_cod.is_(True),
            Order.current_status.in_(REFUSAL_STATUSES),
            Order.id.in_(shipped_order_ids),
            PLACED_AT >= window[0], PLACED_AT < window[1],
        )
    ).one()
    return {"count": int(row[0] or 0), "value": float(row[1] or 0.0)}


def q_signal_health(session: Session, tenant_id: int, window: Tuple[datetime, datetime]) -> Dict[str, int]:
    """
    CapiEvent rows of this tenant whose event time (coalesce(sent_at, created_at)) falls in the report week:
    ConfirmedOrder / DeliveredPurchase counts by status (sent, shadow), how many carry quality_flags (of sent+shadow),
    and the number of late_delivery rows (a DeliveredPurchase that missed Meta's window and was never sent).
    """
    event_time = func.coalesce(CapiEvent.sent_at, CapiEvent.created_at)
    flagged = case((and_(CapiEvent.quality_flags.isnot(None), CapiEvent.quality_flags != ""), 1), else_=0)
    rows = session.execute(
        select(CapiEvent.event_name, CapiEvent.status, func.count(CapiEvent.id), func.sum(flagged)).where(
            CapiEvent.tenant_id == tenant_id,
            CapiEvent.event_name.in_(SIGNAL_EVENT_NAMES),
            CapiEvent.status.in_(SIGNAL_EVENT_STATUSES + ("late_delivery",)),
            event_time >= window[0], event_time < window[1],
        ).group_by(CapiEvent.event_name, CapiEvent.status)
    ).all()
    out = {
        "confirmed_sent": 0, "confirmed_shadow": 0, "delivered_sent": 0, "delivered_shadow": 0,
        "total_events": 0, "flagged_events": 0, "late_delivery": 0,
    }
    for name, status, count, flagged_count in rows:
        count, flagged_count = int(count or 0), int(flagged_count or 0)
        if status == "late_delivery":
            out["late_delivery"] += count
            continue
        prefix = "confirmed" if name == CONFIRMED_EVENT_NAME else "delivered"
        out[f"{prefix}_{status}"] += count
        out["total_events"] += count
        out["flagged_events"] += flagged_count
    return out


def q_incidents(session: Session, tenant_id: int, window: Tuple[datetime, datetime]) -> List[Dict[str, Any]]:
    """Unresolved incidents (any age) plus incidents detected in the report week: kind, detected_at, unresolved."""
    rows = session.execute(
        select(Incident.kind, Incident.detected_at, Incident.resolved_at).where(
            Incident.tenant_id == tenant_id,
            or_(Incident.resolved_at.is_(None),
                and_(Incident.detected_at >= window[0], Incident.detected_at < window[1])),
        ).order_by(Incident.detected_at, Incident.id)
    ).all()
    return [{"kind": r[0], "detected_at": r[1], "unresolved": r[2] is None,
             "in_week": window[0] <= r[1] < window[1]} for r in rows]


def q_creatives(session: Session, tenant_id: int, window: Tuple[datetime, datetime],
                min_orders: int = MIN_CREATIVE_ORDERS) -> List[Dict[str, Any]]:
    """
    Matured-cohort performance per ad_id for ads with >= min_orders cohort orders: orders, delivered, delivery rate,
    net delivered value. Sorted by delivered value descending (ad_id ascending on ties).
    """
    delivered = _delivered_clause(tenant_id)
    n_orders = func.count(Order.id)
    rows = session.execute(
        select(
            Order.ad_id, n_orders,
            func.sum(case((delivered, 1), else_=0)),
            func.sum(case((delivered, Order.value), else_=0.0)),
        ).where(
            Order.tenant_id == tenant_id, _present(Order.ad_id),
            PLACED_AT >= window[0], PLACED_AT < window[1],
        ).group_by(Order.ad_id).having(n_orders >= min_orders)
    ).all()
    result = [{
        "ad_id": r[0], "orders": int(r[1]), "delivered": int(r[2] or 0),
        "delivery_rate": (int(r[2] or 0) / int(r[1])), "delivered_value": float(r[3] or 0.0),
    } for r in rows]
    result.sort(key=lambda r: (-r["delivered_value"], r["ad_id"]))
    return result


# --- Data ------------------------------------------------------------------------------------------------------

@dataclass
class CreativeRow:
    ad_id: str
    orders: int
    delivered: int
    delivery_rate: float
    delivered_value: float


@dataclass
class IncidentLine:
    kind: str
    on: date                      # tenant-local detection date
    unresolved: bool


@dataclass
class DigestData:
    """
    Everything the renderers show. Optional fields are None when their denominator is empty ("no data yet").
    Each field names the query function it comes from.
    """
    tenant_id: int
    tenant_name: str
    currency: str
    mode: str                                   # tenants.mode: shadow | live
    periods: DigestPeriods
    # q_week_orders (report week)
    orders_placed: int = 0
    cod_orders: int = 0
    cod_share: Optional[float] = None           # cod_orders / orders_placed
    confirmed_orders: int = 0
    confirmation_rate: Optional[float] = None   # confirmed_orders / orders_placed, as of send time
    # q_cohort_delivery(ad_only=False) (matured cohort)
    cohort_orders: int = 0
    cohort_delivered: int = 0
    delivered_rate: Optional[float] = None      # cohort_delivered / cohort_orders
    # q_cohort_delivery(ad_only=True)
    cohort_ad_orders: int = 0
    cohort_ad_delivered: int = 0
    ad_delivered_rate: Optional[float] = None   # None when no ad-driven orders in the cohort
    # q_refused_cod
    refused_count: int = 0
    refused_value: float = 0.0
    # q_cohort_delivery: gross ordered value of all cohort orders / net delivered value
    reported_value: float = 0.0
    delivered_value: float = 0.0
    vanity_ratio: Optional[float] = None        # reported_value / delivered_value; None when delivered_value == 0
    # q_signal_health (report week)
    confirmed_events_sent: int = 0
    confirmed_events_shadow: int = 0
    delivered_events_sent: int = 0
    delivered_events_shadow: int = 0
    signal_events_total: int = 0
    signal_events_flagged: int = 0
    flagged_share: Optional[float] = None       # signal_events_flagged / signal_events_total
    late_delivery_count: int = 0
    # q_incidents
    unresolved_incidents: List[IncidentLine] = field(default_factory=list)
    week_incidents: List[IncidentLine] = field(default_factory=list)
    unresolved_incident_total: int = 0
    week_incident_total: int = 0
    # q_creatives (None-equivalent: empty lists when no ad has >= MIN_CREATIVE_ORDERS orders)
    creatives_top: List[CreativeRow] = field(default_factory=list)
    creatives_bottom: List[CreativeRow] = field(default_factory=list)

    @property
    def has_signal_data(self) -> bool:
        return bool(self.signal_events_total or self.late_delivery_count)


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return numerator / denominator if denominator else None


def build_digest(session: Session, tenant_id: int, send_date: date) -> DigestData:
    """Builds the digest numbers for one tenant. `send_date` is the tenant-local date of the Sunday send."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"unknown tenant_id {tenant_id}")
    periods = digest_periods(send_date, tenant.timezone)
    tz = _tz(tenant.timezone)
    data = DigestData(tenant_id=tenant.id, tenant_name=tenant.name, currency=tenant.currency, mode=tenant.mode,
                      periods=periods)

    week = q_week_orders(session, tenant_id, periods.week)
    data.orders_placed, data.cod_orders, data.confirmed_orders = week["placed"], week["cod"], week["confirmed"]
    data.cod_share = _ratio(data.cod_orders, data.orders_placed)
    data.confirmation_rate = _ratio(data.confirmed_orders, data.orders_placed)

    cohort = q_cohort_delivery(session, tenant_id, periods.cohort)
    data.cohort_orders, data.cohort_delivered = int(cohort["orders"]), int(cohort["delivered"])
    data.delivered_rate = _ratio(data.cohort_delivered, data.cohort_orders)
    data.reported_value, data.delivered_value = cohort["gross_value"], cohort["delivered_value"]
    data.vanity_ratio = _ratio(data.reported_value, data.delivered_value)

    ads = q_cohort_delivery(session, tenant_id, periods.cohort, ad_only=True)
    data.cohort_ad_orders, data.cohort_ad_delivered = int(ads["orders"]), int(ads["delivered"])
    data.ad_delivered_rate = _ratio(data.cohort_ad_delivered, data.cohort_ad_orders)

    refused = q_refused_cod(session, tenant_id, periods.cohort)
    data.refused_count, data.refused_value = int(refused["count"]), float(refused["value"])

    health = q_signal_health(session, tenant_id, periods.week)
    data.confirmed_events_sent, data.confirmed_events_shadow = health["confirmed_sent"], health["confirmed_shadow"]
    data.delivered_events_sent, data.delivered_events_shadow = health["delivered_sent"], health["delivered_shadow"]
    data.signal_events_total, data.signal_events_flagged = health["total_events"], health["flagged_events"]
    data.flagged_share = _ratio(data.signal_events_flagged, data.signal_events_total)
    data.late_delivery_count = health["late_delivery"]

    incidents = q_incidents(session, tenant_id, periods.week)
    lines = [IncidentLine(i["kind"], i["detected_at"].astimezone(tz).date(), i["unresolved"]) for i in incidents]
    unresolved = [line for line, i in zip(lines, incidents) if i["unresolved"]]
    in_week = [line for line, i in zip(lines, incidents) if i["in_week"]]
    data.unresolved_incident_total, data.week_incident_total = len(unresolved), len(in_week)
    data.unresolved_incidents = unresolved[:MAX_INCIDENT_LINES]
    data.week_incidents = in_week[:MAX_INCIDENT_LINES]

    ranked = q_creatives(session, tenant_id, periods.cohort)
    rows = [CreativeRow(r["ad_id"], r["orders"], r["delivered"], r["delivery_rate"], r["delivered_value"])
            for r in ranked]
    data.creatives_top = rows[:CREATIVE_LIST_SIZE]
    data.creatives_bottom = list(reversed(rows[CREATIVE_LIST_SIZE:]))[:CREATIVE_LIST_SIZE]  # worst first, no overlap
    return data


# --- Rendering ---------------------------------------------------------------------------------------------------

_TEXT = {
    "ar": {
        "title": "ملخص صحة الإشارات الأسبوعي",
        "subject": "ملخص متهي الأسبوعي لصحة الإشارات — {a} إلى {b}",
        "period": "الأسبوع من {a} إلى {b}",
        "shadow_note": "وضع المراقبة (Shadow): لم يُرسل أي شيء إلى Meta. الأرقام أدناه تعرض ما كان سيتم إرساله.",
        "no_data": "لا توجد بيانات بعد",
        "na": "غير متاح",
        "of": "من",
        "sec_orders": "طلبات الأسبوع",
        "orders_placed": "الطلبات المسجّلة",
        "cod_share": "نسبة الدفع عند الاستلام",
        "confirmation_rate": "نسبة التأكيد (حتى وقت الإرسال)",
        "sec_delivery": "التسليم (طلبات من {a} إلى {b}، مرّ عليها 8 إلى 14 يومًا)",
        "delivered_rate": "نسبة التسليم",
        "ad_delivered_rate": "نسبة التسليم للطلبات القادمة من الإعلانات",
        "sec_refused": "الطلبات المرفوضة (الدفع عند الاستلام)",
        "refused_count": "عدد الطلبات المرفوضة بعد الشحن",
        "refused_value": "قيمة طلبات الدفع عند الاستلام المرفوضة التي استُبعدت من إشارة التسليم",
        "sec_roas": "مبالغة العائد الظاهري (ROAS)",
        "reported_value": "قيمة كل الطلبات (ما أبلغ عنه Purchase الأصلي)",
        "net_delivered_value": "صافي قيمة الطلبات المسلّمة",
        "vanity_ratio": "معامل المبالغة",
        "roas_note": "عائد Purchase الأصلي في Meta مبالغ فيه بهذا المعامل.",
        "roas_scope": "العائد المسلَّم مع الإنفاق الإعلاني غير مشمول هنا، لأنه يتطلب صلاحية ads_read.",
        "sec_signals": "صحة الإشارات (هذا الأسبوع)",
        "confirmed_sent": "أحداث ConfirmedOrder المُرسلة",
        "confirmed_shadow": "أحداث ConfirmedOrder التي كان سيتم إرسالها",
        "delivered_sent": "أحداث DeliveredPurchase المُرسلة",
        "delivered_shadow": "أحداث DeliveredPurchase التي كان سيتم إرسالها",
        "flagged_share": "نسبة الأحداث التي بها تنبيهات جودة",
        "late_delivery": "طلبات وصلت متأخرة ولم تُرسل (late_delivery)",
        "sec_incidents": "الحوادث",
        "no_incidents": "لا توجد حوادث",
        "unresolved": "غير محلولة",
        "this_week": "اكتُشفت هذا الأسبوع",
        "more": "و{n} أخرى",
        "sec_creatives": "تقييم الإعلانات (إعلانات لديها {n} طلبات أو أكثر)",
        "top": "الأعلى",
        "bottom": "الأدنى",
        "h_ad": "الإعلان", "h_orders": "الطلبات", "h_rate": "نسبة التسليم", "h_value": "القيمة المسلّمة",
        "footer": "أرقام مجمّعة فقط — لا تتضمن هذه الرسالة أي بيانات عملاء.",
    },
    "en": {
        "title": "Weekly Signal Hygiene digest",
        "subject": "Motahai weekly Signal Hygiene digest — {a} to {b}",
        "period": "Week of {a} to {b}",
        "shadow_note": "Shadow mode: nothing was sent to Meta. The numbers below show what would have been sent.",
        "no_data": "no data yet",
        "na": "n/a",
        "of": "of",
        "sec_orders": "This week's orders",
        "orders_placed": "Orders placed",
        "cod_share": "COD share",
        "confirmation_rate": "Confirmation rate (as of send time)",
        "sec_delivery": "Delivery (orders placed {a} to {b}, 8-14 days old)",
        "delivered_rate": "Delivered rate",
        "ad_delivered_rate": "Delivered rate, ad-driven orders only",
        "sec_refused": "Refused orders (COD, same cohort)",
        "refused_count": "Orders refused after dispatch",
        "refused_value": "Refused COD value kept out of the delivered signal",
        "sec_roas": "Vanity ROAS inflation",
        "reported_value": "Value of all orders (what native Purchase reported)",
        "net_delivered_value": "Net delivered value",
        "vanity_ratio": "Inflation factor",
        "roas_note": "Meta's native Purchase ROAS is overstated by this factor.",
        "roas_scope": "Delivered ROAS against ad spend is not included here: it needs ads_read access.",
        "sec_signals": "Signal health (this week)",
        "confirmed_sent": "ConfirmedOrder events sent",
        "confirmed_shadow": "ConfirmedOrder events that would have been sent",
        "delivered_sent": "DeliveredPurchase events sent",
        "delivered_shadow": "DeliveredPurchase events that would have been sent",
        "flagged_share": "Share of events with quality flags",
        "late_delivery": "Late deliveries, never sent (late_delivery)",
        "sec_incidents": "Incidents",
        "no_incidents": "No incidents",
        "unresolved": "Unresolved",
        "this_week": "Detected this week",
        "more": "and {n} more",
        "sec_creatives": "Creative scorecard (ads with {n}+ orders)",
        "top": "Top",
        "bottom": "Bottom",
        "h_ad": "Ad", "h_orders": "Orders", "h_rate": "Delivered rate", "h_value": "Delivered value",
        "footer": "Aggregate numbers only — this email contains no customer data.",
    },
}


def _lang(language: Optional[str]) -> str:
    return language if language in LANGUAGES else DEFAULT_LANGUAGE


@dataclass
class _Section:
    title: str
    rows: List[Tuple[str, str]] = field(default_factory=list)   # (label, value)
    notes: List[str] = field(default_factory=list)
    table: Optional[Tuple[List[str], List[List[str]]]] = None   # (headers, rows)
    subtitle_tables: List[Tuple[str, List[str], List[List[str]]]] = field(default_factory=list)
    sublists: List[Tuple[str, List[str]]] = field(default_factory=list)  # (heading, lines)


def _pct(rate: Optional[float], num: int, den: int, t: Dict[str, str]) -> str:
    if rate is None:
        return t["na"]
    return f"{rate * 100:.1f}% ({num} {t['of']} {den})"


def _money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def _sections(data: DigestData, lang: str) -> List[_Section]:
    t = _TEXT[lang]
    p = data.periods
    sections: List[_Section] = []

    orders = _Section(t["sec_orders"])
    if data.orders_placed:
        orders.rows = [
            (t["orders_placed"], str(data.orders_placed)),
            (t["cod_share"], _pct(data.cod_share, data.cod_orders, data.orders_placed, t)),
            (t["confirmation_rate"], _pct(data.confirmation_rate, data.confirmed_orders, data.orders_placed, t)),
        ]
    else:
        orders.rows = [(t["orders_placed"], t["no_data"])]
    sections.append(orders)

    delivery = _Section(t["sec_delivery"].format(a=p.cohort_start.isoformat(), b=p.cohort_end.isoformat()))
    refused = _Section(t["sec_refused"])
    roas = _Section(t["sec_roas"])
    if data.cohort_orders:
        delivery.rows = [
            (t["delivered_rate"], _pct(data.delivered_rate, data.cohort_delivered, data.cohort_orders, t)),
            (t["ad_delivered_rate"],
             _pct(data.ad_delivered_rate, data.cohort_ad_delivered, data.cohort_ad_orders, t)
             if data.cohort_ad_orders else t["no_data"]),
        ]
        refused.rows = [
            (t["refused_count"], str(data.refused_count)),
            (t["refused_value"], _money(data.refused_value, data.currency)),
        ]
        ratio = f"{data.vanity_ratio:.1f}×" if data.vanity_ratio is not None else t["na"]
        roas.rows = [
            (t["reported_value"], _money(data.reported_value, data.currency)),
            (t["net_delivered_value"], _money(data.delivered_value, data.currency)),
            (t["vanity_ratio"], ratio),
        ]
        if data.vanity_ratio is not None:
            roas.notes.append(t["roas_note"])
        roas.notes.append(t["roas_scope"])
    else:
        delivery.rows = [(t["delivered_rate"], t["no_data"])]
        refused.rows = [(t["refused_value"], t["no_data"])]
        roas.rows = [(t["vanity_ratio"], t["no_data"])]
        roas.notes.append(t["roas_scope"])
    sections += [delivery, refused, roas]

    signals = _Section(t["sec_signals"])
    if data.has_signal_data:
        for name, sent, shadow in (("confirmed", data.confirmed_events_sent, data.confirmed_events_shadow),
                                   ("delivered", data.delivered_events_sent, data.delivered_events_shadow)):
            if data.mode == "live" or sent:
                signals.rows.append((t[f"{name}_sent"], str(sent)))
            if data.mode == "shadow" or shadow:
                signals.rows.append((t[f"{name}_shadow"], str(shadow)))
        signals.rows.append((t["flagged_share"], _pct(data.flagged_share, data.signal_events_flagged,
                                                      data.signal_events_total, t)))
        signals.rows.append((t["late_delivery"], str(data.late_delivery_count)))
    else:
        signals.notes.append(t["no_data"])
    sections.append(signals)

    incidents = _Section(t["sec_incidents"])
    if not (data.unresolved_incident_total or data.week_incident_total):
        incidents.notes.append(t["no_incidents"])
    else:
        for heading, lines, total in ((t["unresolved"], data.unresolved_incidents, data.unresolved_incident_total),
                                      (t["this_week"], data.week_incidents, data.week_incident_total)):
            if not total:
                continue
            rendered = [f"{line.kind} — {line.on.isoformat()}" for line in lines]
            if total > len(lines):
                rendered.append(t["more"].format(n=total - len(lines)))
            incidents.sublists.append((heading, rendered))
    sections.append(incidents)

    if data.creatives_top:
        creative = _Section(t["sec_creatives"].format(n=MIN_CREATIVE_ORDERS))
        headers = [t["h_ad"], t["h_orders"], t["h_rate"], t["h_value"]]
        for heading, rows in ((t["top"], data.creatives_top), (t["bottom"], data.creatives_bottom)):
            if rows:
                creative.subtitle_tables.append((heading, headers, [
                    [r.ad_id, str(r.orders), f"{r.delivery_rate * 100:.1f}% ({r.delivered} {t['of']} {r.orders})",
                     _money(r.delivered_value, data.currency)] for r in rows]))
        sections.append(creative)
    return sections


def digest_subject(data: DigestData, language: Optional[str] = DEFAULT_LANGUAGE) -> str:
    """Subject line: generic, no numbers."""
    t = _TEXT[_lang(language)]
    return t["subject"].format(a=data.periods.week_start.isoformat(), b=data.periods.week_end.isoformat())


def render_digest_text(data: DigestData, language: Optional[str] = DEFAULT_LANGUAGE) -> str:
    lang = _lang(language)
    t = _TEXT[lang]
    out = [f"{t['title']} — {data.tenant_name}",
           t["period"].format(a=data.periods.week_start.isoformat(), b=data.periods.week_end.isoformat())]
    if data.mode == "shadow":
        out += ["", t["shadow_note"]]
    for section in _sections(data, lang):
        out += ["", section.title, "-" * min(len(section.title), 40)]
        out += [f"{label}: {value}" for label, value in section.rows]
        for heading, lines in section.sublists:
            out.append(f"{heading}:")
            out += [f"  - {line}" for line in lines]
        for heading, headers, rows in section.subtitle_tables:
            out.append(f"{heading}:")
            out += ["  " + " | ".join(row) for row in [headers] + rows]
        out += section.notes
    out += ["", t["footer"]]
    return "\n".join(out) + "\n"


_FONT = "font-family:Tahoma,Arial,Helvetica,sans-serif;"


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def render_digest_html(data: DigestData, language: Optional[str] = DEFAULT_LANGUAGE) -> str:
    """Email-safe HTML: inline CSS, table layout, no external assets, no tracking pixel."""
    lang = _lang(language)
    t = _TEXT[lang]
    rtl = lang == "ar"
    direction, align = ("rtl", "right") if rtl else ("ltr", "left")
    cell = f"padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:{align};{_FONT}font-size:14px;color:#111827;"
    parts = [
        f'<!DOCTYPE html><html lang="{lang}" dir="{direction}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(t['title'])}</title></head>",
        f'<body dir="{direction}" style="margin:0;padding:0;background:#f3f4f6;">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f3f4f6;"><tr><td align="center" style="padding:16px;">',
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" dir="{direction}" '
        'style="width:100%;max-width:600px;background:#ffffff;border:1px solid #e5e7eb;">',
        f'<tr><td style="padding:20px;text-align:{align};{_FONT}">'
        f'<div style="font-size:20px;font-weight:bold;color:#111827;">{_e(t["title"])} — {_e(data.tenant_name)}</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-top:4px;">'
        f'{_e(t["period"].format(a=data.periods.week_start.isoformat(), b=data.periods.week_end.isoformat()))}</div>'
        "</td></tr>",
    ]
    if data.mode == "shadow":
        parts.append(f'<tr><td style="padding:10px 20px;background:#fef3c7;color:#92400e;text-align:{align};{_FONT}'
                     f'font-size:13px;">{_e(t["shadow_note"])}</td></tr>')
    for section in _sections(data, lang):
        parts.append(f'<tr><td style="padding:16px 20px 4px 20px;text-align:{align};{_FONT}font-size:16px;'
                     f'font-weight:bold;color:#1f2937;">{_e(section.title)}</td></tr>')
        if section.rows:
            rows = "".join(
                f'<tr><td style="{cell}">{_e(label)}</td>'
                f'<td style="{cell}font-weight:bold;white-space:nowrap;">{_e(value)}</td></tr>'
                for label, value in section.rows)
            parts.append(f'<tr><td style="padding:0 20px;"><table role="presentation" width="100%" cellpadding="0" '
                         f'cellspacing="0" border="0" dir="{direction}">{rows}</table></td></tr>')
        for heading, lines in section.sublists:
            items = "".join(f'<li style="margin:2px 0;">{_e(line)}</li>' for line in lines)
            parts.append(f'<tr><td style="padding:6px 20px;text-align:{align};{_FONT}font-size:14px;color:#111827;">'
                         f'<div style="font-weight:bold;">{_e(heading)}</div><ul style="margin:4px 0;">{items}</ul>'
                         "</td></tr>")
        for heading, headers, rows in section.subtitle_tables:
            head = "".join(f'<th style="{cell}background:#f9fafb;">{_e(h)}</th>' for h in headers)
            body = "".join("<tr>" + "".join(f'<td style="{cell}">{_e(c)}</td>' for c in row) + "</tr>" for row in rows)
            parts.append(f'<tr><td style="padding:6px 20px;text-align:{align};{_FONT}font-size:14px;color:#111827;">'
                         f'<div style="font-weight:bold;margin-bottom:4px;">{_e(heading)}</div>'
                         f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                         f'dir="{direction}"><tr>{head}</tr>{body}</table></td></tr>')
        for note in section.notes:
            parts.append(f'<tr><td style="padding:4px 20px;text-align:{align};{_FONT}font-size:13px;color:#6b7280;">'
                         f'{_e(note)}</td></tr>')
    parts.append(f'<tr><td style="padding:20px;text-align:{align};{_FONT}font-size:12px;color:#9ca3af;">'
                 f'{_e(t["footer"])}</td></tr>')
    parts.append("</table></td></tr></table></body></html>")
    return "".join(parts)


# --- Senders ------------------------------------------------------------------------------------------------------

class DigestSender(Protocol):
    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        """Delivers one digest. Raises on failure."""


SMTP_HOST_ENV = "MOTAHAI_SMTP_HOST"
SMTP_PORT_ENV = "MOTAHAI_SMTP_PORT"
SMTP_USER_ENV = "MOTAHAI_SMTP_USER"
SMTP_PASSWORD_ENV = "MOTAHAI_SMTP_PASSWORD"
SMTP_FROM_ENV = "MOTAHAI_SMTP_FROM"


class SmtpNotConfigured(RuntimeError):
    """MOTAHAI_SMTP_HOST / MOTAHAI_SMTP_FROM are not set."""


class SmtpDigestSender:
    """SMTP delivery with STARTTLS. Settings come from the environment, read at send time."""

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        host, sender = os.environ.get(SMTP_HOST_ENV), os.environ.get(SMTP_FROM_ENV)
        if not host or not sender:
            raise SmtpNotConfigured(f"{SMTP_HOST_ENV} and {SMTP_FROM_ENV} must be set")
        port = int(os.environ.get(SMTP_PORT_ENV) or 587)
        user, password = os.environ.get(SMTP_USER_ENV), os.environ.get(SMTP_PASSWORD_ENV)
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, sender, to
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if user:
                smtp.login(user, password or "")
            smtp.send_message(message)


class LogOnlySender:
    """Dry-run sender: records that a send happened (recipient + subject), never the body."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, str]] = []

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        self.sent.append({"to": to, "subject": subject})
        logger.info("digest dry-run: would deliver %r to a recipient at %s", subject, to.rsplit("@", 1)[-1])


def sender_from_env() -> Optional[DigestSender]:
    """SmtpDigestSender when SMTP is configured in the environment, else None."""
    if os.environ.get(SMTP_HOST_ENV) and os.environ.get(SMTP_FROM_ENV):
        return SmtpDigestSender()
    return None


# --- Weekly job -----------------------------------------------------------------------------------------------------

def _as_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return utcnow()
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _record_failure(session: Session, tenant_id: int, week_start: date, error: Exception, now: datetime) -> bool:
    """One digest_failed incident per tenant per week (the job retries every tick; the incident is not repeated)."""
    prefix = f"week_start={week_start.isoformat()}"
    exists = session.scalar(select(Incident.id).where(
        Incident.tenant_id == tenant_id, Incident.kind == DIGEST_FAILED_KIND, Incident.detail.like(f"{prefix}%")))
    if exists is not None:
        return False
    # Error TYPE only: SMTP messages can echo the recipient address.
    session.add(Incident(tenant_id=tenant_id, kind=DIGEST_FAILED_KIND, severity="warning", detected_at=now,
                         detail=f"{prefix}: {type(error).__name__}"))
    session.commit()
    return True


def send_weekly_digests(session: Session, now: Optional[datetime], sender: DigestSender) -> Dict[str, int]:
    """
    For each active tenant with a digest_email: on Sunday at or after 10:00 tenant-local, if no digests row exists for
    that report week, build + render + send and insert the digests row. UNIQUE(tenant_id, week_start) makes it
    idempotent. A failure for one tenant becomes a digest_failed Incident and the others still go out.
    """
    now = _as_utc(now)
    counts = {"sent": 0, "already_sent": 0, "not_due": 0, "no_recipient": 0, "failed": 0}
    tenants = session.scalars(select(Tenant).where(Tenant.active.is_(True)).order_by(Tenant.id)).all()
    for tenant in tenants:
        if not tenant.digest_email:
            counts["no_recipient"] += 1
            continue
        tenant_id, week_start = tenant.id, None
        try:
            local = now.astimezone(_tz(tenant.timezone))
            if local.weekday() != SUNDAY or local.hour < DIGEST_HOUR_LOCAL:
                counts["not_due"] += 1
                continue
            send_date = local.date()
            week_start = send_date - timedelta(days=7)
            if session.scalar(select(Digest.id).where(Digest.tenant_id == tenant_id, Digest.week_start == week_start)):
                counts["already_sent"] += 1
                continue
            language = _lang(tenant.language)
            data = build_digest(session, tenant_id, send_date)
            sender.send(to=tenant.digest_email, subject=digest_subject(data, language),
                        html=render_digest_html(data, language), text=render_digest_text(data, language))
            session.add(Digest(tenant_id=tenant_id, week_start=week_start, sent_at=now, channel=DIGEST_CHANNEL))
            session.commit()
            counts["sent"] += 1
        except IntegrityError:
            session.rollback()  # another worker recorded this week first
            counts["already_sent"] += 1
        except Exception as exc:
            session.rollback()
            counts["failed"] += 1
            logger.error("Digest failed for tenant %s: %s", tenant_id, type(exc).__name__)
            try:
                _record_failure(session, tenant_id, week_start or _as_utc(now).date(), exc, now)
            except Exception:
                session.rollback()
                logger.exception("Could not record digest_failed incident for tenant %s", tenant_id)
    return counts
