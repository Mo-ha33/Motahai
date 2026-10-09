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

import ipaddress
import logging
import re
from typing import Dict, Any, Optional
from .capi_service import DELIVERED_EVENT_NAME, capi_sender, hash_match_value

logger = logging.getLogger("ameen_workforce.webhooks")

COD_PHRASES = ("cash on delivery", "cash_on_delivery", "الدفع عند الاستلام")
COD_TOKEN = re.compile(r"(?<![a-z0-9])cod(?![a-z0-9])")
FULFILLMENT_PLATFORM = "shopify_fulfillment"

# Salla status slugs. ASSUMPTION (verify against a live Salla store): Salla exposes no explicit paid flag
# in order.status.updated, so a prepaid order past review/payment is treated as paid.
SALLA_DELIVERED_SLUGS = ("delivered", "completed", "تم التوصيل", "مكتمل")
SALLA_CANCELLED_SLUGS = ("canceled", "cancelled", "ملغي", "ملغى")
SALLA_PAID_SLUGS = ("in_progress", "processing", "shipping", "shipped", "delivering")

# --- Attribution capture (S1-3) -------------------------------------------------------------------------
# The storefront script (storefront/shopify/assets/motahai-capture.js) writes hidden cart attributes named
# `_mt_<field>`; Shopify copies cart attributes to the order's `note_attributes` ([{name, value}]).
# Everything arriving this way is attacker-controllable (anyone can POST /cart/update.js), so every value is
# validated: a value that does not match its pattern is DROPPED (never truncated into something plausible).
ATTRIBUTION_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content",
                      "ad_id", "fbp", "fbc", "ttclid", "sccid")
NOTE_ATTRIBUTE_PREFIXES = ("_mt_", "mt_")
MAX_UTM_LEN = 255
MAX_USER_AGENT_LEN = 512
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_AD_ID = re.compile(r"^[0-9]{1,64}$")
# Meta cookie formats are fb.<subdomainIndex>.<creationTimeMs>.<id>; matched loosely, case preserved.
_FBP = re.compile(r"^fb\.[0-9]{1,2}\.[0-9]{10,14}\.[0-9]{1,24}$")
_FBC = re.compile(r"^fb\.[0-9]{1,2}\.[0-9]{10,14}\.[A-Za-z0-9_\-]{1,200}$")
_CLICK_ID = re.compile(r"^[A-Za-z0-9_.~:\-]{1,255}$")

