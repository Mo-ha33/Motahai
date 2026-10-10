"""
capi_service.py — Production Meta Conversions API (CAPI) & Rule D-005 Engine
Handles server-side conversion delivery to the Meta Graph API (version pinned in META_GRAPH_API_VERSION).

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
import os
import time
import re
from datetime import date
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Any, Optional, List
import httpx

logger = logging.getLogger("ameen_workforce.capi")

# Meta expires each Graph API version about two years after release; a call to an expired version is silently served by
# the oldest live version instead of failing. v20.0 expired 2026-09-24. Expiration dates are from Meta's version table:
# https://developers.facebook.com/docs/graph-api/changelog/versions (tests fail 90 days before the pinned one expires).
META_GRAPH_API_VERSION = "v22.0"
META_GRAPH_API_VERSION_EXPIRES = date(2027, 5, 20)
# Operator override (e.g. to move to a newer version before a release); must look like "v23.0". Read per send.
META_GRAPH_API_VERSION_ENV = "MOTAHAI_META_GRAPH_API_VERSION"
_GRAPH_VERSION = re.compile(r"^v[0-9]{2,3}\.0$")


def graph_api_version() -> str:
    """The Graph API version to call: the env override when it is a valid version string, else the pinned default."""
    override = (os.environ.get(META_GRAPH_API_VERSION_ENV) or "").strip()
    if not override:
        return META_GRAPH_API_VERSION
    if not _GRAPH_VERSION.match(override):
        raise ValueError(f"{META_GRAPH_API_VERSION_ENV} must look like 'v23.0'")
    return override

# Rule D-005 (Coexist): custom events, never the standard Purchase the native integration sends.
DELIVERED_EVENT_NAME = "DeliveredPurchase"
CONFIRMED_EVENT_NAME = "ConfirmedOrder"
# Statuses that mean revenue is real: COD cash collected / delivered, or prepaid and paid.
CONVERTING_STATUSES = frozenset({"delivered", "paid"})
# ConfirmedOrder eligibility is decided per tenant by confirmation.py (merchant tags, merchant-configured statuses,
# implicit shipped/paid), NOT by a global status list. Salla `in_review` / `under_review` mean the order is still
# AWAITING review (before confirmation) and are deliberately not confirmation signals. Kept empty for compatibility.
CONFIRMED_STATUSES: frozenset = frozenset()
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
    literals).
    - Step 2: ConfirmedOrder (S2-2) -> Fired when the order is genuinely confirmed (merchant tag, configured status,
      shipped/paid, or manual call/WhatsApp confirmation); never for orders merely awaiting review.
    - Step 3: DeliveredPurchase (S2-1) -> Fired when delivered & cash collected.
    """
    name: str
    id_prefix: str
    converting_statuses: frozenset

    def event_id(self, order_id: Any) -> str:
        return f"{self.id_prefix}{order_id}"


DELIVERED_EVENT = EventType(DELIVERED_EVENT_NAME, "delivered_", CONVERTING_STATUSES)
CONFIRMED_EVENT = EventType(CONFIRMED_EVENT_NAME, "confirmed_", CONFIRMED_STATUSES)
EVENT_TYPES: Dict[str, EventType] = {
    DELIVERED_EVENT.name: DELIVERED_EVENT,
    CONFIRMED_EVENT.name: CONFIRMED_EVENT,
}

def quality_flags_for(payload: Dict[str, Any]) -> Optional[str]:
    """
    Comma-separated flags for required-for-website fields the event is MISSING (Meta documents client_user_agent and
    event_source_url as required for action_source=website; what it does without them is undocumented), plus
    `test_event` when the payload carries a test_event_code (it went to Events Manager's Test Events), or None.
    Recorded on capi_events.quality_flags at send time so the pilot can compare accepted vs discarded events.
    Nothing is ever fabricated to fill a gap.
    """
    event = (payload.get("data") or [{}])[0]
    flags = []
    if not (event.get("user_data") or {}).get("client_user_agent"):
        flags.append("missing_user_agent")
    if not event.get("event_source_url"):
        flags.append("missing_event_source_url")
    if payload.get("test_event_code"):
        flags.append("test_event")
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

