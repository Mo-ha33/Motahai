import subprocess
import json
import sys

REPO = "Mo-ha33/Motahai"
PROJECT_NUM = 3
OWNER = "Mo-ha33"

# Definition of Epics and their child Stories
DATA = [
    {
        "epic": {
            "title": "[EPIC 1] Safety, Zero-Trust Governance & IAM Integrity",
            "body": """### Epic Overview
Enforce cryptographic human-in-the-loop (HITL) gates, eliminate synthetic mock fallbacks in production audits, and ensure 100% IAM service-account alignment between Google Cloud Console and codebase runtime.

### Key Success Criteria:
- Publishing live GTM tags requires non-replayable, scoped cryptographic approval token.
- Live audit scripts never return synthetic mock 200 OKs.
- GCP Service Account aligned to `tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com`.
""",
            "labels": ["epic", "p0-critical", "security"]
        },
        "stories": [
            {
                "title": "[STORY 1.1] Cryptographic HITL Approval Gate for Live GTM Deployments",
                "body": """### User Story
As a Marketing & Engineering Lead,
I want live GTM publish actions to strictly require an HMAC/Ed25519 cryptographic token created via an authenticated operator endpoint,
So that no autonomous agent or unauthorized script can deploy tags to production containers unreviewed.

### Acceptance Criteria:
- Live publish endpoint rejects any requests missing valid, signed operator token.
- Tokens are single-use, workspace-scoped, and expire in 30 minutes.
- Refusal messages never leak approval token format or bypass secrets.
- `hermes_gtm_deploy` is locked behind the same cryptographic gate.
""",
                "labels": ["story", "p0-critical", "security", "gtm"]
            },
            {
                "title": "[STORY 1.2] Eliminate Synthetic Fallbacks & Enforce Honest Audit Failures",
                "body": """### User Story
As a Tracking Auditor,
I want the tracking health scanner to report honest network failures instead of serving fake HTML mocks,
So that clients and team members receive genuine diagnostic truth.

### Acceptance Criteria:
- Remove mock HTML fallbacks in `src/ameen_workforce/browser_service.py`.
- Health checks return explicit error status when endpoints are unreachable.
- Synthetic metric indicators marked with `simulated: True` until real beacons are received.
""",
                "labels": ["story", "p0-critical", "security"]
            },
            {
                "title": "[STORY 1.3] Reconcile GCP IAM Service Account Across Codebase & Documentation",
                "body": """### User Story
As a DevOps Engineer,
I want the GCP service account in `tariq_service.py` to match the exact identity registered on Google Cloud Console (`agentic-ai-494313`),
So that credentials, permissions, and audit logs are seamless and error-free.

### Acceptance Criteria:
- Code references updated to `tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com`.
- Removal of deprecated `@motahai-tracking` references.
- All docs, handoff playbooks, and configs reflect the unified GCP identity.
""",
                "labels": ["story", "p0-critical", "security"]
            }
        ]
    },
    {
        "epic": {
            "title": "[EPIC 2] Client Asset Matrix & 1-Click Onboarding Delivery Sheet",
            "body": """### Epic Overview
Standardize client onboarding, credentials intake, and asset matrix handoff into clean, professional delivery sheets. Provide a streamlined 1-click Meta Partner sharing alternative alongside guided token onboarding.

### Key Success Criteria:
- Automated generation of structured Client Delivery Google Sheet / Excel.
- Meta Partner asset sharing workflow documentation and verification.
- Secure, validated client token & pixel intake pipeline.
""",
            "labels": ["epic", "p0-critical", "gtm"]
        },
        "stories": [
            {
                "title": "[STORY 2.1] Automated Client Asset Matrix & Tracking Delivery Sheet Generator",
                "body": """### User Story
As an Account Manager,
I want an automated tool that builds and validates the client tracking asset matrix (GTM IDs, Pixel IDs, GA4 Measurement IDs, webhook secrets),
So that the client receives an organized, verified delivery sheet upon setup completion.

### Acceptance Criteria:
- Generates structured asset matrix export with verification status.
- Validates syntax of GTM container IDs (GTM-XXXXXX), Meta Pixel IDs, and GA4 Measurement IDs.
- Outputs ready-to-share client delivery sheet format.
""",
                "labels": ["story", "p0-critical", "gtm"]
            },
            {
                "title": "[STORY 2.2] Meta Partner Agency Asset Sharing Onboarding Flow",
                "body": """### User Story
As an Agency Partner,
I want clients to have the option to grant pixel/dataset access via Meta Business Settings (Partner sharing) instead of manually copying access tokens,
So that non-technical merchants can onboard in under 2 minutes safely.

### Acceptance Criteria:
- Step-by-step partner request instructions embedded in onboarding UI/CLI.
- System detects shared dataset and tests CAPI connectivity without raw token pasting.
- Fallback guided manual token onboarding remains available for single-account stores.
""",
                "labels": ["story", "p0-critical", "capi"]
            }
        ]
    },
    {
        "epic": {
            "title": "[EPIC 3] Server-Side Meta CAPI & COD Reconciliation Engine (Rule D-005)",
            "body": """### Epic Overview
The core differentiator of Motahai: Server-side tracking for Cash On Delivery (COD) markets. Prevents ad budget destruction by enforcing Rule D-005: Never fire a `Purchase` event upon initial order placement if payment is COD. Only fire `DeliveredPurchase` upon verified delivery and cash collection.

### Key Success Criteria:
- Webhooks from Shopify & Salla verify cryptographic HMAC signatures.
- COD orders emit `OrderPlaced` while in transit; `DeliveredPurchase` only when marked paid + fulfilled.
- Deduplication keys (`event_id`) match between browser and server events.
- Advanced matching fields (phone, email, IP, user-agent) normalized with SHA-256 and E.164.
""",
            "labels": ["epic", "p0-critical", "capi", "cod-engine", "webhooks"]
        },
        "stories": [
            {
                "title": "[STORY 3.1] Meta Graph API v20.0 CAPI Transmitter with E.164 & SHA-256 Normalization",
                "body": """### User Story
As a Performance Marketer,
I want server-side conversion payloads transmitted to Meta CAPI v20.0 with standardized SHA-256 hashing and phone normalization (+966, +20, +971),
So that Event Match Quality (EMQ) scores exceed 8.0/10.

### Acceptance Criteria:
- Phone numbers converted to E.164 format and hashed with SHA-256.
- Email, names, cities, and zip codes normalized (lowercase, trimmed) and hashed.
- Events older than 7 days rejected before dispatch to avoid batch rejections.
- Access token transmitted securely in request body, never in URL query strings or logs.
""",
                "labels": ["story", "p0-critical", "capi"]
            },
            {
                "title": "[STORY 3.2] Implement Rule D-005: COD OrderPlaced vs DeliveredPurchase Lifecycle",
                "body": """### User Story
As an E-Commerce Merchant running COD,
I want orders placed via Cash on Delivery held until physical courier delivery before firing `Purchase` to Meta,
So that Meta ad algorithms optimize for completed cash transactions instead of fake or refused orders.

### Acceptance Criteria:
- Initial COD checkout emits `OrderPlaced` custom event.
- Shopify `fulfilled` status treated as shipment, not delivery.
- `DeliveredPurchase` fired with `event_id = delivered_<order_id>` only when order is paid + fulfilled.
- Cancelled or returned orders held and never converted to `Purchase`.
""",
                "labels": ["story", "p0-critical", "cod-engine", "capi"]
            },
            {
                "title": "[STORY 3.3] Zero-Effort Webhook Ingestion with HMAC Verification (Shopify & Salla)",
                "body": """### User Story
As a Systems Architect,
I want secure webhook listener endpoints for Shopify and Salla that verify cryptographic HMAC headers,
So that fraudulent or unverified incoming payloads cannot trigger fake conversion events.

### Acceptance Criteria:
- Shopify route verifies `X-Shopify-Hmac-Sha256`.
- Salla route verifies Salla webhook signature/token.
- Unsigned or invalid requests return HTTP 401 Unauthorized immediately.
- Valid requests pass idempotency check via database store before queuing CAPI dispatch.
""",
                "labels": ["story", "p0-critical", "webhooks"]
            },
            {
                "title": "[STORY 3.4] Storefront Checkout Capture for UTMs, Ad ID & Click IDs (fbp/fbc)",
                "body": """### User Story
As a Media Buyer,
I want the checkout snippet to capture UTM parameters, `ad_id`, `fbclid`, `_fbp`, and `_fbc` from the browser session,
So that server-side COD events carry full ad attribution directly to the specific ad creative.

### Acceptance Criteria:
- Captures `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, and `ad_id`.
- Captures `_fbp` and formatted `_fbc` cookies.
- Attaches attribution metadata to order webhook payload for downstream CAPI matching.
""",
                "labels": ["story", "p0-critical", "capi", "cod-engine"]
            }
        ]
    },
    {
        "epic": {
            "title": "[EPIC 4] 24/7 Drift Sentinel & Sunday Signal Hygiene Digests",
            "body": """### Epic Overview
Autonomous background watchdog that continuously monitors client websites, GTM containers, and tracking beacons to detect tag drops, broken dataLayers, or theme updates that break tracking.

### Key Success Criteria:
- Scheduled fingerprinting of live containers and dataLayer events.
- Detection of dropped pixels or missing consent mode tags.
- Verified weekly Sunday WhatsApp retention digest sent to merchants.
""",
            "labels": ["epic", "p1-high", "monitoring", "gtm"]
        },
        "stories": [
            {
                "title": "[STORY 4.1] Background Container Fingerprinting & Beacon Sniffer",
                "body": """### User Story
As an Operations Lead,
I want a 24/7 background sentinel that audits client landing pages and checkout steps,
So that our team is alerted within 15 minutes if a theme change removes GTM snippets or breaks the dataLayer.

### Acceptance Criteria:
- Headless browser visits target URLs and verifies presence of GTM container ID.
- Sniffs network calls for outgoing GA4 (`collect`) and Meta Pixel (`tr`) beacons.
- Emits alert if critical events (`AddToCart`, `InitiateCheckout`) fail to trigger.
""",
                "labels": ["story", "p1-high", "monitoring", "gtm"]
            },
            {
                "title": "[STORY 4.2] Verified Sunday Signal Hygiene WhatsApp Report Generator",
                "body": """### User Story
As an E-Commerce Merchant,
I want to receive an executive weekly WhatsApp report every Sunday morning summarizing tracking health, delivered vs returned orders, and recovered conversions,
So that I have absolute confidence in my tracking setup and agency ROI.

### Acceptance Criteria:
- Aggregates database facts: Total orders placed, COD delivered, COD returned/refused, recovered purchases.
- Formats clean, mobile-friendly WhatsApp digest with zero fake data.
- Generates action items if tag drift or high refusal rates are detected.
""",
                "labels": ["story", "p1-high", "monitoring"]
            }
        ]
    },
    {
        "epic": {
            "title": "[EPIC 5] Cash-Delivered Creative Performance & Ad Scorecard",
            "body": """### Epic Overview
Connect cash-collected revenue directly to Meta Ads ad IDs and creative assets, enabling media buyers to see which ad creatives bring real paid orders versus high return-at-door rates.

### Key Success Criteria:
- Ad-level URL tracking macros standardized across campaigns.
- Creative performance scorecard ranking ads by Cash Delivery Rate (CDR) and True ROAS.
""",
            "labels": ["epic", "p2-medium", "cod-engine", "capi"]
        },
        "stories": [
            {
                "title": "[STORY 5.1] Ad-Level URL Macro Template & Cash-Delivered Creative Scorecard",
                "body": """### User Story
As a Media Buyer & Agency Lead,
I want a creative performance scorecard that computes True ROAS based solely on collected cash from delivered orders rather than gross placed orders,
So that we scale winning ads that actually get paid at the doorstep.

### Acceptance Criteria:
- Standardized URL parameters template (`utm_content={{ad.id}}&ad_name={{ad.name}}`).
- Scorecard calculates: Placed Orders, Delivered Cash, Return Rate %, True COD ROAS.
- Identifies "money-wasting" creatives that have high clicks but high refusal rates at delivery.
""",
                "labels": ["story", "p2-medium", "cod-engine"]
            }
        ]
    }
]

