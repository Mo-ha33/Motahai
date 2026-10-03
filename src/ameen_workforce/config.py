"""
Ameen Digital AI Workforce — Configuration Module
==================================================
Target Domain: https://employees.motahai.com
Orchestration: Wesam.ai Platform
Agency:        https://agency.motahai.com
Hermes Hub:    https://hermes.motahai.com
"""

import os
from pydantic import BaseModel, Field

class Settings(BaseModel):
    # Brand & Domain Topology
    BRAND_NAME: str = "Ameen Digital AI Employees"
    AGENCY_DOMAIN: str = "https://agency.motahai.com"
    EMPLOYEES_PORTAL: str = "https://employees.motahai.com"
    WESAM_DNS_TARGET: str = os.getenv("WESAM_DNS_TARGET", "cname.wesam.ai")
    
    # Hermes Integration
    HERMES_BASE_URL: str = os.getenv("HERMES_BASE_URL", "https://hermes.motahai.com")
    HERMES_API_KEY: str = os.getenv("HERMES_API_KEY", "")
    HERMES_WEBHOOK_URL: str = os.getenv("HERMES_WEBHOOK_URL", "https://hermes.motahai.com/webhook/agency")
    HERMES_TRIGGERS_URL: str = os.getenv("HERMES_TRIGGERS_URL", "https://hermes.motahai.com/v1/triggers")
    
    # Local Service Binding
    HOST: str = os.getenv("AMEEN_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("AMEEN_PORT", "58770"))
    
    # HITL Escalation Thresholds & Keywords
    CONFIDENTIAL_KEYWORDS: list[str] = [
        "confidential", "nda", "legal", "lawsuit", "contract signature",
        "tax audit", "bank account", "password", "private key"
    ]
    PAYMENT_KEYWORDS: list[str] = [
        "payment", "invoice", "refund", "credit card", "charge",
        "payout", "wire transfer", "budget increase", "billing authorization"
    ]
    
    # Timeout Settings (Seconds)
    HTTP_TIMEOUT_SECONDS: float = 30.0
    ESCALATION_TIMEOUT_SECONDS: float = 86400.0  # 24 hours max wait for supervisor approval

settings = Settings()