# Default phone country for an order's currency (MENA/GCC). Used when no shipping country is known.
COUNTRY_BY_CURRENCY = {
    "EGP": "EG", "SAR": "SA", "AED": "AE", "KWD": "KW", "QAR": "QA", "BHD": "BH", "OMR": "OM",
}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# E.164 numbering plans: country -> (calling code, national significant number lengths, uses a trunk "0").
# Mobile and landline lengths (ITU-T E.164 national plans): EG mobile 10 / Cairo landline 9; SA and AE mobile 9 /
# landline 8; KW, QA, BH, OM 8 digits with no trunk prefix.
PHONE_PLANS = {
    "EG": ("20", (9, 10), True),
    "SA": ("966", (8, 9), True),
    "AE": ("971", (8, 9), True),
    "KW": ("965", (8,), False),
    "QA": ("974", (8,), False),
    "BH": ("973", (8,), False),
    "OM": ("968", (8,), False),
}
# E.164 allows at most 15 digits; nothing shorter than 8 (calling code + subscriber) is a real number.
E164_MIN_DIGITS, E164_MAX_DIGITS = 8, 15
# Longest calling code first, so "966" is tried before shorter prefixes.
_PLANS_BY_CODE = sorted(PHONE_PLANS.values(), key=lambda plan: -len(plan[0]))


def _strip_trunk_after_code(digits: str) -> str:
    """'+20 010...' or '00966 05...' carry a trunk 0 after the calling code that E.164 drops."""
    for code, lengths, trunk in _PLANS_BY_CODE:
        rest = digits[len(code):]
        if trunk and digits.startswith(code) and rest.startswith("0") and len(rest) - 1 in lengths:
            return code + rest[1:]
    return digits


def normalize_phone(phone: Optional[str], default_country: str = "EG") -> Optional[str]:
    """
    Normalizes a phone to E.164 digits (country code first, no '+', no trunk zero) before hashing, as Meta requires.
    - '+...' or '00...': already international; only a trunk 0 right after a known calling code is dropped.
    - Digits already starting with a known calling code and the right national length are kept (e.g. 9665XXXXXXXX).
    - Otherwise the number is national for `default_country` (EG, SA, AE, KW, QA, BH, OM): one trunk 0 is dropped
      where that country uses one, and the calling code is prefixed when the length fits its numbering plan. A
      number written without its trunk 0 is only prefixed at the mobile length, the one unambiguous case.
    - Never guessed into a country. A national number that fits no plan of a known default country returns None (its
      hash could never match), except the historical rule that a bare 9-digit 5XXXXXXXX is Saudi. International
      numbers and numbers for an unknown default country are kept only at 8 to 15 digits, else None.
    """
    if not phone:
        return None
    raw = str(phone).strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+") or digits.startswith("00"):
        international = _strip_trunk_after_code(digits[2:] if digits.startswith("00") else digits)
        return international if E164_MIN_DIGITS <= len(international) <= E164_MAX_DIGITS else None

    country = (default_country or "").upper()
    plan = PHONE_PLANS.get(country)
    if plan:
        code, lengths, trunk = plan
        if digits.startswith(code) and len(digits) - len(code) in lengths:
            return digits
        if trunk and digits.startswith("0"):
            if len(digits) - 1 in lengths:
                return code + digits[1:]
        elif not trunk and len(digits) in lengths:
            return code + digits
        elif len(digits) == max(lengths):
            # Without its trunk 0 only the mobile length is unambiguous (e.g. AE 5XXXXXXXX, EG 1XXXXXXXXX).
            return code + digits
    for code, lengths, _trunk in _PLANS_BY_CODE:
        if digits.startswith(code) and len(digits) - len(code) in lengths:
            return digits
    if len(digits) == 9 and digits.startswith("5"):
        # Historical rule: a Saudi mobile written without the trunk zero or country code (5XXXXXXXX). SA and AE
        # defaults already resolved it above, so this only applies to other defaults.
        return "966" + digits
    if plan:
        return None  # fits no numbering plan we know: a hash of it could never match, so send no phone at all
    return digits if E164_MIN_DIGITS <= len(digits) <= E164_MAX_DIGITS else None

def hash_email(email: Optional[str]) -> Optional[str]:
    """SHA-256 of the normalized (trimmed, lower-cased) email, or None. Safe to store; the raw email is not."""
    return hash_sha256(email)

def phone_default_country(currency: Optional[str] = None, country: Optional[str] = None,
                          ship_country: Optional[str] = None) -> str:
    """
    Country whose national format a phone without a calling code is read in: the order's shipping country when it
    has a known numbering plan, else the currency's country, else `country` (the tenant's), else EG.
    """
    ship = (ship_country or "").strip().upper()
    if ship in PHONE_PLANS:
        return ship
    return COUNTRY_BY_CURRENCY.get((currency or "").strip().upper()) or (country or "EG").strip().upper()

