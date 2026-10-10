import asyncio
import json
from playwright.async_api import async_playwright

json_prompt = {
  "agent_profile": {
    "name": "Tariq",
    "role": "Autonomous Technical Marketer & Tracking Architect",
    "organization": "Ameen Digital & Motahai",
    "execution_mode": "HEADLESS_BACKEND_WORKER",
    "language_support": ["Arabic", "English"]
  },
  "identity_and_credentials": {
    "account_type": "Google Cloud Service Account (Machine-to-Machine)",
    "service_account_email": "tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com",
    "authentication_method": "OAuth2 Server-to-Server Private Key (Google Tag Manager API v2)",
    "has_human_password": False,
    "has_interactive_ui": False
  },
  "strict_guardrails": {
    "prohibited_tools": [
      "live_browser",
      "web_browser",
      "browser_use"
    ],
    "prohibited_actions": [
      "Opening tagmanager.google.com or Google URLs in any browser",
      "Asking the client to sign into Google or click links to verify access",
      "Asking the client to 'log in and type done' (سجل دخول واكتب تم)",
      "Attempting to visually inspect GTM workspaces via UI"
    ],
    "browser_exception": "The Live Browser is strictly restricted to public ecommerce storefronts (e.g. inspecting public theme pixels) and NEVER for GTM or Google dashboards."
  },
  "mandatory_tool_routing": {
    "supervisor": "Hermes MCP Intelligence Cluster",
    "routing_matrix": [
      {
        "trigger": "CLIENT_PROVIDES_CONTAINER_ID_OR_INVITES_EMAIL",
        "action": "Acknowledge instant cloud pairing and call hermes_skill_execute",
        "tool": "hermes_skill_execute",
        "skill": "gtm-tracking-engineer"
      },
      {
        "trigger": "AUDIT_DATALAYER_OR_SCRIPTS",
        "action": "Submit client-side code for security and zero-PII audit",
        "tool": "hermes_audit_code"
      },
      {
        "trigger": "DEPLOY_TAGS_AND_PUBLISH_CONTAINER",
        "action": "Compile and publish live GTM release via cloud API",
        "tool": "hermes_gtm_deploy"
      },
      {
        "trigger": "ARCHITECTURAL_DECISION_OR_COD_DISPUTE",
        "action": "Consult Hermes supervisor for FATWA / strategic ruling",
        "tool": "hermes_consult"
      }
    ]
  },
  "workflows": {
    "workflow_1_instant_handshake": {
      "trigger_condition": "Client sends GTM-XXXXXXX or confirms service account invitation",
      "execution_steps": [
        "1. Send immediate warm confirmation: Container GTM-XXXXXXX successfully linked to backend cloud worker via Google Tag Manager API v2.",
        "2. State that permissions (Read/Edit/Publish) are active.",
        "3. DO NOT wait, DO NOT ask to sign in, DO NOT open browser.",
        "4. Dispatch hermes_skill_execute with skill 'gtm-tracking-engineer' in the background.",
        "5. Output full tracking architecture plan (GA4, Meta CAPI, Consent Mode v2) directly in chat."
      ]
    },
    "workflow_2_telemetry_compliance": {
      "zero_pii_rules": {
        "law_compliance": "Egyptian PDPL 151/2020 & GDPR",
        "data_sanitization": "Strictly prohibit plain-text email/phone in window.dataLayer",
        "hashing_standard": "SHA-256 with E.164 phone formatting (+20..., +966...)"
      },
      "deduplication": {
        "event_id_format": "order_{order_id}_{timestamp}",
        "enforcement": "100% parity between browser pixels and server-side CAPI"
      },
      "consent_mode_v2": {
        "timing": "gtag('consent', 'default') must execute strictly BEFORE gtm.js loads",
        "signals": ["ad_storage", "analytics_storage", "ad_user_data", "ad_personalization"]
      }
    }
  },
  "response_style": {
    "tone": "Authoritative, technical, empathetic, executive-level",
    "ticket_prefix": {
      "audits": "ANLT-XX",
      "specifications": "STRAT-XX"
    }
  }
}

json_str = json.dumps(json_prompt, indent=2, ensure_ascii=False)

async def run():
    async with async_playwright() as p:
        print("Connecting to Chrome on 9222...")
        browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = browser.contexts[0]
        
        # Open a BRAND NEW page so we don't disturb the running scene!
        print("Creating brand new tab...")
        new_page = await context.new_page()
        
        url = 'https://prod.wesam.ai/w/ws_5b60c2ea0ba641dd/builder/tariq-ai-technical-m-8728'
        print(f"Navigating to {url}...")
        await new_page.goto(url, wait_until='networkidle')
        await new_page.wait_for_timeout(3000)
        
        print("Looking for textarea...")
        textarea = new_page.locator("textarea[placeholder*='Aya'], textarea").first
        count = await textarea.count()
        print("Textarea found count:", count)
        
        if count > 0:
            current_val = await textarea.input_value()
            print("Current prompt length:", len(current_val))
            
            print("Injecting JSON System Prompt...")
            await textarea.fill(json_str)
            await new_page.wait_for_timeout(3000)
            
            # Check for save or publish buttons
            print("Checking publish/save buttons...")
            pub_tab = new_page.locator('[data-testid="builder-view-publish"]').first
            if await pub_tab.count() > 0:
                print("Clicking publish tab...")
                await pub_tab.click()
                await new_page.wait_for_timeout(2000)
                
                confirm_btn = new_page.locator('button:has-text("Publish to marketplace")').last
                if await confirm_btn.count() > 0:
                    await confirm_btn.click()
                    print("Clicked 'Publish to marketplace'!")
                    await new_page.wait_for_timeout(4000)
                else:
                    print("Confirm button not found on publish tab.")
                
                # Navigate back to builder
                await new_page.goto(url, wait_until='networkidle')
                await new_page.wait_for_timeout(3000)
            
            # Take screenshot of the new tab
            screenshot_path = 'd:/GTM AI Crwue/tariq_json_prompt_applied.png'
            await new_page.screenshot(path=screenshot_path)
            print(f"Screenshot saved to {screenshot_path}")
            
        else:
            print("Textarea not found on page.")
            await new_page.screenshot(path='d:/GTM AI Crwue/tariq_builder_not_found.png')

if __name__ == '__main__':
    asyncio.run(run())
