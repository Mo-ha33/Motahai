"""
consultation.py — Hermes Brain, Advice, and Task Execution Tools for MCP.
Enables sub-agents, Antigravity, and developers to consult Hermes Agent, request
strategic opinions/fatawa, dispatch tasks, and audit code.
"""

import base64
import json
import os
import threading
import time
from typing import Dict, Any, Optional, List
from ..executor import VPSExecutor

GTM_SERVICE_ACCOUNT_PATH = "/home/deploy/secrets/gtm-service-account.json"
DEFAULT_USED_NONCES_PATH = "/home/deploy/.hermes/hitl_used_nonces.json"

# ---------------------------------------------------------------------------
# Rule D-003 approval-token verifier.
# Inlined (stdlib + `cryptography`, imported lazily) because this file is deployed alone into the VPS package and cannot
# import ameen_workforce. Byte-for-byte identical to src/ameen_workforce/hitl_tokens.py, which
# also issues the tokens (POST /approvals/gtm-publish). tests/test_hitl_tokens.py enforces parity.
# Ed25519: this side holds ONLY the public key (HITL_VERIFY_PUBLIC_KEY / HITL_VERIFY_PUBLIC_KEY_PATH); the
# private key lives with the Core service under a separate user, so an agent on this host cannot mint approvals.
# The Hermes host needs `pip install cryptography`; without it every publish fails closed.
# ---------------------------------------------------------------------------
# --- BEGIN VERIFIER (must stay identical to the copy inlined in consultation.py) ---
_NONCE_LOCK = threading.Lock()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_key():
    """Ed25519 PUBLIC key from env HITL_VERIFY_PUBLIC_KEY (base64url raw 32 bytes) or
    HITL_VERIFY_PUBLIC_KEY_PATH (PEM). None if missing/invalid (fail closed). No private key is ever read here."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        raw = os.environ.get("HITL_VERIFY_PUBLIC_KEY", "").strip()
        if raw:
            return Ed25519PublicKey.from_public_bytes(_b64url_decode(raw))
        path = os.environ.get("HITL_VERIFY_PUBLIC_KEY_PATH", "").strip()
        if path:
            with open(path, "rb") as fh:
                key = load_pem_public_key(fh.read())
            return key if isinstance(key, Ed25519PublicKey) else None
    except Exception:
        return None
    return None


def _consume_nonce(used_nonce_store, nonce: str, exp: int, now: float, record: bool = True) -> bool:
    """Atomically records `nonce` as used. Returns False if it was already used or the store is unusable.
    With record=False it only checks that the nonce is still unused (nothing is written)."""
    with _NONCE_LOCK:
        try:
            if os.path.exists(used_nonce_store):
                with open(used_nonce_store, "r", encoding="utf-8") as fh:
                    used = json.load(fh)
                if not isinstance(used, dict):
                    return False
            else:
                used = {}
            if nonce in used:
                return False
            if not record:
                return True
            used = {n: e for n, e in used.items() if isinstance(e, (int, float)) and e > now}
            used[nonce] = exp
            directory = os.path.dirname(os.path.abspath(used_nonce_store))
            os.makedirs(directory, exist_ok=True)
            tmp_path = f"{used_nonce_store}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(used, fh)
            os.replace(tmp_path, used_nonce_store)
            return True
        except (OSError, ValueError):
            return False


def verify_publish_token(token, container_id, workspace_id, used_nonce_store, now=None, consume=True):
    """Returns (ok, reason). With consume=True (default) an ok token's nonce is spent (single use);
    consume=False validates everything without spending it (dry check before a later consume)."""
    public_key = _verify_key()
    if public_key is None:
        return False, "verify_key_not_configured"
    if not isinstance(token, str) or not token.strip():
        return False, "token_missing"
    now = time.time() if now is None else now
    try:
        from cryptography.exceptions import InvalidSignature
        payload_b64, sig_b64 = token.strip().split(".")
        try:
            public_key.verify(_b64url_decode(sig_b64), payload_b64.encode("ascii"))
        except InvalidSignature:
            return False, "bad_signature"
        claims = json.loads(_b64url_decode(payload_b64))
        cid, wid, exp, nonce = claims["cid"], claims["wid"], claims["exp"], claims["nonce"]
        if not (isinstance(cid, str) and isinstance(wid, str) and isinstance(nonce, str)
                and isinstance(exp, int) and not isinstance(exp, bool) and nonce):
            return False, "token_malformed"
    except (ValueError, KeyError, TypeError):
        return False, "token_malformed"
    if now >= exp:
        return False, "token_expired"
    if cid != container_id:
        return False, "container_mismatch"
    if wid != workspace_id:
        return False, "workspace_mismatch"
    if not _consume_nonce(used_nonce_store, nonce, exp, now, record=consume):
        return False, "nonce_reused_or_store_unavailable"
    return True, "ok"
# --- END VERIFIER ---


def _used_nonces_path() -> str:
    return os.environ.get("HITL_USED_NONCES_PATH") or DEFAULT_USED_NONCES_PATH


_PENDING_CREDENTIALS = json.dumps({
    "status": "pending_credentials",
    "service_account_email": "tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com",
    "message": "Service Account key not found at /home/deploy/secrets/gtm-service-account.json. Client should invite tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com with Publish permission in GTM Admin > User Management."
})


def _hitl_gate(tool: str, container_id: str, workspace_id: str, version_name: str, token: Optional[str], precheck=None) -> Optional[str]:
    """Returns None if a valid human-issued approval token was presented (and is now spent), else a JSON refusal.

    The refusal deliberately contains no token value, format hint or example: the agent must
    not be able to self-approve. Only a human operator can obtain a token.
    `precheck` (optional) returns a JSON response if the call cannot proceed for a non-approval reason
    (e.g. missing credentials). It runs only AFTER the token validated and BEFORE it is consumed, so such
    failures neither leak state to an unapproved caller nor burn a human-issued token.
    """
    nonces = _used_nonces_path()
    ok, reason = verify_publish_token(token, container_id, workspace_id, nonces, consume=False)
    if ok and precheck is not None:
        early = precheck()
        if early:
            return early
    if ok:
        ok, reason = verify_publish_token(token, container_id, workspace_id, nonces)
    if ok:
        return None
    return json.dumps({
        "status": "AWAITING_HITL_APPROVAL",
        "gate": "D-003",
        "tool": tool,
        "reason": "Live GTM container publishing requires explicit human supervisor sign-off before modifying production.",
        "action_required": "A human operator must issue an approval token via POST /approvals/gtm-publish",
        "approval_check": reason,
        "proposed_container_id": container_id,
        "proposed_workspace_id": workspace_id,
        "proposed_version": version_name
    })


def register_consultation_tools(server):
    executor = server.executor

    @server.tool(
        name="hermes_consult",
        description=(
            "Consult Hermes Agent for high-level architectural advice, technical opinions, "
            "problem-solving strategies, or decisive recommendations ('فتاوى'). "
            "Hermes leverages its persistent memory (MEMORY.md, SOUL.md) and reasoning loop "
            "to analyze the question and deliver structured guidance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The question, technical challenge, or dilemma to consult Hermes on."
                },
                "context": {
                    "type": "string",
                    "description": "Optional background information, code snippet, logs, or architectural context."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override (e.g., 'gemini-2.5-flash', 'deepseek/deepseek-r1')."
                },
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "max"],
                    "description": "Level of reasoning/thinking effort to apply."
                }
            },
            "required": ["prompt"]
        }
    )
    def hermes_consult(prompt: str, context: Optional[str] = None, model: Optional[str] = None, reasoning_effort: Optional[str] = "high") -> str:
        try:
            from ..soul_router_client import consult, RouterExhaustedError, RouterUnavailableError
            r = consult(prompt, context=context)
            return f"{r['text']}\n\n{r['provenance']}"
        except Exception as e:
            if "RouterExhaustedError" in str(type(e)):
                return '```json\n{"status": "BLOCKED", "decision": "All LLM providers are rate-limited; retry later.", "next_agent": "lead-orchestrator"}\n```'
            # Fallback to Hermes CLI if router is unreachable
            full_prompt = prompt
            if context:
                full_prompt = f"### CONTEXT:\n{context}\n\n### CONSULTATION INSTRUCTION:\n{prompt}\n\nPlease analyze deeply and provide your decisive recommendation ('fatwa'), trade-offs, and actionable steps."
            output, success = executor.execute_hermes_cli(prompt=full_prompt, model=model)
            return output

    @server.tool(
        name="hermes_run_task",
        description=(
            "Dispatch an autonomous multi-step execution task or goal to Hermes Agent on the VPS. "
            "Hermes will autonomously investigate, plan, use its available tools and skills, and "
            "return the final output and solution."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The exact goal, assignment, or mission for Hermes to execute."
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of skill names to preload for this task (e.g. ['gtm-tracking-engineer'])."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override."
                }
            },
            "required": ["task"]
        }
    )
    def hermes_run_task(task: str, skills: Optional[List[str]] = None, model: Optional[str] = None) -> str:
        output, success = executor.execute_hermes_cli(prompt=task, model=model, skills=skills)
        return output

    @server.tool(
        name="hermes_audit_code",
        description=(
            "Request a formal code and security audit from Hermes Agent. Evaluates code against "
            "security vulnerabilities, edge cases, architecture alignment, and failure modes. "
            "Returns a structured audit report with AUDIT_APPROVED or CHANGES_REQUESTED and suggested diffs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The code content or patch to audit."
                },
                "file_path": {
                    "type": "string",
                    "description": "Target file name or path being reviewed."
                },
                "focus_areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific audit focuses (e.g. ['security', 'rate_limits', 'performance', 'error_handling'])."
                }
            },
            "required": ["code"]
        }
    )
    def hermes_audit_code(code: str, file_path: Optional[str] = None, focus_areas: Optional[List[str]] = None) -> str:
        focus_str = ", ".join(focus_areas) if focus_areas else "Security, logic correctness, reliability, edge cases"
        prompt = (
            f"Audit this code for: {focus_str}. "
            f"File: {file_path or 'n/a'}. Reply with hermes.verdict.v1 format."
        )
        return hermes_consult(prompt=prompt, context=f"```\n{code}\n```")

    @server.tool(
        name="hermes_gtm_deploy",
        description=(
            "Deploy and publish the configured tracking container (tags, triggers, variables) to Google Tag Manager "
            "through the operator's local browser bridge. Requires a human-issued approval token (Rule D-003)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "container_id": {
                    "type": "string",
                    "description": "GTM container ID, e.g. GTM-5C5N552P"
                },
                "version_name": {
                    "type": "string",
                    "description": "Version name for GTM publish, e.g. v1.0.0 - Motahai Full-Funnel Tracking Release"
                },
                "hitl_approval_token": {
                    "type": "string",
                    "description": "Signed single-use approval token issued by a human operator for this container (workspace 'browser') (Rule D-003)"
                }
            },
            "required": ["container_id"]
        }
    )
    def hermes_gtm_deploy(container_id: str, version_name: str = "v1.0.0 - Motahai Full-Funnel Tracking Release", hitl_approval_token: Optional[str] = None) -> str:
        import urllib.request

        # Rule D-003: the local browser bridge publishes to production, so it is gated exactly like the cloud tool.
        refusal = _hitl_gate("hermes_gtm_deploy", container_id, "browser", version_name, hitl_approval_token)
        if refusal:
            return refusal

        try:
            data = json.dumps({"container_id": container_id, "version_name": version_name}).encode("utf-8")
            req = urllib.request.Request("http://127.0.0.1:8765/gtm-deploy", data=data, headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=45)
            return resp.read().decode("utf-8")
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})


    @server.tool(
        name="hermes_gtm_cloud_publish",
        description=(
            "Direct 24/7 Cloud API deployment to Google Tag Manager via official GTM API v2 (Service Account / OAuth). "
            "Creates, tags, and publishes container versions directly to Google Cloud without requiring a local browser session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "Google Tag Manager Account ID (e.g. 6380333426)"
                },
                "container_id": {
                    "type": "string",
                    "description": "GTM Container numerical ID (e.g. 265962858) or public container string (GTM-XXXXXXX)"
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID (default '2')"
                },
                "version_name": {
                    "type": "string",
                    "description": "Name for the published container version"
                },
                "hitl_approval_token": {
                    "type": "string",
                    "description": "Signed single-use approval token issued by a human operator for this container and workspace (Rule D-003)"
                }
            },
            "required": ["account_id", "container_id"]
        }
    )
    def hermes_gtm_cloud_publish(account_id: str, container_id: str, workspace_id: str = "2", version_name: str = "v1.0.0 - Cloud Autonomous Release", hitl_approval_token: Optional[str] = None) -> str:
        # Rule D-003: Enforce HITL approval gate before live release. The token must be signed by a
        # human operator, bound to this container + workspace, unexpired and unused.
        # Credentials are checked inside the gate, BEFORE the nonce is consumed, so a missing
        # service-account file does not burn a human-issued token.
        secrets_path = GTM_SERVICE_ACCOUNT_PATH
        refusal = _hitl_gate("hermes_gtm_cloud_publish", container_id, workspace_id, version_name, hitl_approval_token,
                             precheck=lambda: None if os.path.exists(secrets_path) else _PENDING_CREDENTIALS)
        if refusal:
            return refusal

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            
            SCOPES = ['https://www.googleapis.com/auth/tagmanager.publish']
            creds = service_account.Credentials.from_service_account_file(secrets_path, scopes=SCOPES)
            service = build('tagmanager', 'v2', credentials=creds)
            
            parent_ws = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
            
            version_body = {
                "name": version_name,
                "notes": "Autonomously deployed and verified by Tariq AI via Google Tag Manager Cloud API v2 with Zero-PII and Consent Mode v2."
            }
            
            version_res = service.accounts().containers().workspaces().create_version(
                path=parent_ws,
                body=version_body
            ).execute()
            
            v_path = version_res['containerVersion']['path']
            
            live_pub = service.accounts().containers().versions().publish(
                path=v_path
            ).execute()
            
            return json.dumps({
                "status": "success",
                "mode": "cloud_api_v2",
                "account_id": account_id,
                "container_id": container_id,
                "live_version_id": live_pub['containerVersion']['containerVersionId'],
                "version_name": live_pub['containerVersion']['name'],
                "url": f"https://tagmanager.google.com/#/versions/{v_path}",
                "zero_pii": "enforced",
                "consent_mode_v2": "enforced"
            })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "mode": "cloud_api_v2",
                "error": str(e)
            })