def run_cmd(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ERROR: {' '.join(cmd)}\n{res.stderr}")
        return None
    return res.stdout.strip()

created_items = []

for epic_data in DATA:
    epic_info = epic_data["epic"]
    print(f"\nCreating Epic: {epic_info['title']}...")
    cmd = [
        "gh", "issue", "create",
        "--repo", REPO,
        "--title", epic_info["title"],
        "--body", epic_info["body"],
        "--label", ",".join(epic_info["labels"])
    ]
    issue_url = run_cmd(cmd)
    print(f"Created Epic Issue: {issue_url}")
    
    # Add Epic to project
    if issue_url:
        item_id = run_cmd(["gh", "project", "item-add", str(PROJECT_NUM), "--owner", OWNER, "--url", issue_url, "--format", "json"])
        print(f"Added Epic to Project: {item_id}")
    
    # Create child stories
    for story_info in epic_data["stories"]:
        print(f"  Creating Story: {story_info['title']}...")
        story_body = story_info["body"] + f"\n\n**Parent Epic:** {issue_url}\n"
        cmd = [
            "gh", "issue", "create",
            "--repo", REPO,
            "--title", story_info["title"],
            "--body", story_body,
            "--label", ",".join(story_info["labels"])
        ]
        story_url = run_cmd(cmd)
        print(f"  Created Story Issue: {story_url}")
        
        if story_url:
            s_item_id = run_cmd(["gh", "project", "item-add", str(PROJECT_NUM), "--owner", OWNER, "--url", story_url, "--format", "json"])
            print(f"  Added Story to Project: {s_item_id}")

print("\nAll Epics and Stories created and linked to Project 3 successfully!")
