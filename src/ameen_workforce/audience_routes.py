"""
audience_routes.py — tenant-scoped access to the hashed Meta audience exports (M4-5, issue #52).

  GET  /v1/tenants/{tenant_id}/audiences                 -> status of both lists + the weekly export schedule
  GET  /v1/tenants/{tenant_id}/audiences/{name}.csv      -> download one list (name: exclude_refusers | seed_delivered_buyers)
  POST /v1/tenants/{tenant_id}/audiences/export          -> run the export now (OPERATOR KEY ONLY)

Auth: GET routes accept the operator key or a tenant API key holding the `audiences:read` scope
(tenant_auth.require_operator_or_tenant_key). Tenant keys default to `stats:read` only, so an operator must grant
`audiences:read` explicitly: hashed customer lists are more sensitive than aggregates. A tenant key may only read its
own tenant (another tenant's id is 404, like an unknown tenant). A tenant key without the scope is the uniform 401.
POST /export is operator-only: ANY `mtk_` bearer gets 403 (the key is not even looked up), every other credential goes
through auth.require_operator.

Files: the path is built ONLY from the integer tenant id and one of the two fixed list names, inside
operator_routes.audience_export_dir(); it is resolved and must stay inside that directory (symlinks pointing out are
treated as "not exported"). The status response carries counts, timestamps and sizes only: never hashes or row content.
Downloads are `Cache-Control: no-store` attachments. One log line per download (tenant id, list name, caller kind);
contents are never logged.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .audiences import MIN_LIST_ROWS, export_tenant_audiences
from .auth import require_operator
from .db import JobRun, Tenant
from .operator_routes import audience_export_dir
from .scheduler import AUDIENCE_EXPORT_INTERVAL, AUDIENCE_EXPORT_JOB_NAME
from .tenant_auth import KEY_TAG, ensure_tenant_matches, require_operator_or_tenant_key
from .webhook_routes import get_session_factory_dep

logger = logging.getLogger("ameen_workforce.service")

LIST_NAMES = ("exclude_refusers", "seed_delivered_buyers")

require_audience_read = require_operator_or_tenant_key("audiences:read")

router = APIRouter(prefix="/v1/tenants/{tenant_id}/audiences")


def require_operator_only(authorization: Optional[str] = Header(None)) -> None:
    """Operator key only. Any tenant-key bearer is a 403 (authenticated-style refusal, no key lookup)."""
    if authorization and authorization.lower().startswith("bearer ") and authorization[7:].strip().startswith(KEY_TAG):
        raise HTTPException(status_code=403, detail="Operator key required")
    require_operator(authorization)


def _list_path(tenant_id: int, name: str) -> Optional[Path]:
    """The CSV path for (int tenant id, fixed list name) strictly inside the export dir, else None."""
    if name not in LIST_NAMES:
        return None
    base = Path(audience_export_dir()).resolve()
    path = (base / f"{int(tenant_id)}_{name}.csv").resolve()
    if path.parent != base:  # e.g. a symlink pointing outside the directory
        return None
    return path


def _count_rows(path: Path) -> int:
    """Data rows (header excluded), streamed in binary chunks: the file is never loaded whole."""
    rows = 0
    last = b"\n"
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 16):
            rows += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        rows += 1  # final line without a trailing newline
    return max(rows - 1, 0)


def _list_status(tenant_id: int, name: str) -> Dict[str, Any]:
    path = _list_path(tenant_id, name)
    try:
        if path is None or not path.is_file():
            raise FileNotFoundError
        stat = path.stat()
        rows: Optional[int] = _count_rows(path)
    except OSError:
        return {"name": name, "rows": None, "updated_at": None, "size_bytes": None}
    return {"name": name, "rows": rows, "size_bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()}


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _schedule(session: Session) -> Dict[str, Any]:
    """Latest run of the (global) weekly job; next due = last successful finish + 7 days (as the scheduler decides)."""
    last = session.scalars(select(JobRun).where(JobRun.job_name == AUDIENCE_EXPORT_JOB_NAME)
                           .order_by(JobRun.id.desc()).limit(1)).first()
    last_ok_finished = session.scalar(select(JobRun.finished_at).where(
        JobRun.job_name == AUDIENCE_EXPORT_JOB_NAME, JobRun.ok.is_(True)).order_by(JobRun.id.desc()).limit(1))
    next_due = None
    if last_ok_finished is not None:
        if last_ok_finished.tzinfo is None:
            last_ok_finished = last_ok_finished.replace(tzinfo=timezone.utc)
        next_due = last_ok_finished + AUDIENCE_EXPORT_INTERVAL
    return {
        "job_name": AUDIENCE_EXPORT_JOB_NAME,
        "last_run_at": _iso(last.finished_at or last.started_at) if last is not None else None,
        "last_run_ok": last.ok if last is not None else None,
        "last_error_type": last.error_type if last is not None else None,
        "next_due_at": _iso(next_due),
    }


def _status(session: Session, tenant_id: int) -> Dict[str, Any]:
    lists = []
    for name in LIST_NAMES:
        item = _list_status(tenant_id, name)
        lists.append({k: item[k] for k in ("name", "rows", "updated_at", "size_bytes")})
    return {"tenant_id": tenant_id, "min_list_rows": MIN_LIST_ROWS, "lists": lists, "schedule": _schedule(session)}


def _require_tenant(session: Session, tenant_id: int) -> None:
    if session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")


@router.get("")
def audiences_status(tenant_id: int, caller: Optional[Tenant] = Depends(require_audience_read),  # auth first
                     factory: sessionmaker = Depends(get_session_factory_dep)) -> Dict[str, Any]:
    if caller is not None:
        ensure_tenant_matches(caller, tenant_id)
    with factory() as session:
        _require_tenant(session, tenant_id)
        return _status(session, tenant_id)


@router.get("/{name}.csv")
def audiences_download(tenant_id: int, name: str, caller: Optional[Tenant] = Depends(require_audience_read),
                       factory: sessionmaker = Depends(get_session_factory_dep)) -> FileResponse:
    if caller is not None:
        ensure_tenant_matches(caller, tenant_id)
    if name not in LIST_NAMES:
        raise HTTPException(status_code=404, detail="Not found")
    with factory() as session:
        _require_tenant(session, tenant_id)
    path = _list_path(tenant_id, name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Audience not exported yet")
    logger.info("Audience download tenant=%s list=%s caller=%s", tenant_id, name,
                "operator" if caller is None else "tenant")
    return FileResponse(path, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{tenant_id}_{name}.csv"',
        "Cache-Control": "no-store",
    })


@router.post("/export")
def audiences_export(tenant_id: int, _operator: None = Depends(require_operator_only),  # auth first
                     factory: sessionmaker = Depends(get_session_factory_dep)) -> Dict[str, Any]:
    with factory() as session:
        _require_tenant(session, tenant_id)
        try:
            export_tenant_audiences(session, tenant_id, audience_export_dir())
        except Exception as exc:
            session.rollback()
            logger.error("Audience export failed tenant=%s: %s", tenant_id, type(exc).__name__)
            raise HTTPException(status_code=500, detail="Audience export failed")
        logger.info("Audience export tenant=%s caller=operator", tenant_id)
        return _status(session, tenant_id)
