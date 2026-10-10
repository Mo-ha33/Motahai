"""
stats_routes.py — Tenant-scoped, read-only stats endpoints over the Sunday-digest queries (issue #41).

  GET /v1/tenants/{tenant_id}/stats/week-orders       -> digest.q_week_orders
  GET /v1/tenants/{tenant_id}/stats/cohort-delivery   -> digest.q_cohort_delivery (all orders + ad-driven only)
  GET /v1/tenants/{tenant_id}/stats/refused-cod       -> digest.q_refused_cod
  GET /v1/tenants/{tenant_id}/stats/signal-health     -> digest.q_signal_health
  GET /v1/tenants/{tenant_id}/stats/creatives         -> digest.q_creatives
  GET /v1/tenants/{tenant_id}/stats/health-score      -> health_score.tenant_health_score
  GET /v1/tenants/{tenant_id}/stats/summary           -> all five in one response

Every response is {"tenant_id", "currency", "window": {"start", "end"}, "data": ...} ("currency" = the tenant's ISO
4217 code, for formatting money); every number comes straight from the existing
q_* functions (nothing is computed or defaulted here). Cohort-based endpoints (cohort-delivery, refused-cod, creatives,
and summary) additionally carry "cohort_window".

Window: optional `start` / `end` query params (ISO 8601 date or datetime; naive = UTC), half-open [start, end). Default
is the last 7 full UTC days ending at today's UTC midnight. end <= start or a span over 92 days -> 422. As in
digest.build_digest, week-orders and signal-health use the window itself while the matured-cohort queries use the
window shifted back 7 days (COHORT_SHIFT), so for the same week the numbers match the Sunday digest. Unlike the digest,
boundaries are UTC midnights, not tenant-local ones.

Auth: every route accepts either the operator key (auth.require_operator) or a tenant API key (`mtk_...`) holding the
`stats:read` scope (tenant_auth.require_operator_or_tenant_key). A tenant key may only read its own tenant: another
tenant's id is 404, the same as an unknown tenant (no existence leak). Any bad tenant key is the uniform 401. The
tenant comes ONLY from the path; an unknown tenant is 404. Read-only: no writes, aggregates only (no PII), nothing logged.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, sessionmaker

from .db import Tenant, get_session_factory
from .tenant_auth import ensure_tenant_matches, require_operator_or_tenant_key
from .health_score import tenant_health_score
from .digest import q_cohort_delivery, q_creatives, q_refused_cod, q_signal_health, q_week_orders

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 92
COHORT_SHIFT = timedelta(days=7)  # digest_periods: cohort = report week shifted back one week

Window = Tuple[datetime, datetime]
SECTIONS = ("week-orders", "cohort-delivery", "refused-cod", "signal-health", "creatives")
_COHORT_BASED = {"cohort-delivery", "refused-cod", "creatives"}

require_stats_auth = require_operator_or_tenant_key("stats:read")

router = APIRouter(prefix="/v1/tenants/{tenant_id}/stats")


def get_stats_session_factory() -> sessionmaker:
    """FastAPI dependency (overridable in tests) returning the sessionmaker used for stats reads."""
    return get_session_factory()


def _parse_bound(name: str, raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be an ISO 8601 date or datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def get_window(start: Optional[str] = Query(None), end: Optional[str] = Query(None)) -> Window:
    """Resolves and validates the [start, end) UTC window (default: the 7 full days before today's UTC midnight)."""
    end_dt = _parse_bound("end", end) if end else datetime.combine(
        datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    start_dt = _parse_bound("start", start) if start else end_dt - timedelta(days=DEFAULT_WINDOW_DAYS)
    if end_dt <= start_dt:
        raise HTTPException(status_code=422, detail="end must be after start")
    if end_dt - start_dt > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(status_code=422, detail=f"window must not exceed {MAX_WINDOW_DAYS} days")
    return start_dt, end_dt


def _cohort(window: Window) -> Window:
    return window[0] - COHORT_SHIFT, window[1] - COHORT_SHIFT


def _iso(window: Window) -> Dict[str, str]:
    return {"start": window[0].isoformat(), "end": window[1].isoformat()}


def _compute(session: Session, tenant_id: int, name: str, window: Window) -> Any:
    cohort = _cohort(window)
    if name == "week-orders":
        return q_week_orders(session, tenant_id, window)
    if name == "cohort-delivery":
        return {"all": q_cohort_delivery(session, tenant_id, cohort),
                "ad_driven": q_cohort_delivery(session, tenant_id, cohort, ad_only=True)}
    if name == "refused-cod":
        return q_refused_cod(session, tenant_id, cohort)
    if name == "signal-health":
        return q_signal_health(session, tenant_id, window)
    return q_creatives(session, tenant_id, cohort)


def _respond(factory: sessionmaker, tenant_id: int, window: Window, names: List[str],
             caller: Optional[Tenant] = None) -> Dict[str, Any]:
    if caller is not None:
        ensure_tenant_matches(caller, tenant_id)
    with factory() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Unknown tenant")
        currency = tenant.currency
        data = {n: _compute(session, tenant_id, n, window) for n in names}
    body: Dict[str, Any] = {"tenant_id": tenant_id, "currency": currency, "window": _iso(window)}
    if any(n in _COHORT_BASED for n in names):
        body["cohort_window"] = _iso(_cohort(window))
    body["data"] = data if len(names) > 1 else data[names[0]]
    return body


def _add_route(name: str) -> None:
    def endpoint(tenant_id: int, caller: Optional[Tenant] = Depends(require_stats_auth),  # auth first: before 422s
                 window: Window = Depends(get_window),
                 factory: sessionmaker = Depends(get_stats_session_factory)) -> Dict[str, Any]:
        return _respond(factory, tenant_id, window, [name], caller)
    endpoint.__name__ = "stats_" + name.replace("-", "_")
    router.get("/" + name)(endpoint)


for _name in SECTIONS:
    _add_route(_name)


@router.get("/health-score")
def stats_health_score(tenant_id: int, caller: Optional[Tenant] = Depends(require_stats_auth),  # auth first: before 422s
                       window: Window = Depends(get_window),
                       factory: sessionmaker = Depends(get_stats_session_factory)) -> Dict[str, Any]:
    if caller is not None:
        ensure_tenant_matches(caller, tenant_id)
    with factory() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Unknown tenant")
        data = tenant_health_score(session, tenant_id, window)
        currency = tenant.currency
    return {"tenant_id": tenant_id, "currency": currency, "window": _iso(window), "data": data}


@router.get("/summary")
def stats_summary(tenant_id: int, caller: Optional[Tenant] = Depends(require_stats_auth),
                  window: Window = Depends(get_window),
                  factory: sessionmaker = Depends(get_stats_session_factory)) -> Dict[str, Any]:
    return _respond(factory, tenant_id, window, list(SECTIONS), caller)
