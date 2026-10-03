"""
Unit tests for Human-in-the-Loop (HITL) Escalation Logic
"""

import pytest
from src.ameen_workforce.models import TaskItem, TaskStatus, AgentRole, EscalationCategory
from src.ameen_workforce.hitl_escalation import HitlEscalationManager

def test_standard_task_does_not_escalate():
    manager = HitlEscalationManager()
    task = TaskItem(
        task_id="T-001",
        title="Check standard GA4 pageview trigger",
        description="Verify pageview events are registered correctly",
        assigned_agent=AgentRole.QA_NETWORK_SNIFFER,
        input_data={"page": "/home"}
    )
    needs_esc, notice = manager.evaluate_task_for_escalation(task)
    assert not needs_esc
    assert notice is None
    assert task.status == TaskStatus.PENDING

def test_payment_verification_triggers_hitl_halt():
    manager = HitlEscalationManager()
    task = TaskItem(
        task_id="T-002",
        title="Authorize client invoice payment and refund",
        description="Process payment for GTM tracking setup fee",
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR,
        input_data={"amount_egp": 15000}
    )
    needs_esc, notice = manager.evaluate_task_for_escalation(task)
    assert needs_esc
    assert notice is not None
    assert notice.category == EscalationCategory.PAYMENT_VERIFICATION
    assert task.status == TaskStatus.AWAITING_HITL_APPROVAL

def test_confidential_decision_triggers_hitl_halt():
    manager = HitlEscalationManager()
    task = TaskItem(
        task_id="T-003",
        title="Review confidential NDA and contract terms",
        description="Review NDA terms before client onboarding",
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR,
        input_data={"document": "client_nda_v1.pdf"}
    )
    needs_esc, notice = manager.evaluate_task_for_escalation(task)
    assert needs_esc
    assert notice is not None
    assert notice.category == EscalationCategory.CONFIDENTIAL_DECISION
    assert task.status == TaskStatus.AWAITING_HITL_APPROVAL

def test_supervisor_resolution_lifecycle():
    manager = HitlEscalationManager()
    task = TaskItem(
        task_id="T-004",
        title="Verify invoice payout",
        description="Vendor payout review",
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR
    )
    _, notice = manager.evaluate_task_for_escalation(task)
    assert notice is not None
    
    # Verify in pending list
    pending = manager.get_pending_escalations()
    assert any(e.escalation_id == notice.escalation_id for e in pending)

    # Supervisor approves
    resolved = manager.resolve_escalation(
        escalation_id=notice.escalation_id,
        approved=True,
        supervisor_id="SUP-OPS-1",
        note="Approved after verifying client wire transfer"
    )
    assert resolved.status == "APPROVED"
    assert resolved.approved_by == "SUP-OPS-1"
    assert len(manager.get_pending_escalations()) == 0
