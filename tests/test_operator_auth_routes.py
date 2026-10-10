"""Issue #74: the task/escalation routes require the operator bearer key (auth runs before body validation)."""

import httpx
import pytest

from src.ameen_workforce.service import app
from src.ameen_workforce.config import settings
from src.ameen_workforce.models import AgentRole

OP_KEY = "operator-key-123"
HERMES_KEY = "hermes-key-456"

ROUTES = [
    ("POST", "/tasks", {"title": "t", "description": "d", "assigned_agent": AgentRole.QA_NETWORK_SNIFFER.value}),
    ("GET", "/tasks/nope", None),
    ("GET", "/escalations", None),
    ("POST", "/escalations/nope/resolve", {"approved": True, "supervisor_id": "S", "note": "n"}),
]


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_KEY", OP_KEY)
    monkeypatch.setattr(settings, "HERMES_API_KEY", HERMES_KEY)


async def _call(method, path, body, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, json=body, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ROUTES)
@pytest.mark.parametrize("token", [None, "wrong-key", HERMES_KEY])
async def test_rejected_without_operator_key(method, path, body, token):
    assert (await _call(method, path, body, token)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("POST", "/tasks"), ("POST", "/escalations/x/resolve")])
async def test_auth_runs_before_body_validation(method, path):
    assert (await _call(method, path, {}, None)).status_code == 401


@pytest.mark.asyncio
async def test_operator_key_succeeds(monkeypatch):
    async def mock_send(*a, **k):
        return True
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_state_update", mock_send)
    r = await _call("POST", "/tasks", ROUTES[0][2], OP_KEY)
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert (await _call("GET", f"/tasks/{tid}", None, OP_KEY)).status_code == 200
    assert (await _call("GET", "/escalations", None, OP_KEY)).status_code == 200
    # authenticated request reaches the handler (404 = unknown escalation, not 401)
    assert (await _call("POST", "/escalations/nope/resolve", ROUTES[3][2], OP_KEY)).status_code == 404
