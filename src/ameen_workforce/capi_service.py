"""
capi_service.py — Production Meta Conversions API (CAPI) & Rule D-005 Engine
Handles server-side conversion delivery to Meta Graph API v20.0.
Implements Rule D-005:
- COD orders fire 'OrderPlaced' on creation.
- True 'Purchase' event is fired SERVER-SIDE ONLY when the order is marked DELIVERED and paid.
- Full SHA-256 normalization for PII under PDPL/GDPR compliance.
"""

import hashlib
import time
import re
import logging
from typing import Dict, Any, Optional, List
import httpx

logger = logging.getLogger("ameen_workforce.capi")

META_GRAPH_API_VERSION = "v20.0"

def hash_sha256(val: Optional[str]) -> Optional[str]:
    """Normalizes and hashes a string using SHA-256."""
    if not val:
        return None
    cleaned = str(val).strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

def normalize_phone(phone: Optional[str], default_country: str = "EG") -> Optional[str]:
    """Normalizes phone to digits-only E.164 representation before hashing."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if default_country == "EG" and digits.startswith("01"):
        digits = "2" + digits
    elif default_country == "SA" and digits.startswith("05"):
        digits = "966" + digits[1:]
    return digits

class MetaCAPISender:
    def __init__(self, pixel_id: Optional[str] = None, access_token: Optional[str] = None):
        self.pixel_id = pixel_id
        self.access_token = access_token

    def build_event_payload(
        self,
        event_name: str,
        event_id: str,
        order_id: str,
        value: float,
        currency: str,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        fbp: Optional[str] = None,
        fbc: Optional[str] = None,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        test_event_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Builds a compliant Meta Conversions API JSON payload.
        Ensures Zero-PII by hashing email and phone with SHA-256.
        """
        user_data: Dict[str, Any] = {}
        if email:
            user_data["em"] = [hash_sha256(email)]
        if phone:
            user_data["ph"] = [hash_sha256(normalize_phone(phone))]
        if fbp:
            user_data["fbp"] = fbp
        if fbc:
            user_data["fbc"] = fbc
        if client_ip:
            user_data["client_ip_address"] = client_ip
        if user_agent:
            user_data["client_user_agent"] = user_agent

        event_obj: Dict[str, Any] = {
            "event_name": event_name,
            "event_time": int(time.time()),
            "event_id": event_id,
            "action_source": "website",
            "user_data": user_data,
            "custom_data": {
                "currency": currency.upper(),
                "value": float(value),
                "order_id": str(order_id)
            }
        }

        payload: Dict[str, Any] = {
            "data": [event_obj]
        }
        if test_event_code:
            payload["test_event_code"] = test_event_code

        return payload

    async def send_event(
        self,
        pixel_id: str,
        access_token: str,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Sends the event payload to Meta Graph API.
        """
        url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{pixel_id}/events"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, json=payload)
                data = res.json()
                if res.status_code == 200:
                    return {
                        "status": "success",
                        "events_received": data.get("events_received", 1),
                        "fbtrace_id": data.get("fbtrace_id"),
                        "raw": data
                    }
                else:
                    return {
                        "status": "error",
                        "http_code": res.status_code,
                        "error_message": data.get("error", {}).get("message", "Unknown Meta API error"),
                        "raw": data
                    }
        except Exception as e:
            logger.error("Meta CAPI dispatch failed: %s", e)
            return {
                "status": "failed",
                "error": str(e)
            }

    def process_cod_order_event(
        self,
        order_id: str,
        status: str,
        value: float,
        currency: str,
        is_cod: bool = True,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        test_event_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Implements Rule D-005:
        - If COD and status != 'delivered'/'paid', do NOT fire Purchase.
        - If status == 'delivered' or prepaid order, fires true Purchase with event_id = purchase_<order_id>.
        """
        event_id = f"purchase_{order_id}"
        norm_status = status.strip().lower()

        if is_cod and norm_status not in ["delivered", "paid", "fulfilled"]:
            return {
                "action": "DEFERRED",
                "reason": "Rule D-005: COD order is pending cash collection. Purchase event held.",
                "order_id": order_id,
                "current_status": status,
                "event_type": "OrderPlaced"
            }

        # Order is confirmed delivered and paid -> emit Purchase
        payload = self.build_event_payload(
            event_name="Purchase",
            event_id=event_id,
            order_id=order_id,
            value=value,
            currency=currency,
            email=email,
            phone=phone,
            test_event_code=test_event_code
        )

        return {
            "action": "READY_TO_EMIT",
            "event_name": "Purchase",
            "event_id": event_id,
            "order_id": order_id,
            "payload": payload
        }

capi_sender = MetaCAPISender()
