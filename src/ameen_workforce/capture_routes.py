"""
capture_routes.py — Public storefront capture endpoint (FX-2, replaces the unsafe /storefront/capture).

  POST    /v1/capture/{tenant_key}   tenant_key = tenants.shop_domain (Shopify *.myshopify.com domain / Salla merchant id)
  OPTIONS /v1/capture/{tenant_key}   CORS preflight

The endpoint is public and unauthenticated by nature (it is called from a customer's browser), so it is built to be
harmless when abused:
  * It NEVER writes to `orders`. It stores a quarantined `pending_captures` row; capture.merge_pending_captures joins
    it to an order later, after a total check (second factor) against the signed webhook's data. See capture.py.
  * IP and user agent come from the CONNECTION / User-Agent header only. `client_ip`, `ip` and `user_agent` keys in the
    body are ignored. X-Forwarded-For's first hop is honored only when env MOTAHAI_TRUSTED_PROXY=1 (set it only when
    the app is behind a proxy that overwrites X-Forwarded-For with the real peer address, e.g. nginx
    `proxy_set_header X-Forwarded-For $remote_addr;`; with `$proxy_add_x_forwarded_for` the first hop is attacker
    controlled). Non-public addresses (loopback/private/reserved) are dropped rather than stored.
  * CORS: only the tenant's own storefront origin (origin of tenants.storefront_url, or https://<shop_domain> for
    Shopify) is accepted; anything else, or a missing Origin, gets 403. Access-Control-Allow-Origin echoes that exact
    origin (never `*`), no credentials. A tenant without a usable origin (Salla without storefront_url) is rejected.
    NOTE: service.py's global CORSMiddleware answers preflights from origins it does not list with 400 before this
    router sees them, so the storefront snippet must send a "simple" request (text/plain, no custom headers) which
    never triggers a preflight. The OPTIONS route exists for completeness/when a preflight does reach the router.
  * Body <= 4 KB (413), JSON or text/plain only (sendBeacon sends text/plain), strict pydantic schema, unknown keys
    ignored, attribution passed through webhook_listener.sanitize_attribution.
  * Rate limit: 30 requests/min per client IP and 600/min per tenant -> 429. The limiter is IN-PROCESS memory:
    multi-process or multi-host deployments (gunicorn workers, several containers) multiply the effective limits and
    need a shared limiter (e.g. Redis). The tenant bucket is only consumed by requests that passed the Origin check,
    but Origin is trivially spoofed outside a browser, so a determined attacker can exhaust a tenant's bucket (denying
    that tenant's real captures for a minute). Accepted; captures are best-effort enrichment.
  * Every accepted request gets an empty 204, whether or not an order with that id exists. Nothing sensitive (IP, UA,
    order ids, attribution) is ever logged.
"""

import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy.orm import sessionmaker

from .credentials import CredentialError, encrypt_value
from .db import PendingCapture, Tenant, get_tenant_by_shop_domain, utcnow
from .webhook_listener import sanitize_attribution, sanitize_client_ip, sanitize_user_agent
from .webhook_routes import get_session_factory_dep

logger = logging.getLogger("ameen_workforce.capture")

router = APIRouter()

MAX_CAPTURE_BODY_BYTES = 4096
CAPTURE_TTL = timedelta(hours=48)
TRUSTED_PROXY_ENV = "MOTAHAI_TRUSTED_PROXY"
IP_LIMIT_PER_MIN = 30
TENANT_LIMIT_PER_MIN = 600
RATE_WINDOW_SECONDS = 60.0
_MAX_LIMITER_KEYS = 50_000
_ALLOWED_CONTENT_TYPES = ("application/json", "text/plain")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


# --- rate limiter (in-process; see module docstring) --------------------------------------------------------

class SlidingWindowLimiter:
    """Allows `limit` hits per `window` seconds per key. In-process only (a shared limiter is needed across workers)."""

    def __init__(self, limit: int, window: float = RATE_WINDOW_SECONDS):
        self.limit = limit
        self.window = window
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= _MAX_LIMITER_KEYS:
                    self._prune(cutoff)
                hits = self._hits[key] = deque()
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def _prune(self, cutoff: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or h[-1] <= cutoff]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


ip_limiter = SlidingWindowLimiter(IP_LIMIT_PER_MIN)
tenant_limiter = SlidingWindowLimiter(TENANT_LIMIT_PER_MIN)


# --- schema --------------------------------------------------------------------------------------------------

class CaptureBody(BaseModel):
    """Strict capture payload. Unknown fields (including client_ip / ip / user_agent) are ignored, never read."""
    model_config = ConfigDict(extra="ignore")

    order_id: Any
    order_total: Any
    currency: Any
    attribution: Optional[Dict[str, Any]] = None

    @field_validator("order_id")
    @classmethod
    def _order_id(cls, v: Any) -> str:
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("order_id must be a string or integer")
        s = str(v).strip()
        if not s or len(s) > 64 or re.search(r"[\x00-\x1f\x7f]", s):
            raise ValueError("order_id invalid")
        return s

    @field_validator("order_total")
    @classmethod
    def _order_total(cls, v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("order_total must be a number")
        v = float(v)
        if not math.isfinite(v) or v < 0 or v > 1e12:
            raise ValueError("order_total out of range")
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: Any) -> str:
        if not isinstance(v, str) or not _CURRENCY.match(v.strip()):
            raise ValueError("currency must be 3 letters")
        return v.strip().upper()


