"""
Unit tests for Multi-Agent Workflow Engine
"""

import pytest
from src.ameen_workforce.models import TaskItem, TaskStatus, AgentRole, WorkflowGoal
from src.ameen_workforce.workflow_engine import WorkforceEngine

@pytest.mark.asyncio
async def test_gtm_container_sanitation_workflow(monkeypatch):
    # Mock hermes bridge to avoid network calls during tests
    async def mock_send(*args, **kwargs):
        return True
    
    engine = WorkforceEngine()
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_state_update", mock_send)
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_escalation_alert", mock_send)

    task = engine.create_task(
        title="Sanitize GTM container export",
        description="Scrub PII and apply Consent Mode v2",
        assigned_agent=AgentRole.AUTO_FIX_ENGINEER
    )

    executed = await engine.execute_task(task)
    assert executed.status == TaskStatus.COMPLETED
    assert executed.output_data["pii_regex_scrubbed"] is True
    assert executed.output_data["currency_standardized"] == "EGP"
    assert executed.output_data["consent_mode_v2_status"] == "enforced_default_denied"

@pytest.mark.asyncio
async def test_pixel_capi_deduplication_workflow(monkeypatch):
    async def mock_send(*args, **kwargs):
        return True

    engine = WorkforceEngine()
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_state_update", mock_send)

    task = engine.create_task(
        title="Verify Meta Pixel and CAPI deduplication",
        description="Check event_id parity",
        assigned_agent=AgentRole.PIXEL_CAPI_SPECIALIST
    )

    executed = await engine.execute_task(task)
    assert executed.status == TaskStatus.COMPLETED
    assert executed.output_data["deduplication_match_rate"] == "98.7%"
    assert executed.output_data["double_fires_detected"] == 0

@pytest.mark.asyncio
async def test_autonomous_tracking_audit_workflow(monkeypatch):
    async def mock_send(*args, **kwargs):
        return True

    async def mock_inspect(*args, **kwargs):
        return {
            "target_url": "https://store.motahai.com",
            "pdpl_151_2020_compliance": "PASS",
            "consent_mode_v2_active": True,
            "detected_gtm_containers": ["GTM-TEST123"],
            "scorecard": {
                "privacy_score": 98,
                "tracking_integrity": 95,
                "overall_grade": "A+"
            }
        }

    engine = WorkforceEngine()
    monkeypatch.setattr("src.ameen_workforce.workflow_engine.hermes_bridge.send_state_update", mock_send)
    monkeypatch.setattr("src.ameen_workforce.browser_service.sniffer_service.inspect_target_url", mock_inspect)

    task = engine.create_task(
        title="Full Egyptian PDPL 151/2020 & GTM Tracking Audit",
        description="Inspect window.dataLayer and network beacons",
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR,
        client_domain="https://store.motahai.com"
    )

    executed = await engine.execute_task(task)
    assert executed.status == TaskStatus.COMPLETED
    assert executed.output_data["pdpl_151_2020_compliance"] == "PASS"
    assert executed.output_data["certified_scorecard"]["privacy_score"] >= 90
