"""
stats_routes.py — Tenant-scoped, read-only stats endpoints over the Sunday-digest queries (issue #41).

  GET /v1/tenants/{tenant_id}/stats/week-orders       -> digest.q_week_orders
  GET /v1/tenants/{tenant_id}/stats/cohort-delivery   -> digest.q_cohort_delivery (all orders + ad-driven only)
  GET /v1/tenants/{tenant_id}/stats/refused-cod       -> digest.q_refused_cod
  GET /v1/tenants/{tenant_id}/stats/signal-health     -> digest.q_signal_health
  GET /v1/tenants/{tenant_id}/stats/creatives         -> digest.q_creatives
  GET /v1/tenants/{tenant_id}/stats/health-score      -> health_score.tenant_health_score (tracking health 0-100)
  GET /v1/tenants/{tenant_id}/stats/summary           -> the five digest sections in one response (not health-score)

health-score: data = {"score": int 0-100 | null, "status": healthy|degraded|critical|insufficient_data, "penalties":
[{"name", "points", "evidence"}]}. score = 100 - sum(penalty points), clamped; a tenant with no signal events, incidents
or staged webhooks is "insufficient_data" with a null score (never 100). as_of = window end. The full formula and
constants are documented in health_score.py.

Every response is {"tenant_id", "window": {"start", "end"}, "data": ...}; every number comes straight from the existing
q_* functions (nothing is computed or defaulted here). Cohort-based endpoints (cohort-delivery, refused-cod, creatives,
and summary) additionally carry "cohort_window".

Window: optional `start` / `end` query params (ISO 8601 date or datetime; naive = UTC), half-open [start, end). Default
is the last 7 full UTC days ending at today's UTC midnight. end <= start or a span over 92 days -> 422. As in
digest.build_digest, week-orders and signal-health use the window itself while the matured-cohort queries use the
window shifted back 7 days (COHORT_SHIFT), so for the same week the numbers match the Sunday digest. Unlike the digest,
boundaries are UTC midnights, not tenant-local ones.

Auth: until per-tenant API keys exist (M3-3) every route requires the operator key (auth.require_operator). The tenant
comes ONLY from the path; an unknown tenant is 404. Read-only: no writes, aggregates only (no PII), nothing logged.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, sessionmaker

from .auth import require_operator
from .db import Tenant, get_session_factory
from .health_score import tenant_health_score
from .digest import q_cohort_delivery, q_creatives, q_refused_cod, q_signal_health, q_week_orders

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 92
COHORT_SHIFT = timedelta(days=7)  # digest_periods: cohort = report week shifted back one week

Window = Tuple[datetime, datetime]
SECTIONS = ("week-orders", "cohort-delivery", "refused-cod", "signal-health", "creatives")
_COHORT_BASED = {"cohort-delivery", "refused-cod", "creatives"}

router = APIRouter(prefix="/v1/tenants/{tenant_id}/stats", dependencies=[Depends(require_operator)])


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


def _respond(factory: sessionmaker, tenant_id: int, window: Window, names: List[str]) -> Dict[str, Any]:
    with factory() as session:
        if session.get(Tenant, tenant_id) is None:
            raise HTTPException(status_code=404, detail="Unknown tenant")
        data = {n: _compute(session, tenant_id, n, window) for n in names}
    body: Dict[str, Any] = {"tenant_id": tenant_id, "window": _iso(window)}
    if any(n in _COHORT_BASED for n in names):
        body["cohort_window"] = _iso(_cohort(window))
    body["data"] = data if len(names) > 1 else data[names[0]]
    return body


def _add_route(name: str) -> None:
    def endpoint(tenant_id: int, window: Window = Depends(get_window),
                 factory: sessionmaker = Depends(get_stats_session_factory)) -> Dict[str, Any]:
        return _respond(factory, tenant_id, window, [name])
    endpoint.__name__ = "stats_" + name.replace("-", "_")
    router.get("/" + name)(endpoint)


for _name in SECTIONS:
    _add_route(_name)


@router.get("/health-score")
def stats_health_score(tenant_id: int, window: Window = Depends(get_window),
                       factory: sessionmaker = Depends(get_stats_session_factory)) -> Dict[str, Any]:
    with factory() as session:
        if session.get(Tenant, tenant_id) is None:
            raise HTTPException(status_code=404, detail="Unknown tenant")
        data = tenant_health_score(session, tenant_id, window)
    return {"tenant_id": tenant_id, "window": _iso(window), "data": data}


@router.get("/summary")
def stats_summary(tenant_id: int, window: Window = Depends(get_window),
                  factory: sessionmaker = Depends(get_stats_session_factory)) -> Dict[str, Any]:
    return _respond(factory, tenant_id, window, list(SECTIONS))