def hash_phone(phone: Optional[str], currency: Optional[str] = None, country: Optional[str] = None,
               ship_country: Optional[str] = None) -> Optional[str]:
    """
    SHA-256 of the E.164-normalized phone, or None. The default country is chosen by phone_default_country
    (shipping country, then currency, then `country`, then EG). Safe to store.
    """
    if not phone:
        return None
    return hash_sha256(normalize_phone(phone, phone_default_country(currency, country, ship_country)))

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
            user_data["ph"] = [hash_phone(phone, currency)]
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
        # Meta documents the token as the `access_token` parameter (no Authorization header).
        # It goes in the JSON body, not the URL, so HTTP client/proxy URL logs never contain it.
        headers = {"Content-Type": "application/json"}
        body = {**payload, "access_token": access_token}

        try:
            url = f"https://graph.facebook.com/{graph_api_version()}/{pixel_id}/events"  # bad override -> ValueError
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
        event_type: EventType = DELIVERED_EVENT,
        is_confirmed: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Status/value part of Rule D-005. Returns a SUPPRESSED or DEFERRED decision, or None when the order is
        eligible for `event_type` (no payload is built here).
        is_confirmed=True satisfies eligibility for ConfirmedOrder (e.g. via merchant tag 'confirmed').
        """
        norm_status = status.strip().lower()
        if norm_status in NON_REVENUE_STATUSES:
            return {
                "action": "SUPPRESSED",
                "reason": "Rule D-005: order is cancelled/refunded/voided. No conversion is sent.",
                "order_id": order_id,
                "current_status": status
            }
        is_eligible_status = (norm_status in event_type.converting_statuses) or (event_type == CONFIRMED_EVENT and is_confirmed)
        if not is_eligible_status:
            if event_type == CONFIRMED_EVENT:
                reason = "Rule D-005: order is not confirmed yet. Conversion held until confirmation."
            elif is_cod:
                reason = "Rule D-005: COD order is not delivered/paid yet. Conversion held until delivery."
            else:
                reason = "Rule D-005: prepaid order is not paid yet. Conversion held until payment."
            return {
                "action": "DEFERRED",
                "reason": reason,
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
        event_type: EventType = DELIVERED_EVENT,
        is_confirmed: bool = False
    ) -> Dict[str, Any]:
        """
        Implements Rule D-005 (Coexist): decides whether to send the custom 'DeliveredPurchase' or 'ConfirmedOrder'.
        - cancelled / fully refunded / voided (any payment method), or a net value <= 0: SUPPRESSED, never emitted.
          A partially refunded order is NOT suppressed: `value` is the net collected amount.
        - COD and status not 'delivered'/'paid' (incl. 'shipped'/'fulfilled'): DEFERRED until delivery (for DeliveredPurchase).
        - Prepaid and status not 'paid'/'delivered': DEFERRED until payment (for DeliveredPurchase).
        - Not confirmed and event_type is ConfirmedOrder: DEFERRED until confirmed.
        - For DeliveredPurchase: placed_at given and now >= placed_at + LATE_DELIVERY_CUTOFF:
          LATE_DELIVERY (S2-1), never sent, so the upload stays inside Meta's 7-day limit.
        - Explicit event_time older than 7 days or >10 min in the future: STALE (Meta would reject the whole request).
        - Otherwise READY_TO_EMIT with event_id = <prefix><order_id> and event_time = event_time.
        """
        event_id = event_type.event_id(order_id)
        held = self.check_eligibility(order_id, status, value, is_cod, event_type, is_confirmed=is_confirmed)
        if held is not None:
            return held

        if event_type == DELIVERED_EVENT and placed_at is not None and (time.time() if now is None else now) >= placed_at + LATE_DELIVERY_CUTOFF.total_seconds():
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

    def process_confirmed_order_event(
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
        now: Optional[float] = None,
        is_confirmed: bool = True
    ) -> Dict[str, Any]:
        """
        Processes ConfirmedOrder event (Step 2 on the 3-step signal ladder).
        Fired when customer confirms via call, WhatsApp bot, or merchant tag.
        event_id = confirmed_<order_id>, event_time = confirmation timestamp (now or webhook time),
        value = full order total.
        """
        return self.process_cod_order_event(
            order_id=order_id,
            status=status,
            value=value,
            currency=currency,
            is_cod=is_cod,
            email=email,
            phone=phone,
            test_event_code=test_event_code,
            event_time=event_time,
            email_hash=email_hash,
            phone_hash=phone_hash,
            fbp=fbp,
            fbc=fbc,
            client_ip=client_ip,
            user_agent=user_agent,
            match_hashes=match_hashes,
            event_source_url=event_source_url,
            placed_at=None,
            now=now,
            event_type=CONFIRMED_EVENT,
            is_confirmed=is_confirmed
        )

capi_sender = MetaCAPISender()
