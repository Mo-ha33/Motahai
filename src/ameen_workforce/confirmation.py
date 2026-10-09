"""
confirmation.py — Which orders count as CONFIRMED for the ConfirmedOrder signal (Step 2 of the D-005 ladder).

ConfirmedOrder must mean "a human or a bot confirmed this order", never "the order exists". Salla's `under_review` /
`in_review` mean the order is still AWAITING review (before confirmation), so they are NOT confirmation signals.

Confirmation sources (stored in orders.confirmation_source):
  tag              an explicit merchant tag (exact, case-insensitive, trimmed match; never a substring)
  status           a Salla status slug/name the merchant configured as "confirmed" for their store
  implicit_shipped the order was actually shipped/fulfilled or delivered (needs rules.implicit_on_ship)
  implicit_paid    a prepaid order is paid (needs rules.implicit_on_ship)
  manual           emit_confirmed_order() (call center / WhatsApp bot)

Per-tenant configuration lives in tenants.confirmation_rules (JSON, NULL = defaults):
  {"tags": [...], "statuses": [...], "implicit_on_ship": true}
A key that is present REPLACES its default list; a missing key keeps the default.

Cancelled / refunded / voided orders are never confirmed, and a cancellation tag (cod-cancelled, ...) blocks every
automatic source, not only the tag one.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from .capi_service import NON_REVENUE_STATUSES

logger = logging.getLogger("ameen_workforce.confirmation")

SOURCE_TAG = "tag"
SOURCE_STATUS = "status"
SOURCE_IMPLICIT_SHIPPED = "implicit_shipped"
SOURCE_IMPLICIT_PAID = "implicit_paid"
SOURCE_MANUAL = "manual"
CONFIRMATION_SOURCES = (SOURCE_TAG, SOURCE_STATUS, SOURCE_IMPLICIT_SHIPPED, SOURCE_IMPLICIT_PAID, SOURCE_MANUAL)

# Explicit merchant tags, matched exactly (case-insensitive, trimmed).
DEFAULT_CONFIRMATION_TAGS = ("confirmed", "cod-confirmed", "order-confirmed", "تم التأكيد", "مؤكد")
# Merchant-specific Salla statuses: EMPTY by default; the pilot configures the merchant's real confirmed status.
DEFAULT_CONFIRMATION_STATUSES: tuple = ()
DEFAULT_IMPLICIT_ON_SHIP = True
# A present cancellation tag blocks automatic confirmation (COD apps write these when the customer declines).
CANCELLATION_TAGS = frozenset({
    "cod-cancelled", "cod-canceled", "cancelled-by-customer", "canceled-by-customer",
    "cancelled", "canceled", "order-cancelled", "order-canceled", "ملغي", "ملغى",
})

MAX_RULE_ITEMS = 50
MAX_RULE_ITEM_LEN = 64
RULE_KEYS = ("tags", "statuses", "implicit_on_ship")


def normalize_token(value: Any) -> str:
    """Trimmed, lower-cased text used for exact tag/status comparison."""
    return str(value).strip().lower()


def _clean_list(name: str, value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"confirmation_rules.{name} must be a list of strings")
    if len(value) > MAX_RULE_ITEMS:
        raise ValueError(f"confirmation_rules.{name} has more than {MAX_RULE_ITEMS} items")
    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"confirmation_rules.{name} must contain only strings")
        token = normalize_token(item)
        if not token:
            raise ValueError(f"confirmation_rules.{name} must not contain empty strings")
        if len(token) > MAX_RULE_ITEM_LEN:
            raise ValueError(f"confirmation_rules.{name} items must be at most {MAX_RULE_ITEM_LEN} characters")
        if token not in cleaned:
            cleaned.append(token)
    return cleaned


def validate_confirmation_rules(rules: Any) -> Dict[str, Any]:
    """
    Validates a rules dict for writing and returns the normalized version (only the keys given, tags/statuses
    lower-cased and de-duplicated). Raises ValueError on anything else: unknown keys, non-list tags/statuses, non-string
    or over-long items, a non-bool implicit_on_ship.
    """
    if not isinstance(rules, dict):
        raise ValueError("confirmation_rules must be a dict")
    unknown = set(rules) - set(RULE_KEYS)
    if unknown:
        raise ValueError(f"unknown confirmation_rules keys: {sorted(unknown)}")
    out: Dict[str, Any] = {}
    for key in ("tags", "statuses"):
        if key in rules:
            out[key] = _clean_list(key, rules[key])
    if "implicit_on_ship" in rules:
        if not isinstance(rules["implicit_on_ship"], bool):
            raise ValueError("confirmation_rules.implicit_on_ship must be a bool")
        out["implicit_on_ship"] = rules["implicit_on_ship"]
    return out


def default_confirmation_rules() -> Dict[str, Any]:
    return {
        "tags": [normalize_token(t) for t in DEFAULT_CONFIRMATION_TAGS],
        "statuses": [normalize_token(s) for s in DEFAULT_CONFIRMATION_STATUSES],
        "implicit_on_ship": DEFAULT_IMPLICIT_ON_SHIP,
    }


def resolve_confirmation_rules(rules: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Full rules (all three keys) from a stored/partial rules dict; None or an invalid value gives the defaults."""
    resolved = default_confirmation_rules()
    if rules is None:
        return resolved
    try:
        resolved.update(validate_confirmation_rules(rules))
    except ValueError as e:
        logger.error("Ignoring invalid confirmation_rules (%s); using defaults", e)
    return resolved


