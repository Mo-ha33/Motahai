"""
health_score.py — Tracking health score 0-100 per tenant (issue #42, plan M3-2).

The score is computed ONLY from real rows, never defaulted: capi_events.quality_flags, job_runs (scheduler heartbeat and
failures), incidents and staged_webhooks (dead letters / stuck rows). Nothing here reads or returns PII: the output is
a score, a status and a list of named penalties, each with the points deducted and evidence counts.

Formula (score = clamp(100 - sum(penalty points), 0, 100), as an int; every penalty is capped):

  flagged_events        round(FLAGGED_MAX_POINTS * flagged / total) over sent+shadow ConfirmedOrder/DeliveredPurchase
                        capi_events in the window (digest.q_signal_health); flagged = quality_flags non-empty. max 30
  late_delivery         LATE_DELIVERY_POINTS (2) per DeliveredPurchase row in status late_delivery.            max 10
  scheduler_stale       SCHEDULER_STALE_POINTS (15) when no successful send_due_events job_run finished within
                        HEARTBEAT_MAX_AGE (45 min) before `as_of`, or none exists at all.
  job_failures          JOB_FAILURE_POINTS (3) per job_run with ok = false finished in the window.            max 15
  incidents_critical /  per incident unresolved at `as_of`, by severity: critical 20 (max 40), error 10 (max 30),
  incidents_error /     warning 4 (max 12). Other severities do not score.
  incidents_warning
  webhooks_dead         WEBHOOK_DEAD_POINTS (8) per dead-lettered staged_webhooks row received before `as_of`. max 24
  webhooks_stuck        WEBHOOK_STUCK_POINTS (2) per pending staged_webhooks row older than WEBHOOK_STUCK_AGE (1h)
                        at `as_of` (the scheduler replay should have drained it).                              max 10

Status: >= 80 "healthy", >= 50 "degraded", otherwise "critical".

Insufficient data: a tenant with no capi signal events and no late_delivery rows in the window, no unresolved incidents
and no staged webhooks gets score null and status "insufficient_data" (never 100). job_runs are scheduler-global (the
table has no tenant_id), so they apply to every tenant equally and do not count as tenant data.

`as_of` is the end of the requested window, so a historical window is judged as it stood at that time.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Incident, JobRun, StagedWebhook
from .digest import q_signal_health

FLAGGED_MAX_POINTS = 30
LATE_DELIVERY_POINTS, LATE_DELIVERY_MAX = 2, 10
SCHEDULER_STALE_POINTS = 15
HEARTBEAT_MAX_AGE = timedelta(minutes=45)  # mirrors scheduler.HEARTBEAT_MAX_AGE (not imported: scheduler imports digest)
HEARTBEAT_JOB_NAME = "send_due_events"  # mirrors scheduler.SEND_JOB_NAME
JOB_FAILURE_POINTS, JOB_FAILURE_MAX = 3, 15
INCIDENT_POINTS = {"critical": (20, 40), "error": (10, 30), "warning": (4, 12)}  # severity -> (per incident, cap)
WEBHOOK_DEAD_POINTS, WEBHOOK_DEAD_MAX = 8, 24
WEBHOOK_STUCK_POINTS, WEBHOOK_STUCK_MAX = 2, 10
WEBHOOK_STUCK_AGE = timedelta(hours=1)
HEALTHY_MIN, DEGRADED_MIN = 80, 50


@dataclass(frozen=True)
class HealthInputs:
    """Plain counts read from real rows. `heartbeat_age_seconds` is None when no successful send job run exists."""
    total_events: int = 0
    flagged_events: int = 0
    late_delivery: int = 0
    heartbeat_age_seconds: Optional[float] = None
    job_failures: int = 0
    open_incidents: Dict[str, int] = field(default_factory=dict)  # severity -> unresolved count
    webhooks_dead: int = 0
    webhooks_stuck: int = 0
    staged_webhooks: int = 0  # every staged row regardless of status (data-presence check)


def _penalty(name: str, points: int, **evidence: Any) -> Dict[str, Any]:
    return {"name": name, "points": points, "evidence": evidence}


def compute_health_score(inputs: HealthInputs) -> Dict[str, Any]:
    """Pure: returns {"score", "status", "penalties": [{"name", "points", "evidence"}]} (score None if insufficient)."""
    has_data = bool(inputs.total_events or inputs.late_delivery or sum(inputs.open_incidents.values())
                    or inputs.staged_webhooks)
    if not has_data:
        return {"score": None, "status": "insufficient_data", "penalties": []}

    penalties: List[Dict[str, Any]] = []
    if inputs.total_events and inputs.flagged_events:
        points = round(FLAGGED_MAX_POINTS * inputs.flagged_events / inputs.total_events)
        penalties.append(_penalty("flagged_events", points, flagged=inputs.flagged_events, total=inputs.total_events))
    if inputs.late_delivery:
        penalties.append(_penalty("late_delivery", min(LATE_DELIVERY_MAX, LATE_DELIVERY_POINTS * inputs.late_delivery),
                                  count=inputs.late_delivery))
    if inputs.heartbeat_age_seconds is None or inputs.heartbeat_age_seconds > HEARTBEAT_MAX_AGE.total_seconds():
        penalties.append(_penalty("scheduler_stale", SCHEDULER_STALE_POINTS,
                                  last_success_found=inputs.heartbeat_age_seconds is not None,
                                  age_seconds=inputs.heartbeat_age_seconds))
    if inputs.job_failures:
        penalties.append(_penalty("job_failures", min(JOB_FAILURE_MAX, JOB_FAILURE_POINTS * inputs.job_failures),
                                  count=inputs.job_failures))
    for severity, (per, cap) in INCIDENT_POINTS.items():
        count = inputs.open_incidents.get(severity, 0)
        if count:
            penalties.append(_penalty(f"incidents_{severity}", min(cap, per * count), open=count))
    if inputs.webhooks_dead:
        penalties.append(_penalty("webhooks_dead", min(WEBHOOK_DEAD_MAX, WEBHOOK_DEAD_POINTS * inputs.webhooks_dead),
                                  count=inputs.webhooks_dead))
    if inputs.webhooks_stuck:
        penalties.append(_penalty("webhooks_stuck", min(WEBHOOK_STUCK_MAX, WEBHOOK_STUCK_POINTS * inputs.webhooks_stuck),
                                  count=inputs.webhooks_stuck))

    score = max(0, min(100, 100 - sum(p["points"] for p in penalties)))
    status = "healthy" if score >= HEALTHY_MIN else "degraded" if score >= DEGRADED_MIN else "critical"
    return {"score": score, "status": status, "penalties": penalties}


def collect_health_inputs(session: Session, tenant_id: int, window: Tuple[datetime, datetime]) -> HealthInputs:
    """Reads the counts for one tenant (job_runs are global). `as_of` = window end."""
    start, as_of = window
    signal = q_signal_health(session, tenant_id, window)

    last_ok = session.scalar(select(func.max(JobRun.finished_at)).where(
        JobRun.job_name == HEARTBEAT_JOB_NAME, JobRun.ok.is_(True), JobRun.finished_at <= as_of))
    failures = session.scalar(select(func.count(JobRun.id)).where(
        JobRun.ok.is_(False), JobRun.finished_at >= start, JobRun.finished_at < as_of)) or 0

    open_by_sev: Dict[str, int] = {}
    for severity, count in session.execute(
            select(Incident.severity, func.count(Incident.id)).where(
                Incident.tenant_id == tenant_id, Incident.resolved_at.is_(None), Incident.detected_at < as_of,
            ).group_by(Incident.severity)).all():
        open_by_sev[severity] = int(count)

    staged = dict(session.execute(
        select(StagedWebhook.status, func.count(StagedWebhook.id)).where(
            StagedWebhook.tenant_id == tenant_id, StagedWebhook.received_at < as_of,
        ).group_by(StagedWebhook.status)).all())
    stuck = session.scalar(select(func.count(StagedWebhook.id)).where(
        StagedWebhook.tenant_id == tenant_id, StagedWebhook.status == "pending",
        StagedWebhook.received_at < as_of - WEBHOOK_STUCK_AGE)) or 0

    return HealthInputs(
        total_events=signal["total_events"], flagged_events=signal["flagged_events"],
        late_delivery=signal["late_delivery"],
        heartbeat_age_seconds=None if last_ok is None else (as_of - last_ok).total_seconds(),
        job_failures=int(failures), open_incidents=open_by_sev,
        webhooks_dead=int(staged.get("dead", 0)), webhooks_stuck=int(stuck),
        staged_webhooks=sum(int(v) for v in staged.values()),
    )


def tenant_health_score(session: Session, tenant_id: int, window: Tuple[datetime, datetime]) -> Dict[str, Any]:
    return compute_health_score(collect_health_inputs(session, tenant_id, window))
