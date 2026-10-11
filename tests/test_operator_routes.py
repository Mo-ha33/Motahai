"""M2-2 / M3-4: operator endpoints (manual order confirmation, audience export) and the weekly export job."""

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import operator_routes
from src.ameen_workforce.capi_service import MetaCAPISender
from src.ameen_workforce.db import CapiEvent, JobRun, Order, OrderStatusEvent, create_tenant
from src.ameen_workforce.scheduler import AUDIENCE_EXPORT_JOB_NAME, run_scheduler_tick
from src.ameen_workforce.service import app

KEY = "operator-secret-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def H(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class Sender(MetaCAPISender):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def send_event(self, pixel_id, access_token, payload):
        self.calls.append(payload)
        return {"status": "success", "fbtrace_id": "T1"}


@pytest.fixture
def factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)


@pytest.fixture
def sender():
    return Sender()


@pytest.fixture
def client(factory, sender, monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_API_KEY", KEY)
    monkeypatch.setenv(operator_routes.AUDIENCE_EXPORT_DIR_ENV, str(tmp_path / "aud"))
    app.dependency_overrides[operator_routes.get_session_factory_dep] = lambda: factory
    app.dependency_overrides[operator_routes.get_capi_sender_dep] = lambda: sender
    yield TestClient(app)
    app.dependency_overrides.pop(operator_routes.get_session_factory_dep, None)
    app.dependency_overrides.pop(operator_routes.get_capi_sender_dep, None)


def add_order(session, tenant, platform_id, status="pending", phone=None, events=()):
    order = Order(tenant_id=tenant.id, platform_order_id=platform_id, is_cod=True, value=100.0, currency="EGP",
                  current_status=status, phone_hash=phone, created_at=NOW - timedelta(days=5))
    session.add(order)
    session.flush()
    for ev in events:
        session.add(OrderStatusEvent(order_id=order.id, status=ev, source="webhook"))
    session.commit()
    return order


def confirmed_events(session, tenant):
    return session.scalars(select(CapiEvent).where(
        CapiEvent.tenant_id == tenant.id, CapiEvent.event_name == "ConfirmedOrder")).all()


CONFIRM = "/operator/orders/{}/{}/confirm"
EXPORT = "/operator/tenants/{}/audiences/export"


# --- auth -------------------------------------------------------------------------------------------------

def test_auth_required(client, shadow_tenant, monkeypatch):
    for url in (CONFIRM.format("shadow.myshopify.com", "1"), EXPORT.format("shadow.myshopify.com")):
        assert client.post(url).status_code == 401
        assert client.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    monkeypatch.delenv("OPERATOR_API_KEY")
    assert client.post(EXPORT.format("shadow.myshopify.com"), headers=AUTH).status_code == 401


# --- confirm ----------------------------------------------------------------------------------------------

def test_confirm_unknown_tenant_and_order_404(client, db_session, shadow_tenant):
    assert client.post(CONFIRM.format("nope.myshopify.com", "1"), headers=AUTH).status_code == 404
    assert client.post(CONFIRM.format("shadow.myshopify.com", "missing"), headers=AUTH).status_code == 404


def test_confirm_emits_once_and_is_idempotent(client, db_session, shadow_tenant):
    add_order(db_session, shadow_tenant, "A-1")
    first = client.post(CONFIRM.format("shadow.myshopify.com", "A-1"), headers=AUTH)
    assert first.status_code == 200
    assert first.json()["status"] == "confirmed"
    assert len(confirmed_events(db_session, shadow_tenant)) == 1
    second = client.post(CONFIRM.format("shadow.myshopify.com", "A-1"), headers=AUTH)
    assert second.status_code == 200
    assert second.json()["status"] == "already_confirmed"
    assert len(confirmed_events(db_session, shadow_tenant)) == 1
    order = db_session.scalar(select(Order).where(Order.platform_order_id == "A-1"))
    assert order.confirmation_source == "manual"


def test_confirm_cross_tenant_impossible(client, db_session, shadow_tenant):
    other = create_tenant(db_session, name="Other", platform="shopify", shop_domain="other.myshopify.com",
                          meta_dataset_id="999", settlement_hours=0)
    add_order(db_session, other, "X-7")
    res = client.post(CONFIRM.format("shadow.myshopify.com", "X-7"), headers=AUTH)
    assert res.status_code == 404
    assert confirmed_events(db_session, other) == [] and confirmed_events(db_session, shadow_tenant) == []
    # same order number in both tenants: only the URL tenant's order is touched
    add_order(db_session, shadow_tenant, "X-7")
    assert client.post(CONFIRM.format("shadow.myshopify.com", "X-7"), headers=AUTH).status_code == 200
    assert len(confirmed_events(db_session, shadow_tenant)) == 1
    assert confirmed_events(db_session, other) == []


# --- export -----------------------------------------------------------------------------------------------

def seed_audience_data(session, tenant):
    for i, phone in enumerate(("0100", "0100", "0111")):  # 0100 refused twice -> refuser
        add_order(session, tenant, f"R-{i}", status="cancelled", phone=H(phone), events=("pending", "shipped"))
    add_order(session, tenant, "D-1", status="delivered", phone=H("0122"), events=("pending", "shipped", "delivered"))


def test_export_unknown_tenant_404(client):
    assert client.post(EXPORT.format("nope.myshopify.com"), headers=AUTH).status_code == 404


def test_export_returns_counts_and_only_hashes(client, db_session, shadow_tenant):
    seed_audience_data(db_session, shadow_tenant)
    res = client.post(EXPORT.format("shadow.myshopify.com"), headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["exclude_refusers_count"] == 1 and body["seed_delivered_buyers_count"] == 1
    text = json.dumps(body)
    assert "0100" not in text and H("0100") not in text  # response carries no identifiers at all
    for path in (body["exclude_refusers_path"], body["seed_delivered_buyers_path"]):
        rows = list(csv.reader(open(path, encoding="utf-8")))
        assert len(rows) >= 2
        flat = " ".join(" ".join(r) for r in rows[1:])
        assert "0100" not in flat and "0122" not in flat
    assert H("0100") in open(body["exclude_refusers_path"]).read()


def test_export_cross_tenant_isolated(client, db_session, shadow_tenant):
    other = create_tenant(db_session, name="Other", platform="shopify", shop_domain="other.myshopify.com",
                          meta_dataset_id="999", settlement_hours=0)
    seed_audience_data(db_session, other)
    body = client.post(EXPORT.format("shadow.myshopify.com"), headers=AUTH).json()
    assert body["tenant_id"] == shadow_tenant.id
    assert body["exclude_refusers_count"] == 0 and body["seed_delivered_buyers_count"] == 0


# --- scheduler job ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduler_runs_export_per_active_tenant_weekly(factory, db_session, shadow_tenant, monkeypatch, tmp_path):
    monkeypatch.setenv(operator_routes.AUDIENCE_EXPORT_DIR_ENV, str(tmp_path / "aud"))
    other = create_tenant(db_session, name="Other", platform="shopify", shop_domain="other.myshopify.com",
                          meta_dataset_id="999", settlement_hours=0)
    off = create_tenant(db_session, name="Off", platform="shopify", shop_domain="off.myshopify.com",
                        meta_dataset_id="998", settlement_hours=0)
    off.active = False
    db_session.commit()
    seed_audience_data(db_session, other)
    summary = await run_scheduler_tick(session_factory=factory, now=NOW, sender=Sender(),
                                       digest_sender=lambda *a, **k: None)
    job = summary["jobs"][AUDIENCE_EXPORT_JOB_NAME]
    assert job["ok"] is True
    assert job["counts"]["tenants"] == 2 and job["counts"]["failed"] == 0
    assert job["counts"]["exclude_refusers"] == 1 and job["counts"]["seed_delivered_buyers"] == 1
    assert (tmp_path / "aud" / f"{other.id}_exclude_refusers.csv").exists()
    assert not (tmp_path / "aud" / f"{off.id}_exclude_refusers.csv").exists()
    runs = db_session.scalars(select(JobRun).where(JobRun.job_name == AUDIENCE_EXPORT_JOB_NAME)).all()
    assert len(runs) == 1 and runs[0].ok is True
    # not due again within a week; due after 7 days
    again = await run_scheduler_tick(session_factory=factory, now=NOW + timedelta(days=1), sender=Sender())
    assert AUDIENCE_EXPORT_JOB_NAME not in again["jobs"]
    later = await run_scheduler_tick(session_factory=factory, now=NOW + timedelta(days=7, minutes=1), sender=Sender())
    assert later["jobs"][AUDIENCE_EXPORT_JOB_NAME]["ok"] is True
