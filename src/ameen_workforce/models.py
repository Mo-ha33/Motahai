"""
Ameen Digital AI Workforce — Domain & State Models
===================================================
Defines strongly typed models for agents, tasks, telemetry, and HITL escalations.
"""

from enum import Enum
from typing import Dict, Any, Optional, List
import time
from pydantic import BaseModel, Field

class AgentRole(str, Enum):
    LEAD_GTM_ORCHESTRATOR = "Lead GTM Orchestrator"
    DATALAYER_ARCHITECT = "DataLayer Architect"
    PIXEL_CAPI_SPECIALIST = "Pixel & CAPI Specialist"
    QA_NETWORK_SNIFFER = "QA Network Sniffer"
    AUTO_FIX_ENGINEER = "Auto-Fix Engineer"
    GROWTH_BI_ANALYST = "Growth BI Analyst"
    GTM_STRATEGY_LEAD = "GTM Strategy & Acquisition Lead"
    CRO_ENGINEER = "CRO & Experimentation Engineer"

class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_HITL_APPROVAL = "AWAITING_HITL_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"

class EscalationCategory(str, Enum):
    PAYMENT_VERIFICATION = "PAYMENT_VERIFICATION"
    CONFIDENTIAL_DECISION = "CONFIDENTIAL_DECISION"
    HUMAN_JUDGMENT_REQUIRED = "HUMAN_JUDGMENT_REQUIRED"
    SECURITY_RISK = "SECURITY_RISK"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"

class EscalationNotice(BaseModel):
    escalation_id: str
    task_id: str
    agent_role: AgentRole
    category: EscalationCategory
    reason: str
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    status: str = "PENDING_SUPERVISOR_ACTION"
    resolution_note: Optional[str] = None
    approved_by: Optional[str] = None
    resolved_at: Optional[float] = None

class TaskItem(BaseModel):
    task_id: str
    title: str
    description: str
    assigned_agent: AgentRole
    client_name: Optional[str] = "Ameen Digital Client"
    client_domain: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output_data: Optional[Dict[str, Any]] = None
    requires_payment_review: bool = False
    requires_confidential_review: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    escalation: Optional[EscalationNotice] = None

class HermesTelemetryEvent(BaseModel):
    source: str = "https://employees.motahai.com"
    event_type: str
    agent: str
    task_id: str
    payload: Dict[str, Any]
    timestamp: float = Field(default_factory=time.time)

class WorkflowGoal(str, Enum):
    GTM_CONTAINER_SANITATION = "GTM Container Sanitation & Production Release"
    DOUBLE_FIRE_CAPI_CHECK = "Quick Double-Fire & CAPI Parity Health Check"
    AUTONOMOUS_TRACKING_AUDIT = "Autonomous GTM Tracking & Privacy Audit"
    CLIENT_ONBOARDING = "End-to-End Client Onboarding & Setup"
