"""
capi_service.py — Production Meta Conversions API (CAPI) & Rule D-005 Engine
Handles server-side conversion delivery to Meta Graph API v20.0.

Rule D-005 (decision "Option A: Coexist", 2026-10-09):
- The merchant's native Shopify/Salla Meta integration keeps sending the standard 'Purchase'
  at order creation. We NEVER send a standard 'Purchase' (no double counting, no fighting it).
- Instead we send the custom event 'DeliveredPurchase' to the same dataset, with
  event_id = delivered_<order_id>, so a custom conversion can count real revenue only:
    * COD orders: only once the order is DELIVERED (Shopify: financial_status == 'paid', i.e. the
      merchant marked the cash as collected; or a fulfillment shipment_status == 'delivered').
      Shopify 'fulfilled' only means handed to the courier and is NOT delivery.
    * Prepaid orders: once PAID.
- Cancelled / fully refunded / voided orders never emit. A PARTIALLY refunded delivered/paid order is still a
  conversion, valued at the net collected amount (S2-4); a net value <= 0 is suppressed.
- event_time is the order's PLACED time (S2-1), so Meta's 7-day click attribution window is measured from the click to
  the order, not to delivery. Meta's separate 7-day upload limit is protected by LATE_DELIVERY_CUTOFF (6.5 days after
  placement): past it the conversion is not sent (action LATE_DELIVERY).
- Option B (take over the standard Purchase from the native integration) is a future opt-in.
- Full SHA-256 normalization for PII under PDPL/GDPR compliance.
"""

import hashlib
import time
import re
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Any, Optional, List
import httpx

logger = logging.getLogger("ameen_workforce.capi")

META_GRAPH_API_VERSION = "v20.0"

# Rule D-005 (Coexist): custom event, never the standard Purchase the native integration sends.
DELIVERED_EVENT_NAME = "DeliveredPurchase"
# Statuses that mean revenue is real: COD cash collected / delivered, or prepaid and paid.
CONVERTING_STATUSES = frozenset({"delivered", "paid"})
# Meta rejects the WHOLE request if any event_time is older than 7 days.
MAX_EVENT_AGE_SECONDS = 7 * 24 * 3600
MAX_EVENT_FUTURE_SECONDS = 10 * 60
# S2-1: a conversion whose order was placed this long ago (or longer) is never sent (margin under the 7-day limit).
LATE_DELIVERY_CUTOFF = timedelta(days=6.5)
# Statuses that must never emit, whatever the payment method. ("partially_refunded" is deliberately NOT here:
# a partial refund is still a delivered/paid order, valued at the net collected amount, S2-4.)
NON_REVENUE_STATUSES = frozenset({
    "cancelled", "canceled", "refunded", "voided",
    "restored", "returned", "failed_delivery"
})


@dataclass(frozen=True)
class EventType:
    """
    One CAPI event kind on the D-005 signal ladder. Event name/id/eligibility live here (not as scattered string
    literals) so a second event type (ConfirmedOrder, S2-2) is added by declaring another EventType.
    """
    name: str
    id_prefix: str
    converting_statuses: frozenset

    def event_id(self, order_id: Any) -> str:
        return f"{self.id_prefix}{order_id}"


DELIVERED_EVENT = EventType(DELIVERED_EVENT_NAME, "delivered_", CONVERTING_STATUSES)
EVENT_TYPES: Dict[str, EventType] = {DELIVERED_EVENT.name: DELIVERED_EVENT}

def quality_flags_for(payload: Dict[str, Any]) -> Optional[str]:
    """
    Comma-separated flags for required-for-website fields the event is MISSING (Meta documents client_user_agent and
    event_source_url as required for action_source=website; what it does without them is undocumented), or None.
    Recorded on capi_events.quality_flags at send time so the pilot can compare accepted vs discarded events.
    Nothing is ever fabricated to fill a gap.
    """
    event = (payload.get("data") or [{}])[0]
    flags = []
    if not (event.get("user_data") or {}).get("client_user_agent"):
        flags.append("missing_user_agent")
    if not event.get("event_source_url"):
        flags.append("missing_event_source_url")
    return ",".join(flags) or None

