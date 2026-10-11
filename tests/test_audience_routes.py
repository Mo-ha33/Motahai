"""
test_audience_routes.py — tenant audience status / download / export endpoints (M4-5, issue #52).
"""

import logging
import os
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce.audience_routes import LIST_NAMES, _list_path, router
from src.ameen_workforce.audiences import MIN_LIST_ROWS, write_customer_list_csv
from src.ameen_workforce.db import JobRun, create_tenant, utcnow
from src.ameen_workforce.scheduler import AUDIENCE_EXPORT_JOB_NAME
from src.ameen_workforce.tenant_auth import create_tenant_key
from src.ameen_workforce.webhook_routes import get_session_factory_dep

OPERATOR_KEY = "op-key-for-audience-tests"
AUTH = {"Authorization": f"Bearer {OPERATOR_KEY}"}


def h(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def tenants(db_session):
    a = create_tenant(db_session, name="A Shop", platform="shopify", shop_domain="a.myshopify.com")
    b = create_tenant(db_session, name="B Shop", platform="salla", shop_domain="555")
    return a, b


@pytest.fixture
def client(db_session, fernet_key, monkeypatch, tenants):
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    return TestClient(app)


def base(tenant_id):
    return f"/v1/tenants/{tenant_id}/audiences"


def write_lists(tenant_id):
    out = Path(os.environ["AUDIENCE_EXPORT_DIR"])
    write_customer_list_csv([{"phone_hash": h(i), "email_hash": h(100 + i)} for i in range(3)],
                            out / f"{tenant_id}_exclude_refusers.csv")
    write_customer_list_csv([{"phone_hash": h(i), "email_hash": None, "value": 10.0 * i} for i in range(1, 6)],
                            out / f"{tenant_id}_seed_delivered_buyers.csv", include_value=True)
    return out


def by_name(body):
    return {item["name"]: item for item in body["lists"]}


def test_status_before_and_after_export(client, tenants):
    a, _ = tenants
    body = client.get(base(a.id), headers=AUTH).json()
    assert body["tenant_id"] == a.id and body["min_list_rows"] == MIN_LIST_ROWS
    assert [i["name"] for i in body["lists"]] == list(LIST_NAMES)
    assert all(i["rows"] is None and i["updated_at"] is None and i["size_bytes"] is None for i in body["lists"])
    assert body["schedule"] == {"job_name": AUDIENCE_EXPORT_JOB_NAME, "last_run_at": None, "last_run_ok": None,
                                "last_error_type": None, "next_due_at": None}

    resp = client.post(base(a.id) + "/export", headers=AUTH)
    assert resp.status_code == 200
    exported = resp.json()
    assert set(exported) == {"tenant_id", "min_list_rows", "lists", "schedule"}  # no file paths
    assert all(i["rows"] == 0 and i["size_bytes"] > 0 and i["updated_at"] for i in exported["lists"])
    assert client.get(base(a.id), headers=AUTH).json()["lists"] == exported["lists"]


def test_status_counts_rows_without_leaking_hashes(client, tenants):
    a, _ = tenants
    write_lists(a.id)
    resp = client.get(base(a.id), headers=AUTH)
    items = by_name(resp.json())
    assert items["exclude_refusers"]["rows"] == 3
    assert items["seed_delivered_buyers"]["rows"] == 5
    assert h(1) not in resp.text


def test_schedule_from_latest_job_run(client, db_session, tenants):
    a, _ = tenants
    finished = utcnow() - timedelta(days=2)
    db_session.add(JobRun(job_name=AUDIENCE_EXPORT_JOB_NAME, started_at=finished, finished_at=finished, ok=True))
    db_session.add(JobRun(job_name=AUDIENCE_EXPORT_JOB_NAME, started_at=utcnow(), finished_at=utcnow(), ok=False,
                          error_type="RuntimeError"))
    db_session.commit()
    sched = client.get(base(a.id), headers=AUTH).json()["schedule"]
    assert sched["last_run_ok"] is False and sched["last_error_type"] == "RuntimeError"
    assert sched["last_run_at"] is not None and sched["next_due_at"] is not None


def test_download(client, tenants, caplog):
    a, _ = tenants
    out = write_lists(a.id)
    with caplog.at_level(logging.INFO, logger="ameen_workforce.service"):
        resp = client.get(base(a.id) + "/exclude_refusers.csv", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == f'attachment; filename="{a.id}_exclude_refusers.csv"'
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content == (out / f"{a.id}_exclude_refusers.csv").read_bytes()
    lines = [r.getMessage() for r in caplog.records if "Audience download" in r.getMessage()]
    assert lines == [f"Audience download tenant={a.id} list=exclude_refusers caller=operator"]
    assert not any(h(1) in r.getMessage() for r in caplog.records)


def test_unknown_name_and_not_exported_are_404(client, tenants):
    a, _ = tenants
    write_lists(a.id)
    assert client.get(base(a.id) + "/other.csv", headers=AUTH).status_code == 404
    assert client.get(base(a.id) + "/..%2Fsecret.csv", headers=AUTH).status_code == 404
    assert client.get(base(a.id) + "/exclude_refusers.csv", headers={"Authorization": "Bearer nope"}).status_code == 401
    b_resp = client.get(base(tenants[1].id) + "/seed_delivered_buyers.csv", headers=AUTH)
    assert b_resp.status_code == 404  # tenant B has nothing exported yet
    assert client.get(base(99999), headers=AUTH).status_code == 404
    assert client.get(base(99999) + "/exclude_refusers.csv", headers=AUTH).status_code == 404


def test_tenant_key_with_scope_reads_own_only(client, db_session, tenants):
    a, b = tenants
    write_lists(a.id)
    write_lists(b.id)
    _, key = create_tenant_key(db_session, a.id, scopes="stats:read audiences:read")
    hdr = {"Authorization": f"Bearer {key}"}
    assert client.get(base(a.id), headers=hdr).status_code == 200
    own = client.get(base(a.id) + "/seed_delivered_buyers.csv", headers=hdr)
    assert own.status_code == 200 and own.headers["cache-control"] == "no-store"
    assert client.get(base(b.id), headers=hdr).status_code == 404
    assert client.get(base(b.id) + "/seed_delivered_buyers.csv", headers=hdr).status_code == 404


def test_tenant_key_without_scope_is_401(client, db_session, tenants):
    a, _ = tenants
    write_lists(a.id)
    _, key = create_tenant_key(db_session, a.id)  # default scope: stats:read only
    hdr = {"Authorization": f"Bearer {key}"}
    assert client.get(base(a.id), headers=hdr).status_code == 401
    assert client.get(base(a.id) + "/exclude_refusers.csv", headers=hdr).status_code == 401


def test_export_is_operator_only(client, db_session, tenants):
    a, _ = tenants
    _, key = create_tenant_key(db_session, a.id, scopes="stats:read audiences:read")
    assert client.post(base(a.id) + "/export", headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert client.post(base(a.id) + "/export").status_code == 401
    assert client.post(base(a.id) + "/export", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(base(99999) + "/export", headers=AUTH).status_code == 404
    assert not (Path(os.environ["AUDIENCE_EXPORT_DIR"]) / f"{a.id}_exclude_refusers.csv").exists()


def test_path_stays_inside_export_dir(tenants, tmp_path):
    a, _ = tenants
    base_dir = Path(os.environ["AUDIENCE_EXPORT_DIR"]).resolve()
    for name in LIST_NAMES:
        assert _list_path(a.id, name).parent == base_dir
    assert _list_path(a.id, "../x") is None
    assert _list_path(a.id, "exclude_refusers/../../x") is None
    # a symlink escaping the directory is refused
    base_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.csv"
    outside.write_text("phone,email\n")
    (base_dir / f"{a.id}_exclude_refusers.csv").symlink_to(outside)
    assert _list_path(a.id, "exclude_refusers") is None


def _write_rows_file(path: Path, data_rows: int, include_value: bool = False) -> None:
    write_customer_list_csv([{"phone_hash": h(i), "email_hash": None, "value": 1.0} for i in range(data_rows)],
                            path, include_value=include_value)


def test_row_count_excludes_header_for_multiline_empty_and_chunked_files(tmp_path):
    from src.ameen_workforce.audience_routes import _count_rows

    many = tmp_path / "many.csv"
    _write_rows_file(many, 7, include_value=True)  # multi-line file
    assert _count_rows(many) == 7

    empty = tmp_path / "empty.csv"
    write_customer_list_csv([], empty)  # header only
    assert empty.read_text().count("\n") == 1
    assert _count_rows(empty) == 0

    no_newline = tmp_path / "no_newline.csv"
    no_newline.write_bytes(b"phone,email\nabc,def\nghi,jkl")  # last row lacks a trailing newline
    assert _count_rows(no_newline) == 2

    zero_bytes = tmp_path / "zero.csv"
    zero_bytes.write_bytes(b"")
    assert _count_rows(zero_bytes) == 0

    # Larger than the 64 KiB read chunk: rows straddle chunk boundaries and must still be counted exactly.
    big = tmp_path / "big.csv"
    big.write_bytes(b"phone,email\n" + b"x" * 1000 + (b"y" * 99 + b"\n") * 2000)  # first row is the x-run + y-line
    assert big.stat().st_size > (1 << 16)
    assert _count_rows(big) == 2000


def test_status_rows_for_header_only_export_is_zero(client, tenants):
    a, _ = tenants
    export = client.post(base(a.id) + "/export", headers=AUTH).json()
    assert all(i["rows"] == 0 for i in export["lists"])
    assert all(i["rows"] == 0 for i in client.get(base(a.id), headers=AUTH).json()["lists"])


def test_schedule_with_no_job_runs_is_all_null(client, tenants):
    a, _ = tenants
    sched = client.get(base(a.id), headers=AUTH).json()["schedule"]
    assert sched == {"job_name": AUDIENCE_EXPORT_JOB_NAME, "last_run_at": None, "last_run_ok": None,
                     "last_error_type": None, "next_due_at": None}


def test_schedule_after_failed_run_has_no_next_due(client, db_session, tenants):
    a, _ = tenants
    failed_at = utcnow() - timedelta(hours=3)
    db_session.add(JobRun(job_name=AUDIENCE_EXPORT_JOB_NAME, started_at=failed_at, finished_at=failed_at, ok=False,
                          error_type="OperationalError"))
    db_session.commit()
    sched = client.get(base(a.id), headers=AUTH).json()["schedule"]
    assert sched["last_run_ok"] is False
    assert sched["last_error_type"] == "OperationalError"
    assert sched["last_run_at"] is not None
    assert sched["next_due_at"] is None  # nothing has ever succeeded, so nothing is scheduled from it


def test_schedule_after_success_is_due_seven_days_after_finish(client, db_session, tenants):
    from datetime import datetime

    a, _ = tenants
    finished = utcnow() - timedelta(days=1)
    db_session.add(JobRun(job_name=AUDIENCE_EXPORT_JOB_NAME, started_at=finished - timedelta(minutes=5),
                          finished_at=finished, ok=True))
    db_session.commit()
    sched = client.get(base(a.id), headers=AUTH).json()["schedule"]
    assert sched["last_run_ok"] is True and sched["last_error_type"] is None
    due = datetime.fromisoformat(sched["next_due_at"]).replace(tzinfo=None)
    assert due == finished.replace(tzinfo=None) + timedelta(days=7)


def test_failure_after_success_keeps_next_due_from_last_success(client, db_session, tenants):
    from datetime import datetime

    a, _ = tenants
    ok_at = utcnow() - timedelta(days=8)
    db_session.add(JobRun(job_name=AUDIENCE_EXPORT_JOB_NAME, started_at=ok_at, finished_at=ok_at, ok=True))
    db_session.add(JobRun(job_name=AUDIENCE_EXPORT_JOB_NAME, started_at=utcnow(), finished_at=utcnow(), ok=False,
                          error_type="RuntimeError"))
    db_session.commit()
    sched = client.get(base(a.id), headers=AUTH).json()["schedule"]
    assert sched["last_run_ok"] is False
    due = datetime.fromisoformat(sched["next_due_at"]).replace(tzinfo=None)
    assert due == ok_at.replace(tzinfo=None) + timedelta(days=7)


def test_symlink_pointing_outside_export_dir_is_not_exported(client, tenants, tmp_path):
    a, _ = tenants
    base_dir = Path(os.environ["AUDIENCE_EXPORT_DIR"])
    base_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.csv"
    outside.write_text("phone,email\nsecret,secret\n")
    (base_dir / f"{a.id}_exclude_refusers.csv").symlink_to(outside)
    assert client.get(base(a.id) + "/exclude_refusers.csv", headers=AUTH).status_code == 404
    items = by_name(client.get(base(a.id), headers=AUTH).json())
    assert items["exclude_refusers"]["rows"] is None and items["exclude_refusers"]["size_bytes"] is None


@pytest.mark.parametrize("path", [
    "/..%2Fx.csv",
    "/..%2F..%2Fetc%2Fpasswd.csv",
    "/exclude_refusers.txt",
    "/exclude_refusers.csv.csv",
    "/exclude_refusers%2F..%2Fseed_delivered_buyers.csv",
])
def test_names_outside_the_fixed_list_are_404(client, tenants, path):
    a, _ = tenants
    write_lists(a.id)
    resp = client.get(base(a.id) + path, headers=AUTH)
    assert resp.status_code == 404
    assert "phone" not in resp.text  # no file contents leak through an error body


def test_revoked_tenant_key_is_401_on_status_and_download(client, db_session, tenants):
    from src.ameen_workforce.tenant_auth import revoke_tenant_key

    a, _ = tenants
    write_lists(a.id)
    row, key = create_tenant_key(db_session, a.id, scopes="stats:read audiences:read")
    hdr = {"Authorization": f"Bearer {key}"}
    assert client.get(base(a.id), headers=hdr).status_code == 200
    revoke_tenant_key(db_session, row.id)
    db_session.commit()
    assert client.get(base(a.id), headers=hdr).status_code == 401
    assert client.get(base(a.id) + "/exclude_refusers.csv", headers=hdr).status_code == 401


def test_download_leaks_no_file_path_outside_the_filename(client, tenants, tmp_path):
    a, _ = tenants
    write_lists(a.id)
    resp = client.get(base(a.id) + "/seed_delivered_buyers.csv", headers=AUTH)
    assert resp.status_code == 200
    export_dir = str(Path(os.environ["AUDIENCE_EXPORT_DIR"]).resolve())
    for name, value in resp.headers.items():
        assert export_dir not in value and str(tmp_path) not in value, name
        if name != "content-type":  # text/csv is the only header value that legitimately has a slash
            assert "/" not in value and "\\" not in value, name
    disposition = resp.headers["content-disposition"]
    assert disposition == f'attachment; filename="{a.id}_seed_delivered_buyers.csv"'
    assert disposition.count("filename=") == 1 and disposition.count("/") == 0