# --- helpers -------------------------------------------------------------------------------------------------

def _origin_of(url: Optional[str]) -> Optional[str]:
    """Normalized scheme://host[:port] of `url` (lowercase, default port dropped), or None if it is not http(s)."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if scheme not in ("http", "https") or not host:
        return None
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def tenant_allowed_origin(tenant: Tenant) -> Optional[str]:
    """The ONLY origin allowed to post captures for `tenant`; None (= everything denied) when it cannot be derived."""
    origin = _origin_of(tenant.storefront_url)
    if origin:
        return origin
    if tenant.platform == "shopify":
        return _origin_of(f"https://{tenant.shop_domain}")
    return None


def _request_origin(request: Request) -> Optional[str]:
    header = request.headers.get("origin")
    if not header or header.strip().lower() == "null":
        return None
    return _origin_of(header)


def _connection_ip(request: Request) -> Optional[str]:
    """Peer address (or, with MOTAHAI_TRUSTED_PROXY=1, the first X-Forwarded-For hop) as a valid PUBLIC IP, else None."""
    host = request.client.host if request.client else None
    if os.environ.get(TRUSTED_PROXY_ENV) == "1":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            host = forwarded.split(",")[0].strip()
    ip = sanitize_client_ip(host)
    if ip is None:
        return None
    try:
        return ip if ipaddress.ip_address(ip).is_global else None
    except ValueError:
        return None


def _limiter_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    if os.environ.get(TRUSTED_PROXY_ENV) == "1":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            host = forwarded.split(",")[0].strip() or host
    return host


async def _read_limited_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_CAPTURE_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_CAPTURE_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _cors_headers(origin: str) -> Dict[str, str]:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}


def _load_tenant_and_origin(factory: sessionmaker, tenant_key: str, request: Request):
    """(tenant_id, allowed_origin). 404 unknown/inactive tenant, 403 wrong or missing Origin."""
    session = factory()
    try:
        tenant = get_tenant_by_shop_domain(session, tenant_key)
        if tenant is None or not tenant.active:
            raise HTTPException(status_code=404, detail="Unknown shop")
        allowed = tenant_allowed_origin(tenant)
        origin = _request_origin(request)
        if allowed is None or origin is None or origin != allowed:
            raise HTTPException(status_code=403, detail="Origin not allowed")
        return tenant.id, allowed
    finally:
        session.close()


# --- routes --------------------------------------------------------------------------------------------------

@router.options("/v1/capture/{tenant_key}")
async def capture_preflight(tenant_key: str, request: Request,
                            factory: sessionmaker = Depends(get_session_factory_dep)):
    _tenant_id, origin = _load_tenant_and_origin(factory, tenant_key, request)
    headers = _cors_headers(origin)
    headers.update({"Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"})
    return Response(status_code=204, headers=headers)


@router.post("/v1/capture/{tenant_key}")
async def storefront_capture(tenant_key: str, request: Request,
                             factory: sessionmaker = Depends(get_session_factory_dep)):
    if not ip_limiter.allow(_limiter_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "60"})

    tenant_id, origin = _load_tenant_and_origin(factory, tenant_key, request)

    if not tenant_limiter.allow(f"t:{tenant_id}"):
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "60"})

    content_type = (request.headers.get("content-type") or "text/plain").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported content type")

    raw = await _read_limited_body(request)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        body = CaptureBody.model_validate(data)
    except (ValueError, UnicodeDecodeError, ValidationError):
        raise HTTPException(status_code=422, detail="Invalid capture payload") from None

    attribution = sanitize_attribution(body.attribution if isinstance(body.attribution, dict) else {})
    client_ip = _connection_ip(request)
    user_agent = sanitize_user_agent(request.headers.get("user-agent"))

    ip_ct = ua_ct = None
    try:
        ip_ct = encrypt_value(client_ip) if client_ip else None
        ua_ct = encrypt_value(user_agent) if user_agent else None
    except CredentialError as exc:  # no/invalid Fernet key: keep the attribution, skip IP/UA (fail closed on secrets)
        logger.warning("Capture IP/UA not stored: %s", type(exc).__name__)

    now = utcnow()
    session = factory()
    try:
        session.add(PendingCapture(
            tenant_id=tenant_id, platform_order_id=body.order_id, order_total=body.order_total,
            currency=body.currency, ip_ciphertext=ip_ct, ua_ciphertext=ua_ct,
            received_at=now, expires_at=now + CAPTURE_TTL, **attribution))
        session.commit()
    finally:
        session.close()

    return Response(status_code=204, headers=_cors_headers(origin))