def hash_sha256(val: Optional[str]) -> Optional[str]:
    """Normalizes and hashes a string using SHA-256."""
    if not val:
        return None
    cleaned = str(val).strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

def is_stale_event_time(event_time: int, now: Optional[float] = None) -> bool:
    """True if event_time is older than Meta's 7-day window (or more than 10 minutes in the future)."""
    now = time.time() if now is None else now
    age = now - float(event_time)
    return age > MAX_EVENT_AGE_SECONDS or age < -MAX_EVENT_FUTURE_SECONDS

COUNTRY_BY_CURRENCY = {"EGP": "EG", "SAR": "SA"}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

def normalize_phone(phone: Optional[str], default_country: str = "EG") -> Optional[str]:
    """Normalizes phone to digits-only E.164 representation before hashing."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if digits.startswith("00"):
        # International call prefix (0020..., 00966...) -> country code
        digits = digits[2:]
    elif default_country == "EG" and digits.startswith("01"):
        digits = "2" + digits
    elif default_country == "SA" and digits.startswith("05"):
        digits = "966" + digits[1:]
    if len(digits) == 9 and digits.startswith("5"):
        # Saudi mobile written without the trunk zero or country code (5XXXXXXXX)
        digits = "966" + digits
    return digits

def hash_email(email: Optional[str]) -> Optional[str]:
    """SHA-256 of the normalized (trimmed, lower-cased) email, or None. Safe to store; the raw email is not."""
    return hash_sha256(email)

def hash_phone(phone: Optional[str], currency: Optional[str] = None, country: Optional[str] = None) -> Optional[str]:
    """
    SHA-256 of the normalized phone, or None. The default country comes from the currency when it is
    mapped (same rule as build_event_payload), else from `country`, else EG. Safe to store.
    """
    if not phone:
        return None
    default_country = COUNTRY_BY_CURRENCY.get((currency or "").upper()) or (country or "EG").upper()
    return hash_sha256(normalize_phone(phone, default_country))

def _validated_hash(value: str, field: str) -> str:
    """Pre-hashed values must already be a SHA-256 hex digest; anything else would be double-hashed or invalid."""
    cleaned = str(value).strip().lower()
    if not _SHA256_HEX.match(cleaned):
        raise ValueError(f"{field} must be a 64-char SHA-256 hex digest (already hashed); got an unhashed value")
    return cleaned

# --- S2-3 match keys ---------------------------------------------------------------------------------------
# Keys stored as hashes on the order (column f"{key}_hash") and sent in user_data under the same name.
MATCH_KEYS = ("external_id", "fn", "ln", "ct", "st", "zp", "country")
_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_COUNTRY_CODE = re.compile(r"^[a-z]{2}$")

def _collapse(value: Any) -> str:
    return _WS.sub(" ", str(value)).strip().lower()

def normalize_match_value(key: str, value: Any) -> Optional[str]:
    """
    Meta normalization for one match key (before SHA-256), or None if nothing usable remains:
    fn/ln/st trim + lowercase + collapse spaces (Arabic is unaffected beyond trim/collapse); ct lowercase with
    spaces and punctuation removed; zp lowercase with spaces and dashes removed; country ISO-3166 alpha-2 lowercase
    (anything else dropped); external_id trimmed as-is (ids are opaque, case preserved).
    """
    if value is None or isinstance(value, bool):
        return None
    if key == "external_id":
        text = str(value).strip()
        return text or None
    text = _collapse(value)
    if key == "ct":
        text = _NON_WORD.sub("", text)
    elif key == "zp":
        text = re.sub(r"[\s\-]+", "", text)
    elif key == "country":
        text = text if _COUNTRY_CODE.match(text) else ""
    elif key not in ("fn", "ln", "st"):
        raise ValueError(f"unknown match key {key!r}")
    return text or None

def hash_match_value(key: str, value: Any) -> Optional[str]:
    """SHA-256 hex of the normalized match value, or None. Safe to store; the raw value is not."""
    normalized = normalize_match_value(key, value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None

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
        test_event_code: Optional[str] = None,
        event_time: Optional[int] = None,
        email_hash: Optional[str] = None,
        phone_hash: Optional[str] = None,
        match_hashes: Optional[Dict[str, Optional[str]]] = None,
        event_source_url: Optional[str] = None,
        now: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Builds a compliant Meta Conversions API JSON payload.
        match_hashes (S2-3): already-hashed values keyed by MATCH_KEYS (external_id, fn, ln, ct, st, zp, country);
        empty ones are skipped. event_source_url goes on the event itself. `now` (unix seconds) overrides the clock
        for the staleness check only.
        Ensures Zero-PII by hashing email and phone with SHA-256.
        email_hash / phone_hash accept values that are ALREADY hashed (e.g. read back from storage, where only
        hashes are kept); they are used as-is (never re-hashed) and win over the raw email / phone.
        event_time (unix seconds) defaults to now; an explicit value older than 7 days (or >10 min ahead) raises ValueError
        because Meta would reject the whole request.
        """
        if event_time is None:
            event_time = int(time.time())
        elif is_stale_event_time(event_time, now):
            raise ValueError("event_time is older than 7 days or in the future; Meta rejects the whole request")
        user_data: Dict[str, Any] = {}
        if email_hash:
            user_data["em"] = [_validated_hash(email_hash, "email_hash")]
        elif email:
            user_data["em"] = [hash_sha256(email)]
        if phone_hash:
            user_data["ph"] = [_validated_hash(phone_hash, "phone_hash")]
        elif phone:
            default_country = COUNTRY_BY_CURRENCY.get(currency.upper(), "EG")
            user_data["ph"] = [hash_sha256(normalize_phone(phone, default_country))]
        for key in MATCH_KEYS:
            hashed = (match_hashes or {}).get(key)
            if hashed:
                user_data[key] = [_validated_hash(hashed, key)]
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
            "event_time": int(event_time),
            "event_id": event_id,
            "action_source": "website",
            "user_data": user_data,
            "custom_data": {
                "currency": currency.upper(),
                "value": float(value),
                "order_id": str(order_id)
            }
        }

        if event_source_url:
            event_obj["event_source_url"] = event_source_url

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
        # Meta documents the token as the `access_token` parameter (no Authorization header).
        # It goes in the JSON body, not the URL, so HTTP client/proxy URL logs never contain it.
        headers = {"Content-Type": "application/json"}
        body = {**payload, "access_token": access_token}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, json=body)
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
            # Log only the exception type: never the request body, which carries the access token.
            logger.error("Meta CAPI dispatch failed: %s", type(e).__name__)
            return {
                "status": "failed",
                "error": type(e).__name__
            }

    def check_eligibility(
        self,
        order_id: str,
        status: str,
        value: float,
        is_cod: bool = True,
        event_type: EventType = DELIVERED_EVENT
    ) -> Optional[Dict[str, Any]]:
        """
        Status/value part of Rule D-005. Returns a SUPPRESSED or DEFERRED decision, or None when the order is
        eligible for `event_type` (no payload is built here).
        """
        norm_status = status.strip().lower()
        if norm_status in NON_REVENUE_STATUSES:
            return {
                "action": "SUPPRESSED",
                "reason": "Rule D-005: order is cancelled/refunded/voided. No conversion is sent.",
                "order_id": order_id,
                "current_status": status
            }
        if norm_status not in event_type.converting_statuses:
            return {
                "action": "DEFERRED",
                "reason": (
                    "Rule D-005: COD order is not delivered/paid yet. Conversion held until delivery."
                    if is_cod else
                    "Rule D-005: prepaid order is not paid yet. Conversion held until payment."
                ),
                "order_id": order_id,
                "current_status": status,
                "held_event": event_type.name
            }
        if not value or float(value) <= 0:
            return {
                "action": "SUPPRESSED",
                "reason": "Rule D-005: net collected value is zero or negative (fully refunded/free). No conversion is sent.",
                "order_id": order_id,
                "current_status": status
            }
        return None

    def process_cod_order_event(
        self,
        order_id: str,
        status: str,
        value: float,
        currency: str,
        is_cod: bool = True,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        test_event_code: Optional[str] = None,
        event_time: Optional[int] = None,
        email_hash: Optional[str] = None,
        phone_hash: Optional[str] = None,
        fbp: Optional[str] = None,
        fbc: Optional[str] = None,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        match_hashes: Optional[Dict[str, Optional[str]]] = None,
        event_source_url: Optional[str] = None,
        placed_at: Optional[int] = None,
        now: Optional[float] = None,
        event_type: EventType = DELIVERED_EVENT
    ) -> Dict[str, Any]:
        """
        Implements Rule D-005 (Coexist): decides whether to send the custom 'DeliveredPurchase' (or another EventType).
        - cancelled / fully refunded / voided (any payment method), or a net value <= 0: SUPPRESSED, never emitted.
          A partially refunded order is NOT suppressed: `value` is the net collected amount.
        - COD and status not 'delivered'/'paid' (incl. 'shipped'/'fulfilled'): DEFERRED until delivery.
        - Prepaid and status not 'paid'/'delivered': DEFERRED until payment.
        - placed_at (unix seconds, the order's placed time) given and now >= placed_at + LATE_DELIVERY_CUTOFF:
          LATE_DELIVERY (S2-1), never sent, so the upload stays inside Meta's 7-day limit.
        - Explicit event_time older than 7 days or >10 min in the future: STALE (Meta would reject the whole request).
        - Otherwise READY_TO_EMIT with event_id = <prefix><order_id> and event_time = event_time (the pipeline passes the
          order's placed time; default now for the stateless path).
        The standard 'Purchase' is left to the merchant's native Shopify/Salla integration.
        fbp / fbc / client_ip / user_agent improve match quality and go into user_data unhashed (as Meta requires).
        client_ip / user_agent come from the encrypted checkout_context (D-006) and are never taken from the webhook
        server's own connection. match_hashes / event_source_url: see build_event_payload. `now` (unix seconds)
        overrides the clock for the cutoff and staleness checks.
        """
        event_id = event_type.event_id(order_id)
        held = self.check_eligibility(order_id, status, value, is_cod, event_type)
        if held is not None:
            return held

        if placed_at is not None and (time.time() if now is None else now) >= placed_at + LATE_DELIVERY_CUTOFF.total_seconds():
            return {
                "action": "LATE_DELIVERY",
                "reason": "Rule D-005: order was placed too long ago (past the placed+6.5d cutoff); Meta would reject "
                          "or ignore it. Not sent.",
                "order_id": order_id,
                "current_status": status
            }

        if event_time is not None and is_stale_event_time(event_time, now):
            return {
                "action": "STALE",
                "reason": "Rule D-005: event_time is older than 7 days or in the future; Meta rejects the whole request. Not sent.",
                "order_id": order_id,
                "current_status": status
            }

        # Delivered (COD) or paid (prepaid) -> emit the custom conversion
        payload = self.build_event_payload(
            event_name=event_type.name,
            event_id=event_id,
            order_id=order_id,
            value=value,
            currency=currency,
            email=email,
            phone=phone,
            fbp=fbp,
            fbc=fbc,
            client_ip=client_ip,
            user_agent=user_agent,
            test_event_code=test_event_code,
            event_time=event_time,
            email_hash=email_hash,
            phone_hash=phone_hash,
            match_hashes=match_hashes,
            event_source_url=event_source_url,
            now=now
        )

        return {
            "action": "READY_TO_EMIT",
            "event_name": event_type.name,
            "event_id": event_id,
            "order_id": order_id,
            "payload": payload
        }

capi_sender = MetaCAPISender()
