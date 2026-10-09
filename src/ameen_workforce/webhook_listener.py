"""
webhook_listener.py — Zero-Effort Shopify & Salla Webhook Ingestion Engine
Receives order status updates from e-commerce platforms.
Applies Rule D-005 (decision "Option A: Coexist") without requiring courier API keys:
we never send a standard Purchase (the merchant's native Meta integration owns it); we send the
custom event 'DeliveredPurchase' (event_id = delivered_<order_id>) for COD orders only once delivered
and for prepaid orders once paid.

Shopify delivery signals:
- orders/* topics (parse_shopify_order): `fulfillment_status == "fulfilled"` only means the order was
  handed to the courier, i.e. SHIPPED. A COD order counts as delivered ONLY when
  `financial_status == "paid"` (the merchant marked the cash as collected) AND it has a fulfillment
  (fulfillment_status fulfilled/partial or a non-empty `fulfillments` list), because some COD setups
  mark orders paid at creation; paid but unfulfilled COD is `paid_unfulfilled` and held.
- fulfillments/update topic (parse_shopify_fulfillment): `shipment_status == "delivered"`. This payload
  carries `order_id` but NOT order value/currency/customer, so it cannot be emitted on its own.
  The persistent path (order_pipeline.process_webhook, S1-1) joins the fulfillment to its stored order;
  the stateless handle_order_update path takes the stored order as `order_context` and otherwise
  yields NEEDS_ORDER_CONTEXT.
"""

import logging
import re
from typing import Dict, Any, Optional
from .capi_service import capi_sender

logger = logging.getLogger("ameen_workforce.webhooks")

COD_PHRASES = ("cash on delivery", "cash_on_delivery", "الدفع عند الاستلام")
COD_TOKEN = re.compile(r"(?<![a-z0-9])cod(?![a-z0-9])")
FULFILLMENT_PLATFORM = "shopify_fulfillment"

# Salla status slugs. ASSUMPTION (verify against a live Salla store): Salla exposes no explicit paid flag
# in order.status.updated, so a prepaid order past review/payment is treated as paid.
SALLA_DELIVERED_SLUGS = ("delivered", "completed", "تم التوصيل", "مكتمل")
SALLA_CANCELLED_SLUGS = ("canceled", "cancelled", "ملغي", "ملغى")
SALLA_PAID_SLUGS = ("in_progress", "processing", "shipping", "shipped", "delivering")

def is_cod_gateway(name: Any) -> bool:
    """
    True for a Shopify gateway name that means cash on delivery. 'manual' is deliberately NOT
    matched: it also covers bank transfer and other manual payment methods.
    """
    text = str(name).strip().lower()
    return any(phrase in text for phrase in COD_PHRASES) or bool(COD_TOKEN.search(text))

