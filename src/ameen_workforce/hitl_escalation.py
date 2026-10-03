"""
Ameen Digital AI Workforce — Human-in-the-Loop (HITL) Escalation Engine
========================================================================
Implements Operating Principle 2:
- Works shoulder-to-shoulder with the human team.
- Halts execution and dispatches immediate escalation alerts if a task involves:
    1. Confidential decisions or sensitive data
    2. Payment, billing, or financial authorization
    3. Ambiguous situations requiring human subjective judgment
"""

import time
import uuid
import logging
from typing import Optional, Dict, Any, Tuple, List
from .models import TaskItem, TaskStatus, EscalationNotice, EscalationCategory, AgentRole
from .config import settings

logger = logging.getLogger("ameen_workforce.hitl")

class HitlEscalationManager:
    def __init__(self):
        self._active_escalations: Dict[str, EscalationNotice] = {}

    def evaluate_task_for_escalation(self, task: TaskItem) -> Tuple[bool, Optional[EscalationNotice]]:
        """
        Evaluates a task against escalation rules.
        Returns:
            (requires_escalation, escalation_notice)
        """
        combined_text = f"{task.title} {task.description} {str(task.input_data)}".lower()

        import re

        # 1. Payment Verification Check
        if task.requires_payment_review or any(re.search(rf"\b{re.escape(kw)}\b", combined_text) for kw in settings.PAYMENT_KEYWORDS):
            notice = self._create_notice(
                task=task,
                category=EscalationCategory.PAYMENT_VERIFICATION,
                reason="Task requires payment, refund, billing authorization, or financial verification."
            )
            return True, notice

        # 2. Confidentiality & Legal Decision Check
        if task.requires_confidential_review or any(re.search(rf"\b{re.escape(kw)}\b", combined_text) for kw in settings.CONFIDENTIAL_KEYWORDS):
            notice = self._create_notice(
                task=task,
                category=EscalationCategory.CONFIDENTIAL_DECISION,
                reason="Task involves confidential records, NDA/legal commitments, or private credentials."
            )
            return True, notice

        # 3. High-Risk Human Judgment (e.g., container deletion, mass changes)
        if "delete container" in combined_text or "purge tags" in combined_text:
            notice = self._create_notice(
                task=task,
                category=EscalationCategory.HUMAN_JUDGMENT_REQUIRED,
                reason="Irreversible production modification requested requiring supervisor sign-off."
            )
            return True, notice

        return False, None

    def _create_notice(self, task: TaskItem, category: EscalationCategory, reason: str) -> EscalationNotice:
        esc_id = f"ESC-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        notice = EscalationNotice(
            escalation_id=esc_id,
            task_id=task.task_id,
            agent_role=task.assigned_agent,
            category=category,
            reason=reason,
            details={
                "task_title": task.title,
                "client_name": task.client_name,
                "input_snapshot": task.input_data
            }
        )
        self._active_escalations[esc_id] = notice
        task.status = TaskStatus.AWAITING_HITL_APPROVAL
        task.escalation = notice
        logger.warning(
            "AUTOMATED EXECUTION HALTED [HITL Alert]: Task %s escalated (%s) - %s",
            task.task_id, category.value, reason
        )
        return notice

    def resolve_escalation(self, escalation_id: str, approved: bool, supervisor_id: str, note: str) -> Optional[EscalationNotice]:
        """Called when a supervisor reviews and resolves the escalation."""
        if escalation_id not in self._active_escalations:
            return None

        notice = self._active_escalations[escalation_id]
        notice.status = "APPROVED" if approved else "REJECTED"
        notice.approved_by = supervisor_id
        notice.resolution_note = note
        notice.resolved_at = time.time()
        logger.info(
            "Escalation %s resolved by %s: %s (Note: %s)",
            escalation_id, supervisor_id, notice.status, note
        )
        return notice

    def get_pending_escalations(self) -> List[EscalationNotice]:
        return [e for e in self._active_escalations.values() if e.status == "PENDING_SUPERVISOR_ACTION"]

hitl_manager = HitlEscalationManager()
