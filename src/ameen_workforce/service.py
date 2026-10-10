"""
Ameen Digital AI Workforce — FastAPI Service Gateway
=====================================================
Target Domain: https://employees.motahai.com
Orchestration: Wesam.ai Platform
Umbrella:      Ameen Digital (agency.motahai.com)
Supervisor / Gateway: Hermes Agent (hermes.motahai.com)
"""

import hmac
import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException, Request, Response, status, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import settings
from .models import TaskItem, TaskStatus, AgentRole, EscalationNotice
from .hitl_escalation import hitl_manager
from .workflow_engine import workforce_engine
from .hermes_bridge import hermes_bridge
from .hitl_tokens import issue_publish_token, DEFAULT_TTL_SECONDS
from .db import init_db
from .webhook_routes import router as webhook_router
from .capture_routes import router as capture_router
from .tenant_key_routes import router as tenant_key_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [AmeenWorkforce] %(message)s")
logger = logging.getLogger("ameen_workforce.service")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Creates tables (idempotent). A DB problem must not take the whole gateway down: the webhook routes will
    # fail on their own and the non-persistent endpoints keep working.
    try:
        init_db()
    except Exception as exc:
        logger.error("Database initialisation failed: %s", type(exc).__name__)
    # The conversion scheduler runs in-app ONLY when MOTAHAI_RUN_SCHEDULER_IN_APP=1 (default off). Each uvicorn
    # worker would start its own loop (service runs --workers 2), duplicating sends. Production runs the
    # standalone daemon `python -m ameen_workforce.scheduler` (deployment/systemd/motahai-scheduler.service).
    scheduler_in_app = None
    if os.environ.get("MOTAHAI_RUN_SCHEDULER_IN_APP") == "1":
        from .scheduler import SchedulerRunner
        scheduler_in_app = SchedulerRunner()
        await scheduler_in_app.start()
    try:
        yield
    finally:
        if scheduler_in_app is not None:
            await scheduler_in_app.stop()

app = FastAPI(
    title="Ameen Digital AI Workforce Engine",
    description="Operational Engine for AI Employees under Ameen Digital (agency.motahai.com) on Wesam.ai Platform",
    version="1.0.0",
    lifespan=lifespan
)
app.include_router(webhook_router)
app.include_router(capture_router)
app.include_router(tenant_key_router)

# CORS Policy
ALLOWED_ORIGINS = [
    "https://employees.motahai.com",
    "https://agency.motahai.com",
    "https://hermes.motahai.com",
    "http://127.0.0.1:58770",
    "http://localhost:3000"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------
class CreateTaskRequest(BaseModel):
    title: str
    description: str
    assigned_agent: AgentRole
    client_name: Optional[str] = "Ameen Digital Client"
    client_domain: Optional[str] = None
    input_data: Optional[Dict[str, Any]] = None
    requires_payment_review: Optional[bool] = False
    requires_confidential_review: Optional[bool] = False

class ResolveEscalationRequest(BaseModel):
    approved: bool
    supervisor_id: str
    note: str

class GtmPublishApprovalRequest(BaseModel):
    container_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    workspace_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")

def require_operator(authorization: Optional[str] = Header(None)) -> None:
    """
    Authenticates a HUMAN operator for Rule D-003 approvals via env OPERATOR_API_KEY.
    This key is deliberately separate from HERMES_API_KEY / any agent or webhook credential:
    an agent that can call the tools must never be able to mint its own approval.
    Used as a dependency so authentication runs before request-body validation.
    Fails closed: if OPERATOR_API_KEY is unset (or equals HERMES_API_KEY) nobody is authenticated.
    """
    operator_key = os.environ.get("OPERATOR_API_KEY", "")
    if not operator_key:
        logger.error("OPERATOR_API_KEY is not configured; refusing all approval requests")
        raise HTTPException(status_code=401, detail="Operator authentication required")
    if settings.HERMES_API_KEY and hmac.compare_digest(operator_key.encode(), settings.HERMES_API_KEY.encode()):
        logger.error("OPERATOR_API_KEY must differ from HERMES_API_KEY; refusing all approval requests")
        raise HTTPException(status_code=401, detail="Operator authentication required")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented.encode(), operator_key.encode()):
        raise HTTPException(status_code=401, detail="Operator authentication required", headers={"WWW-Authenticate": "Bearer"})

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    return {
        "status": "online",
        "workforce": "Ameen Digital AI Employees",
        "domain": settings.EMPLOYEES_PORTAL,
        "agency": settings.AGENCY_DOMAIN,
        "orchestrator": "Wesam.ai Platform",
        "hermes_hub": settings.HERMES_BASE_URL,
        "timestamp": time.time()
    }

