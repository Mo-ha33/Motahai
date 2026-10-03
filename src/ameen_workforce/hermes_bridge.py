"""
Ameen Digital AI Workforce — Hermes Agent Telemetry & Communication Bridge
===========================================================================
Implements Operating Principle 3:
- Communicates state updates, task telemetry, and HITL escalation alerts
  back to the Hermes Autonomous AI Agent backend at https://hermes.motahai.com.
"""

import time
import json
import logging
from typing import Dict, Any, Optional
import httpx
from .config import settings
from .models import TaskItem, EscalationNotice, HermesTelemetryEvent

logger = logging.getLogger("ameen_workforce.hermes_bridge")

class HermesBridge:
    def __init__(self, base_url: str = settings.HERMES_BASE_URL, api_key: str = settings.HERMES_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.triggers_url = f"{self.base_url}/v1/triggers"
        self.agency_webhook_url = f"{self.base_url}/webhook/agency"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Source": "employees.motahai.com",
            "User-Agent": "Ameen-Workforce-Engine/1.0"
        }

    async def send_state_update(self, event_type: str, task: TaskItem, extra: Optional[Dict[str, Any]] = None) -> bool:
        """Sends an operational state update to Hermes Agent."""
        payload = {
            "source": settings.EMPLOYEES_PORTAL,
            "event_type": event_type,
            "agent": task.assigned_agent.value,
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status.value,
            "client_name": task.client_name,
            "timestamp": time.time(),
            "details": extra or {}
        }

        req_body = {
            "source": "employees.motahai.com",
            "event_type": event_type,
            "payload": payload,
            "async_execution": True
        }

        try:
            async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS, verify=False) as client:
                res = await client.post(self.triggers_url, headers=self._get_headers(), json=req_body)
                if res.status_code in [200, 201, 202]:
                    logger.info("Successfully synced event '%s' for task %s to Hermes", event_type, task.task_id)
                    return True
                else:
                    logger.warning("Hermes rejected state update (HTTP %s): %s", res.status_code, res.text)
                    return False
        except Exception as e:
            logger.error("Failed to reach Hermes backend at %s: %s", self.triggers_url, e)
            return False

    async def send_escalation_alert(self, notice: EscalationNotice) -> bool:
        """
        Dispatches high-priority Human-in-the-Loop escalation alert to Hermes.
        Hermes can route this alert to Telegram, WhatsApp, or the Agency Dashboard.
        """
        alert_body = {
            "source": "employees.motahai.com",
            "event_type": "HITL_SUPERVISOR_ESCALATION",
            "payload": {
                "escalation_id": notice.escalation_id,
                "task_id": notice.task_id,
                "agent_role": notice.agent_role.value,
                "category": notice.category.value,
                "reason": notice.reason,
                "details": notice.details,
                "created_at": notice.created_at,
                "action_url": f"{settings.EMPLOYEES_PORTAL}/escalations/{notice.escalation_id}",
                "urgency": "HIGH"
            },
            "async_execution": True
        }

        try:
            async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS, verify=False) as client:
                res = await client.post(self.triggers_url, headers=self._get_headers(), json=alert_body)
                if res.status_code in [200, 201, 202]:
                    logger.info("Dispatched HITL escalation %s to Hermes successfully", notice.escalation_id)
                    return True
                else:
                    logger.error("Hermes returned error for escalation %s: %s", notice.escalation_id, res.text)
                    return False
        except Exception as e:
            logger.error("Network failure sending escalation %s to Hermes: %s", notice.escalation_id, e)
            return False

hermes_bridge = HermesBridge()
