"""
API Service & Integration Tests (Async with httpx.ASGITransport)
"""

import pytest
import httpx
from src.ameen_workforce.service import app
from src.ameen_workforce.models import AgentRole

@pytest.mark.asyncio
async def test_health_check_endpoint():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "online"
        assert data["domain"] == "https://employees.motahai.com"
        assert data["orchestrator"] == "Wesam.ai Platform"

@pytest.mark.asyncio
async def test_root_endpoint():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "Ameen Digital" in data["workforce"]
        assert len(data["active_employees"]) == 8

OP_KEY = "test-operator-key"
OP_HEADERS = {"Authorization": f"Bearer {OP_KEY}"}


@pytest.mark.asyncio
async def test_create_standard_task(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", OP_KEY)
    async def mock_send(*args, **kwargs):
        return True
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_state_update", mock_send)

    payload = {
        "title": "Check GA4 Event Tags",
        "description": "Verify tag trigger conditions",
        "assigned_agent": AgentRole.QA_NETWORK_SNIFFER.value
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tasks", json=payload, headers=OP_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "COMPLETED"
        assert data["assigned_agent"] == AgentRole.QA_NETWORK_SNIFFER.value

@pytest.mark.asyncio
async def test_create_task_triggering_hitl(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", OP_KEY)
    async def mock_send(*args, **kwargs):
        return True
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_state_update", mock_send)
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_escalation_alert", mock_send)

    payload = {
        "title": "Sign client NDA and execute payment authorization",
        "description": "Confidential agreement requiring supervisor review",
        "assigned_agent": AgentRole.LEAD_GTM_ORCHESTRATOR.value,
        "requires_payment_review": True
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tasks", json=payload, headers=OP_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "AWAITING_HITL_APPROVAL"
        assert data["escalation"] is not None
        esc_id = data["escalation"]["escalation_id"]

        # Verify escalation appears in list
        esc_res = await client.get("/escalations", headers=OP_HEADERS)
        esc_list = esc_res.json()
        assert any(e["escalation_id"] == esc_id for e in esc_list)

        # Resolve escalation
        resolve_res = await client.post(f"/escalations/{esc_id}/resolve", json={
            "approved": True,
            "supervisor_id": "SUPERVISOR-AMYN",
            "note": "Verified payment transaction and NDA terms manually"
        }, headers=OP_HEADERS)
        assert resolve_res.status_code == 200
        res_data = resolve_res.json()
        assert res_data["status"] == "RESOLVED"
        assert res_data["escalation"]["status"] == "APPROVED"