@app.get("/")
async def root():
    return {
        "workforce": settings.BRAND_NAME,
        "portal": settings.EMPLOYEES_PORTAL,
        "docs": f"{settings.EMPLOYEES_PORTAL}/health",
        "hitl_collaboration": "ACTIVE",
        "active_employees": [role.value for role in AgentRole]
    }

@app.post("/tasks", response_model=TaskItem)
async def create_and_run_task(req: CreateTaskRequest):
    """Creates and initiates execution of an AI Employee task."""
    task = workforce_engine.create_task(
        title=req.title,
        description=req.description,
        assigned_agent=req.assigned_agent,
        client_name=req.client_name or "Ameen Digital Client",
        client_domain=req.client_domain,
        input_data=req.input_data,
        requires_payment_review=bool(req.requires_payment_review),
        requires_confidential_review=bool(req.requires_confidential_review)
    )
    executed_task = await workforce_engine.execute_task(task)
    return executed_task

@app.get("/tasks/{task_id}", response_model=TaskItem)
async def get_task_status(task_id: str):
    if task_id not in workforce_engine.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return workforce_engine.tasks[task_id]

@app.get("/escalations", response_model=List[EscalationNotice])
async def list_pending_escalations():
    """Supervisor view: Lists all tasks halted pending human review."""
    return hitl_manager.get_pending_escalations()

@app.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: str, req: ResolveEscalationRequest):
    """Supervisor action: Approve or reject halted task."""
    resolved = hitl_manager.resolve_escalation(
        escalation_id=escalation_id,
        approved=req.approved,
        supervisor_id=req.supervisor_id,
        note=req.note
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="Escalation ID not found")

    # Find associated task
    task = workforce_engine.tasks.get(resolved.task_id)
    if task:
        if req.approved:
            # Resume execution
            task.requires_payment_review = False
            task.requires_confidential_review = False
            # Strip sensitive keywords from title/description to prevent loop
            task = await workforce_engine.execute_task(task)
        else:
            task.status = TaskStatus.FAILED
            task.output_data = {"error": f"Rejected by Supervisor ({req.supervisor_id}): {req.note}"}
            await hermes_bridge.send_state_update("TASK_SUPERVISOR_REJECTED", task)

    return {
        "status": "RESOLVED",
        "escalation": resolved,
        "task_state": task.status if task else None
    }

@app.post("/approvals/gtm-publish")
async def approve_gtm_publish(req: GtmPublishApprovalRequest, _operator: None = Depends(require_operator)):
    """
    Rule D-003: a human operator mints a signed, single-use approval token for ONE container + workspace.
    The token is then handed to the publish tool (hitl_approval_token). Operator bearer key required.
    """
    try:
        token = issue_publish_token(req.container_id, req.workspace_id, ttl_seconds=DEFAULT_TTL_SECONDS)
    except RuntimeError as exc:
        logger.error("Cannot issue approval token: %s", exc)
        raise HTTPException(status_code=503, detail="Approval signing is not configured")
    logger.info("D-003 approval issued for container=%s workspace=%s", req.container_id, req.workspace_id)
    return {
        "approval_token": token,
        "gate": "D-003",
        "container_id": req.container_id,
        "workspace_id": req.workspace_id,
        "expires_in_seconds": DEFAULT_TTL_SECONDS,
        "single_use": True
    }

@app.post("/webhook/hermes")
async def receive_from_hermes(request: Request, authorization: Optional[str] = Header(None)):
    """Receives task dispatches from Hermes Autonomous Agent."""
    body = await request.json()
    logger.info("Received dispatch from Hermes: %s", body.get("event_type"))
    
    # Process event
    event_type = body.get("event_type")
    payload = body.get("payload", {})
    
    task = workforce_engine.create_task(
        title=payload.get("title", f"Hermes Trigger: {event_type}"),
        description=payload.get("description", "Automated trigger from Hermes Agent"),
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR,
        client_name=payload.get("client_name", "Ameen Client"),
        client_domain=payload.get("client_domain"),
        input_data=payload
    )
    
    # Run async
    await workforce_engine.execute_task(task)
    return {"status": "ENQUEUED", "task_id": task.task_id}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