def _clean_text(value: Any, max_len: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    return cleaned if cleaned and len(cleaned) <= max_len else None

def _matching(value: Any, pattern: "re.Pattern[str]") -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if pattern.match(cleaned) else None

def sanitize_attribution(raw: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """
    Validates raw attribution values (keys from ATTRIBUTION_FIELDS) and returns ALL keys, junk or missing as None.
    utm_*: control chars removed, <= 255 chars; ad_id: digits only (<= 64); fbp/fbc: Meta cookie shape;
    ttclid/sccid: URL-safe token <= 255 chars.
    """
    raw = raw or {}
    return {
        "utm_source": _clean_text(raw.get("utm_source"), MAX_UTM_LEN),
        "utm_medium": _clean_text(raw.get("utm_medium"), MAX_UTM_LEN),
        "utm_campaign": _clean_text(raw.get("utm_campaign"), MAX_UTM_LEN),
        "utm_content": _clean_text(raw.get("utm_content"), MAX_UTM_LEN),
        "ad_id": _matching(raw.get("ad_id"), _AD_ID),
        "fbp": _matching(raw.get("fbp"), _FBP),
        "fbc": _matching(raw.get("fbc"), _FBC),
        "ttclid": _matching(raw.get("ttclid"), _CLICK_ID),
        "sccid": _matching(raw.get("sccid"), _CLICK_ID),
    }

def extract_shopify_attribution(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Reads the `_mt_*` entries of `note_attributes` (list of {name, value}) and sanitizes them. First occurrence wins."""
    found: Dict[str, Any] = {}
    notes = payload.get("note_attributes")
    for item in notes if isinstance(notes, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"].strip().lower()
        for prefix in NOTE_ATTRIBUTE_PREFIXES:
            if name.startswith(prefix):
                key = name[len(prefix):]
                if key in ATTRIBUTION_FIELDS:
                    found.setdefault(key, item.get("value"))
                break
    return sanitize_attribution(found)

def sanitize_client_ip(value: Any) -> Optional[str]:
    """A valid IPv4/IPv6 address string, else None."""
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None

def sanitize_user_agent(value: Any) -> Optional[str]:
    """Control characters removed, truncated to 512 chars (a truncated UA still matches), else None."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()[:MAX_USER_AGENT_LEN]
    return cleaned or None

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
        "held_event": DELIVERED_EVENT_NAME
    }

# --- Net value and match keys (S2-3 / S2-4) ---------------------------------------------------------------
def _money(value: Any) -> float:
    """Tolerant money parse for optional fields (junk counts as 0); never used for the order total itself."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def shopify_net_total(payload: Dict[str, Any]) -> float:
    """
    Net amount collected for a Shopify order (S2-4), computed from the refunds: total_price minus the sum of
    `refunds[].transactions[]` with kind "refund" and status "success" (compared case-insensitively: REST "refund" /
    GraphQL "REFUND"), never below 0. WHY not current_total_price: Shopify documents currentTotalPriceSet as the price
    "after returns, includes taxes and discounts" but does not state that refunds or edits are included, so it is used
    ONLY as a fallback when the payload has no `refunds` array at all (then total_price if that is missing too).
    """
    refunds = payload.get("refunds")
    if not isinstance(refunds, list):
        current = payload.get("current_total_price")
        if current not in (None, ""):
            return float(current)
        return float(payload.get("total_price") or 0.0)
    total = float(payload.get("total_price") or 0.0)
    refunded = 0.0
    for refund in refunds:
        transactions = refund.get("transactions") if isinstance(refund, dict) else None
        for txn in transactions if isinstance(transactions, list) else []:
            if (isinstance(txn, dict) and str(txn.get("kind") or "").lower() == "refund"
                    and str(txn.get("status") or "").lower() == "success"):
                refunded += _money(txn.get("amount"))
    return max(total - refunded, 0.0)

def _shopify_address(payload: Dict[str, Any]) -> Dict[str, Any]:
    """RAW address fields (hashed immediately by the pipeline, never stored): shipping address, else billing."""
    chosen: Dict[str, Any] = {}
    for key in ("shipping_address", "billing_address"):
        candidate = payload.get(key)
        if isinstance(candidate, dict) and any(candidate.get(f) for f in ("first_name", "last_name", "city", "zip")):
            chosen = candidate
            break
    code = chosen.get("province_code")
    state = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z]{2}", code.strip()) else chosen.get("province")
    return {"fn": chosen.get("first_name"), "ln": chosen.get("last_name"), "ct": chosen.get("city"),
            "st": state, "zp": chosen.get("zip"), "country": chosen.get("country_code")}

def _id_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    return str(value)

def match_hashes_from_parsed(parsed: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """SHA-256 match keys (external_id, fn, ln, ct, st, zp, country) for a parsed order; empty ones are None."""
    address = parsed.get("address") or {}
    hashes = {key: hash_match_value(key, address.get(key)) for key in ("fn", "ln", "ct", "st", "zp", "country")}
    hashes["external_id"] = hash_match_value("external_id", parsed.get("external_id"))
    return hashes

class OrderWebhookProcessor:
    @staticmethod
    def parse_shopify_order(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses Shopify orders/fulfilled, orders/updated, or orders/paid webhook.
        Status: cancelled | voided | refunded (never emit), delivered (COD, paid and fulfilled),
        paid_unfulfilled (COD, paid but not fulfilled: held), paid (prepaid, paid),
        shipped (fulfilled but not paid), pending.
        `partially_refunded` counts as paid (a partial refund implies the order was paid): the order stays eligible
        and total_price is the NET collected value (see shopify_net_total). Only a full `refunded` is suppressed.
        """
        order_id = str(payload.get("id") or payload.get("order_number") or "")
        financial_status = (payload.get("financial_status") or "").lower()
        fulfillment_status = (payload.get("fulfillment_status") or "").lower()
        
        # Payment gateway checks for COD
        gateways = [str(g) for g in payload.get("payment_gateway_names", [])]
        is_cod = any(is_cod_gateway(g) for g in gateways)
        
        # Shopify 'fulfilled' = handed to the courier (shipped), NOT delivered.
        is_paid = financial_status in ("paid", "partially_refunded")
        if payload.get("cancelled_at"):
            effective_status = "cancelled"
        elif financial_status == "refunded":
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
        
        total_price = shopify_net_total(payload)
        currency = payload.get("currency") or "EGP"
        client_details = payload.get("client_details")
        client_details = client_details if isinstance(client_details, dict) else {}

        return {
            "platform": "shopify",
            "order_id": order_id,
            "status": effective_status,
            "is_cod": is_cod,
            "total_price": total_price,
            "currency": currency,
            "email": email,
            "phone": phone,
            # Attribution (S1-3). client_ip / user_agent are pass-through for the CAPI payload only: never stored.
            "attribution": extract_shopify_attribution(payload),
            "client_ip": sanitize_client_ip(client_details.get("browser_ip") or payload.get("browser_ip")),
            "user_agent": sanitize_user_agent(client_details.get("user_agent")),
            # Match keys (S2-3), RAW and in-flight only: the pipeline stores their hashes, never these values.
            "external_id": _id_str(customer.get("id")),
            "address": _shopify_address(payload)
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
            "attribution": None,
            "client_ip": None,
            "user_agent": None,
            "external_id": None,
            "address": None,
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
        # TODO(S2-4, Salla): no refunded/net amount is known for Salla `amounts` yet; partial refunds are not netted
        # (statuses restored/refunded/returned are still suppressed). Verify the payload of order.refunded on a real store.
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
            "phone": phone,
            # No known way yet for a Salla app to attach attribution to an order (see storefront/salla/README.md).
            "attribution": None,
            "client_ip": None,
            "user_agent": None,
            # INFERRED Salla fields (verify on a real store): customer.id/first_name/last_name/city/country_code and
            # shipping.address.{city,postal_code,country_code}. Missing ones are simply None.
            "external_id": _id_str(customer.get("id")),
            "address": {
                "fn": customer.get("first_name"), "ln": customer.get("last_name"),
                "ct": ((data.get("shipping") or {}).get("address") or {}).get("city") or customer.get("city"),
                "st": None,
                "zp": ((data.get("shipping") or {}).get("address") or {}).get("postal_code"),
                "country": ((data.get("shipping") or {}).get("address") or {}).get("country_code")
                           or customer.get("country_code"),
            }
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
        to Meta CAPI if eligible. LEGACY STATELESS PATH: no settlement window, no placed-time event_time/cutoff and no
        stored checkout context; the persistent order_pipeline.process_webhook is the production path. Platforms: "shopify" (orders/*), "shopify_fulfillment"
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
            test_event_code=test_event_code,
            fbp=(parsed.get("attribution") or {}).get("fbp"),
            fbc=(parsed.get("attribution") or {}).get("fbc"),
            client_ip=parsed.get("client_ip"),
            user_agent=parsed.get("user_agent"),
            match_hashes=match_hashes_from_parsed(parsed)
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