def fulfillment_context_decision(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    D-005 decision for a parsed fulfillments/update record that has NO order context (value/currency/customer).
    Nothing can be emitted from it: delivered -> NEEDS_ORDER_CONTEXT, cancelled/failed -> SUPPRESSED,
    anything else -> DEFERRED. Shared by the stateless handler and the persistent order pipeline.
    """
    if parsed["status"] == "delivered":
        return {
            "action": "NEEDS_ORDER_CONTEXT",
            "reason": "Rule D-005: fulfillment is delivered but the payload has no order value/currency. "
                      "Join it to the stored order (S1-1) before emitting DeliveredPurchase.",
            "order_id": parsed["order_id"],
            "current_status": parsed["status"]
        }
    if parsed["status"] in ("cancelled", "failed_delivery"):
        return {
            "action": "SUPPRESSED",
            "reason": "Rule D-005: fulfillment cancelled or delivery failed. No conversion is sent.",
            "order_id": parsed["order_id"],
            "current_status": parsed["status"]
        }
    return {
        "action": "DEFERRED",
        "reason": "Rule D-005: fulfillment not delivered yet. Conversion held until delivery.",
        "order_id": parsed["order_id"],
        "current_status": parsed["status"],
        "held_event": "DeliveredPurchase"
    }

class OrderWebhookProcessor:
    @staticmethod
    def parse_shopify_order(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses Shopify orders/fulfilled, orders/updated, or orders/paid webhook.
        Status: cancelled | voided | refunded (never emit), delivered (COD, paid and fulfilled),
        paid_unfulfilled (COD, paid but not fulfilled: held), paid (prepaid, paid),
        shipped (fulfilled but not paid), pending.
        """
        order_id = str(payload.get("id") or payload.get("order_number") or "")
        financial_status = (payload.get("financial_status") or "").lower()
        fulfillment_status = (payload.get("fulfillment_status") or "").lower()
        
        # Payment gateway checks for COD
        gateways = [str(g) for g in payload.get("payment_gateway_names", [])]
        is_cod = any(is_cod_gateway(g) for g in gateways)
        
        # Shopify 'fulfilled' = handed to the courier (shipped), NOT delivered.
        is_paid = financial_status == "paid"
        if payload.get("cancelled_at"):
            effective_status = "cancelled"
        elif financial_status in ("refunded", "partially_refunded"):
            effective_status = "refunded"
        elif financial_status == "voided":
            effective_status = "voided"
        elif is_paid and is_cod:
            # Some COD setups mark the order paid at creation, so paid alone is not proof of delivery:
            # require that the order has also been fulfilled (handed to the courier).
            has_fulfillment = fulfillment_status in ("fulfilled", "partial") or bool(payload.get("fulfillments"))
            effective_status = "delivered" if has_fulfillment else "paid_unfulfilled"
        elif is_paid:
            effective_status = "paid"
        elif fulfillment_status == "fulfilled":
            effective_status = "shipped"
        else:
            effective_status = "pending"

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
    def parse_shopify_fulfillment(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses a Shopify fulfillments/update webhook. Delivered iff `shipment_status == "delivered"`.
        The payload has no order value/currency/customer/payment method, so those fields are None and
        `needs_order_context` is True: the S1-1 persistence task joins this record to the stored order.
        """
        shipment_status = str(payload.get("shipment_status") or "").lower()
        fulfillment_state = str(payload.get("status") or "").lower()

        if fulfillment_state in ("cancelled", "canceled", "error", "failure"):
            effective_status = "cancelled"
        elif shipment_status == "delivered":
            effective_status = "delivered"
        elif shipment_status == "failure":
            effective_status = "failed_delivery"
        else:
            effective_status = "shipped"

        return {
            "platform": FULFILLMENT_PLATFORM,
            "order_id": str(payload.get("order_id") or ""),
            "fulfillment_id": str(payload.get("id") or ""),
            "status": effective_status,
            "shipment_status": shipment_status,
            "is_cod": None,
            "total_price": None,
            "currency": None,
            "email": None,
            "phone": None,
            "needs_order_context": True
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
        
        if status_slug in SALLA_DELIVERED_SLUGS:
            effective_status = "delivered"
        elif status_slug in SALLA_CANCELLED_SLUGS:
            effective_status = "cancelled"
        elif not is_cod and status_slug in SALLA_PAID_SLUGS:
            effective_status = "paid"
        else:
            effective_status = status_slug  # e.g. restored / refunded / returned are suppressed downstream

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
        test_event_code: Optional[str] = None,
        order_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Ingests the platform webhook, applies Rule D-005, and dispatches the custom 'DeliveredPurchase'
        to Meta CAPI if eligible. Platforms: "shopify" (orders/*), "shopify_fulfillment"
        (fulfillments/update), "salla".

        order_context: for "shopify_fulfillment" only. The stored order (keys total_price, currency,
        is_cod, email, phone) that S1-1 persistence will join by order_id. Without it a delivered
        fulfillment yields NEEDS_ORDER_CONTEXT because the payload carries no order value.
        """
        if platform == "shopify":
            parsed = self.parse_shopify_order(payload)
        elif platform == FULFILLMENT_PLATFORM:
            parsed = self.parse_shopify_fulfillment(payload)
            if order_context and order_context.get("total_price") is not None:
                parsed = {**parsed, **{k: v for k, v in order_context.items() if k in (
                    "total_price", "currency", "is_cod", "email", "phone")}, "needs_order_context": False}
        elif platform == "salla":
            parsed = self.parse_salla_order(payload)
        else:
            return {"status": "error", "message": f"Unsupported platform: {platform}"}

        if parsed.get("needs_order_context"):
            # Fulfillment-only record: nothing can be emitted without the order's value/currency.
            decision = fulfillment_context_decision(parsed)
            return {"status": "processed", "parsed_order": parsed, "d005_decision": decision}

        decision = capi_sender.process_cod_order_event(
            order_id=parsed["order_id"],
            status=parsed["status"],
            value=parsed["total_price"],
            currency=parsed["currency"],
            is_cod=bool(parsed["is_cod"]),
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
