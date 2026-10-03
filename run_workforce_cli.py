#!/usr/bin/env python3
"""
Ameen Digital AI Workforce — Interactive Operational CLI
=========================================================
Target Domain: https://employees.motahai.com
Umbrella:      Ameen Digital (agency.motahai.com)
Platform:      Wesam.ai Platform
Hub / Sync:    Hermes Autonomous AI Agent (https://hermes.motahai.com)
"""

import asyncio
import json
import sys
import time
from typing import Optional

from src.ameen_workforce.models import AgentRole, TaskStatus, TaskItem
from src.ameen_workforce.hitl_escalation import hitl_manager
from src.ameen_workforce.workflow_engine import workforce_engine
from src.ameen_workforce.hermes_bridge import hermes_bridge
from src.ameen_workforce.config import settings

def print_banner():
    print("""
================================================================================
   AMEEN DIGITAL AI WORKFORCE — AUTONOMOUS MULTI-AGENT SWARM
   Portal:  https://employees.motahai.com
   Agency:  https://agency.motahai.com
   Hub:     https://hermes.motahai.com
   Engine:  Wesam.ai Orchestrator | Egyptian PDPL 151/2020 Compliant
================================================================================
""")

async def run_tracking_audit():
    domain = input("\nEnter client domain to audit (default: https://client.motahai.com): ").strip()
    if not domain:
        domain = "https://client.motahai.com"
        
    print(f"\n[+] Dispatching Lead GTM Orchestrator & QA Network Sniffer for: {domain}")
    task = workforce_engine.create_task(
        title=f"Autonomous GTM Tracking & PDPL Audit for {domain}",
        description="Inspect dataLayer, consent mode v2, and beacon traffic",
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR,
        client_domain=domain
    )
    result = await workforce_engine.execute_task(task)
    print("\n--- Audit Results ---")
    print(json.dumps(result.output_data, indent=2))
    print(f"Task Status: {result.status.value}")

async def run_capi_check():
    print("\n[+] Dispatching Pixel & CAPI Specialist for deduplication check...")
    task = workforce_engine.create_task(
        title="Meta Pixel & CAPI Deduplication Parity Check",
        description="Verify event_id collision and double-firing beacons",
        assigned_agent=AgentRole.PIXEL_CAPI_SPECIALIST
    )
    result = await workforce_engine.execute_task(task)
    print("\n--- CAPI Parity Results ---")
    print(json.dumps(result.output_data, indent=2))
    print(f"Task Status: {result.status.value}")

async def run_gtm_sanitation():
    print("\n[+] Dispatching Auto-Fix Engineer for GTM container sanitation...")
    task = workforce_engine.create_task(
        title="Sanitize GTM Container & Scrub PII",
        description="Enforce zero-PII regex and standardize EGP variables",
        assigned_agent=AgentRole.AUTO_FIX_ENGINEER
    )
    result = await workforce_engine.execute_task(task)
    print("\n--- Sanitation Results ---")
    print(json.dumps(result.output_data, indent=2))
    print(f"Task Status: {result.status.value}")

async def test_hitl_escalation(is_payment: bool):
    if is_payment:
        title = "Authorize client invoice payment of 25,000 EGP"
        desc = "Payout for enterprise tracking container release"
        req_pay = True
        req_conf = False
    else:
        title = "Sign client mutual NDA and legal terms"
        desc = "Review confidential terms before onboarding"
        req_pay = False
        req_conf = True

    print(f"\n[!] Simulating Sensitive Action: '{title}'")
    task = workforce_engine.create_task(
        title=title,
        description=desc,
        assigned_agent=AgentRole.LEAD_GTM_ORCHESTRATOR,
        requires_payment_review=req_pay,
        requires_confidential_review=req_conf
    )
    result = await workforce_engine.execute_task(task)
    print("\n--- HITL Escalation Status ---")
    print(f"Task Status: {result.status.value}")
    if result.escalation:
        print(f"Escalation ID: {result.escalation.escalation_id}")
        print(f"Category:      {result.escalation.category.value}")
        print(f"Reason:        {result.escalation.reason}")
        print(f"Alert State:   DISPATCHED TO SUPERVISOR / HERMES HUB")

async def review_and_resolve_escalations():
    pending = hitl_manager.get_pending_escalations()
    if not pending:
        print("\n[i] No pending escalations. All tasks running normally.")
        return

    print(f"\n--- Pending Supervisor Escalations ({len(pending)}) ---")
    for idx, e in enumerate(pending, 1):
        print(f"[{idx}] ID: {e.escalation_id} | Agent: {e.agent_role.value} | Category: {e.category.value}")
        print(f"    Reason: {e.reason}")
        print(f"    Task:   {e.details.get('task_title')}")

    choice = input("\nEnter escalation number to resolve (or Enter to cancel): ").strip()
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(pending):
        return

    esc = pending[int(choice) - 1]
    action = input("Approve (A) or Reject (R)? [A/r]: ").strip().lower()
    approved = action != "r"
    supervisor = input("Enter supervisor ID/name: ").strip() or "SUPERVISOR"
    note = input("Enter approval/rejection note: ").strip() or "Reviewed by human supervisor"

    resolved = hitl_manager.resolve_escalation(
        escalation_id=esc.escalation_id,
        approved=approved,
        supervisor_id=supervisor,
        note=note
    )
    print(f"\n[OK] Escalation {esc.escalation_id} resolved as: {resolved.status}")

async def test_hermes_connection():
    print(f"\n[+] Testing connectivity to Hermes Agent at {settings.HERMES_BASE_URL}...")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            res = await client.get(f"{settings.HERMES_BASE_URL}/health")
            print(f"HTTP Status: {res.status_code}")
            print(f"Response:    {res.text}")
    except Exception as e:
        print(f"Connection test failed: {e}")

async def main():
    print_banner()
    while True:
        print("""
Operational Menu:
1. Run Autonomous Tracking & PDPL 151/2020 Audit (Lead GTM Orchestrator)
2. Run Meta CAPI vs Browser Deduplication Check (Pixel & CAPI Specialist)
3. Run GTM Container Sanitation & Release (Auto-Fix Engineer)
4. Test HITL Confidentiality Escalation (NDA / Legal Review)
5. Test HITL Payment Escalation (Billing / Invoice Review)
6. Review & Resolve Pending Supervisor Escalations
7. Check Hermes Hub Connectivity (hermes.motahai.com)
8. Exit
""")
        choice = input("Select an option (1-8): ").strip()
        if choice == "1":
            await run_tracking_audit()
        elif choice == "2":
            await run_capi_check()
        elif choice == "3":
            await run_gtm_sanitation()
        elif choice == "4":
            await test_hitl_escalation(is_payment=False)
        elif choice == "5":
            await test_hitl_escalation(is_payment=True)
        elif choice == "6":
            await review_and_resolve_escalations()
        elif choice == "7":
            await test_hermes_connection()
        elif choice == "8":
            print("\nExiting Ameen Digital AI Workforce CLI.")
            break
        else:
            print("Invalid option. Please try again.")

if __name__ == "__main__":
    asyncio.run(main())