def effective_confirmation_rules(tenant: Any) -> Dict[str, Any]:
    """The tenant's effective rules: tenants.confirmation_rules merged over the defaults (NULL/None tenant = defaults)."""
    return resolve_confirmation_rules(getattr(tenant, "confirmation_rules", None))


def set_confirmation_rules(session, tenant, rules: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Validates and stores `rules` on the tenant and commits. rules=None clears the override (back to defaults).
    Raises ValueError for invalid input (nothing is written). Returns the effective rules.
    """
    tenant.confirmation_rules = None if rules is None else validate_confirmation_rules(rules)
    session.commit()
    return effective_confirmation_rules(tenant)


def normalized_tags(raw: Any) -> List[str]:
    """Tags from a comma-separated string or a list, trimmed and lower-cased; empty ones dropped."""
    if isinstance(raw, str):
        items: Iterable[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return []
    return [t for t in (normalize_token(i) for i in items if i is not None) if t]


def has_cancellation_tag(tags: Iterable[str]) -> bool:
    return any(t in CANCELLATION_TAGS for t in tags)


def evaluate_confirmation(
    rules: Optional[Dict[str, Any]],
    *,
    effective_status: Optional[str],
    tags: Iterable[str] = (),
    status_candidates: Iterable[str] = (),
    shipped: bool = False,
    prepaid_paid: bool = False,
) -> Optional[str]:
    """
    The confirmation source for an order, or None when it is not confirmed (yet).
    effective_status: the order's pipeline status (cancelled/refunded/voided/... never confirm).
    tags: normalized order tags. status_candidates: the platform status slug/name(s) to compare with rules.statuses.
    shipped: the order was actually shipped/fulfilled/delivered. prepaid_paid: a prepaid order is paid.
    Priority: tag > status > implicit_shipped > implicit_paid.
    """
    if effective_status is not None and effective_status.strip().lower() in NON_REVENUE_STATUSES:
        return None
    rules = resolve_confirmation_rules(rules)
    tags = [normalize_token(t) for t in tags]
    if has_cancellation_tag(tags):
        return None
    if any(t in rules["tags"] for t in tags):
        return SOURCE_TAG
    wanted = set(rules["statuses"])
    if wanted and any(normalize_token(s) in wanted for s in status_candidates if s):
        return SOURCE_STATUS
    if rules["implicit_on_ship"]:
        if shipped:
            return SOURCE_IMPLICIT_SHIPPED
        if prepaid_paid:
            return SOURCE_IMPLICIT_PAID
    return None
