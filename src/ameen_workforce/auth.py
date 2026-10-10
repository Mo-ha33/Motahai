"""
auth.py — Operator authentication dependency shared by service.py and the route modules.

Lives in its own module so route modules (e.g. stats_routes.py) can use it without importing service.py, which would be
circular (service.py includes their routers). service.py re-exports `require_operator` so existing imports keep working.
"""

import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException

from .config import settings

logger = logging.getLogger("ameen_workforce.service")


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
