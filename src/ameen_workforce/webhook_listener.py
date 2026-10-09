"""
webhook_listener.py — Zero-Effort Shopify & Salla Webhook Ingestion Engine
Receives order status updates from e-commerce platforms.
Triggers Meta CAPI post-delivery event under Rule D-005 without requiring courier API keys.
"""

import logging
from typing import Dict, Any, Optional
from .capi_service import capi_sender

logger = logging.getLogger("ameen_workforce.webhooks")

class OrderWebhookProcessor:
    @staticmethod
    def parse_shopify_order(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses Shopify orders/fulfilled, orders/updated, or orders/paid webhook.
        """
        order_id = str(payload.get("id") or payload.get("order_number") or "")
        financial_status = (payload.get("financial_status") or "").lower()
        fulfillment_status = (payload.get("fulfillment_status") or "").lower()
        
        # Payment gateway checks for COD
        gateways = [str(g).lower() for g in payload.get("payment_gateway_names", [])]
        is_cod = any("cod" in g or "cash" in g or "manual" in g for g in gateways)
        
        # Check delivery status
        is_delivered = fulfillment_status == "fulfilled" or financial_status == "paid"
        effective_status = "delivered" if is_delivered else "in_transit"

        customer = payload.get("customer") or {}
        email = customer.get("email") or payload.get("email")
        phone = customer.get("phone") or (payload.get("shipping_address") or {}).get("phone")
        
        total_price = float(payload.get("current_total_price") or payload.get("total_price") or 0.0)
        currency = payload.get("currency") or "EGP"

        return {
            "platform": "shopify",
            "order_id": order_id,
            "status": effective_status,
            "is_cod": is_cod,
            "total_price": total_price,
            "currency": currency,
            "email": email,
            "phone": phone
        }

    @staticmethod
    def parse_salla_order(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses Salla order.status.updated webhook.
        """
        data = payload.get("data") or payload
        order_id = str(data.get("id") or data.get("reference_id") or "")
        status_info = data.get("status") or {}
        status_slug = (status_info.get("slug") or status_info.get("name") or "").lower()
        
        payment_method = str(data.get("payment_method") or "").lower()
        is_cod = "cod" in payment_method or "cash" in payment_method or "عند الاستلام" in payment_method
        
        is_delivered = status_slug in ["delivered", "completed", "تم التوصيل", "مكتمل"]
        effective_status = "delivered" if is_delivered else status_slug

        customer = data.get("customer") or {}
        email = customer.get("email")
        phone = customer.get("mobile")

        amounts = data.get("amounts") or {}
        total_price = float((amounts.get("total") or {}).get("amount") or data.get("total") or 0.0)
        currency = (amounts.get("total") or {}).get("currency") or data.get("currency") or "SAR"

        return {
            "platform": "salla",
            "order_id": order_id,
            "status": effective_status,
            "is_cod": is_cod,
            "total_price": total_price,
            "currency": currency,
            "email": email,
            "phone": phone
        }

    async def handle_order_update(
        self,
        platform: str,
        payload: Dict[str, Any],
        pixel_id: Optional[str] = None,
        access_token: Optional[str] = None,
        test_event_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Ingests the platform webhook, applies Rule D-005, and dispatches to Meta CAPI if eligible.
        """
        if platform == "shopify":
            parsed = self.parse_shopify_order(payload)
        elif platform == "salla":
            parsed = self.parse_salla_order(payload)
        else:
            return {"status": "error", "message": f"Unsupported platform: {platform}"}

        decision = capi_sender.process_cod_order_event(
            order_id=parsed["order_id"],
            status=parsed["status"],
            value=parsed["total_price"],
            currency=parsed["currency"],
            is_cod=parsed["is_cod"],
            email=parsed["email"],
            phone=parsed["phone"],
            test_event_code=test_event_code
        )

        if decision["action"] == "READY_TO_EMIT" and pixel_id and access_token:
            result = await capi_sender.send_event(
                pixel_id=pixel_id,
                access_token=access_token,
                payload=decision["payload"]
            )
            decision["capi_dispatch_result"] = result

        return {
            "status": "processed",
            "parsed_order": parsed,
            "d005_decision": decision
        }

order_webhook_processor = OrderWebhookProcessor()
