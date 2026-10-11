"""
storefront_probe.py - Deterministic headless storefront probe for Gate 2 & S3-6 Drift Sentinel (G3 / S3-10).

Loads a merchant's public storefront home page and one product page in a headless browser (or evaluates a
recorded fixture via an injected runner), captures network request URLs only (never bodies or cookies), strips
all query parameters except the allowlisted tracking/consent keys (`id`, `tid`, `gcs`, `gcd`), and reports:
  * gtm_ids_seen, ga4_ids_seen, meta_pixel_ids_seen, other_pixels (tiktok, snap)
  * expected: {gtm_loaded, ga4_seen, pixel_seen} (true/false when status=="OK" and requested, else null)
  * duplicates: {gtm, ga4, meta} (IDs loaded more than once on a single page or competing distinct IDs)
  * consent_signals_seen (true/false when status=="OK", null when status=="UNKNOWN")
  * hit_before_consent (true/false when a consent-update transition is detectable in the request log, else null)

Any load failure, timeout, TLS error, bot challenge or SSRF-blocked host returns `status: "UNKNOWN"` with a
reason code (`FETCH_FAILED`, `TIMEOUT`, `BOT_CHALLENGE`, `TLS_ERROR`, `BLOCKED_HOST`) and never reports `false`
for unobserved signals.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

PAGE_TIMEOUT_MS = 20_000
ALLOWED_QUERY_KEYS = frozenset({"id", "tid", "gcs", "gcd"})

# Vendor / platform domains that must never be probed as a merchant storefront
DENIED_HOST_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "google.com",
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.com",
    "facebook.net",
    "fbcdn.net",
    "tiktok.com",
    "snapchat.com",
)

_GTM_ID_RE = re.compile(r"^GTM-[A-Z0-9]{4,10}$")
_GA4_ID_RE = re.compile(r"^G-[A-Z0-9]{4,16}$")
_PIXEL_ID_RE = re.compile(r"^[0-9]{10,20}$")

_BOT_MARKERS = (
    "cf-chl",
    "just a moment...",
    "attention required! | cloudflare",
    "challenge-platform",
    "verify you are human",
    "captcha",
)


class ProbeFailure(Exception):
    """Raised when a page load or SSRF check fails with a classified reason code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass
class PageCapture:
    url: str
    http_status: Optional[int]
    requests: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    title: str = ""


ResolverFn = Callable[[str, int], Sequence[Tuple[Any, ...]]]
PageRunnerFn = Callable[[str, int], PageCapture]


def _default_resolver(host: str, port: int) -> Sequence[Tuple[Any, ...]]:
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


def sanitize_url(url: str) -> str:
    """Strips userinfo, fragments, and all query parameters except `id`, `tid`, `gcs`, `gcd`."""
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    host = parts.hostname.lower().rstrip(".")
    netloc = f"{host}:{parts.port}" if parts.port and parts.port not in (80, 443) else host
    kept_params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k in ALLOWED_QUERY_KEYS and v
    ]
    clean_query = urlencode(kept_params)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, clean_query, ""))


def validate_and_resolve_store_url(
    url: str,
    resolver: Optional[ResolverFn] = None,
) -> str:
    """Validates `url` is a public https store URL and resolves its DNS against SSRF ranges.

    Returns the normalized URL on success, or raises ProbeFailure("BLOCKED_HOST", ...).
    """
    if not isinstance(url, str) or not url.strip():
        raise ProbeFailure("BLOCKED_HOST", "store_url must be a non-empty https URL.")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ProbeFailure("BLOCKED_HOST", "Malformed URL.") from exc

    if parts.scheme.lower() != "https":
        raise ProbeFailure("BLOCKED_HOST", "Only https store URLs are permitted.")
    if parts.username or parts.password:
        raise ProbeFailure("BLOCKED_HOST", "Credentials in URL are not permitted.")
    if parts.port not in (None, 443):
        raise ProbeFailure("BLOCKED_HOST", "Non-standard ports are not permitted.")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        raise ProbeFailure("BLOCKED_HOST", "Host must be a public domain name.")

    # Refuse IP literals directly
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise ProbeFailure("BLOCKED_HOST", "IP literals are not permitted.")
    except ValueError:
        pass

    for suffix in DENIED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            raise ProbeFailure("BLOCKED_HOST", f"Host '{host}' is on the denied host list.")

    res_fn = resolver or _default_resolver
    try:
        infos = res_fn(host, 443)
    except OSError as exc:
        raise ProbeFailure("FETCH_FAILED", f"DNS resolution failed for {host}.") from exc

    if not infos:
        raise ProbeFailure("FETCH_FAILED", f"No DNS records found for {host}.")

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise ProbeFailure("BLOCKED_HOST", "Invalid IP address in DNS response.") from exc
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ProbeFailure("BLOCKED_HOST", f"Host '{host}' resolved to non-public IP.")

    return sanitize_url(url)


def find_product_url(home_url: str, links: Iterable[str]) -> Optional[str]:
    """Selects the first product page link on the same store domain.

    Priority 1: Shopify `/products/<handle>` path (including locale prefixes like `/ar/products/...`).
    Priority 2: Salla / Zid / WooCommerce product paths (`/product/...`, `/p/...`, `/p<digits>`).
    """
    try:
        home_parts = urlsplit(home_url)
    except ValueError:
        return None
    home_host = (home_parts.hostname or "").lower().rstrip(".")
    if not home_host:
        return None

    shopify_candidates: List[str] = []
    other_candidates: List[str] = []

    for raw in links or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        joined = urljoin(home_url, raw.strip())
        try:
            p = urlsplit(joined)
        except ValueError:
            continue
        if p.scheme.lower() != "https":
            continue
        host = (p.hostname or "").lower().rstrip(".")
        if host != home_host:
            continue
        path = p.path or "/"
        # Skip collection-only or cart/account URLs
        if any(seg in path.lower() for seg in ("/cart", "/account", "/login", "/checkout", "/search")):
            continue
        clean = sanitize_url(joined)
        if not clean or clean == sanitize_url(home_url):
            continue
        if re.search(r"(?:^|/)(?:products)/[^/?#]+", path, re.IGNORECASE):
            shopify_candidates.append(clean)
        elif re.search(r"(?:^|/)(?:product/[^/?#]+|p/[^/?#]+|p[0-9]{4,})", path, re.IGNORECASE):
            other_candidates.append(clean)

    if shopify_candidates:
        return shopify_candidates[0]
    if other_candidates:
        return other_candidates[0]
    return None


def _unknown_result(reason: str, pages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "reason": reason,
        "pages": pages if pages is not None else [],
        "gtm_ids_seen": [],
        "ga4_ids_seen": [],
        "meta_pixel_ids_seen": [],
        "other_pixels": [],
        "expected": {
            "gtm_loaded": None,
            "ga4_seen": None,
            "pixel_seen": None,
        },
        "duplicates": {
            "gtm": [],
            "ga4": [],
            "meta": [],
        },
        "consent_signals_seen": None,
        "hit_before_consent": None,
    }


def _is_bot_challenge(capture: PageCapture) -> bool:
    if capture.http_status in (403, 429, 503):
        return True
    title_low = (capture.title or "").lower()
    if any(m in title_low for m in _BOT_MARKERS):
        return True
    for r in capture.requests:
        r_low = r.lower()
        if "/cdn-cgi/challenge-platform" in r_low or "challenges.cloudflare.com" in r_low:
            return True
    return False


def _extract_from_page_requests(requests: Sequence[str]) -> Tuple[
    List[str],  # gtm_ids_in_order
    List[str],  # ga4_tag_loads_in_order (from gtag/js?id=G-...)
    List[str],  # ga4_all_ids_in_order (from gtag/js or /g/collect?tid=G-...)
    List[str],  # meta_ids_in_order
    Set[str],   # other_pixels
    bool,       # consent_seen
    Optional[bool],  # hit_before_consent
]:
    gtm_loads: List[str] = []
    ga4_script_loads: List[str] = []
    ga4_all: List[str] = []
    meta_all: List[str] = []
    other: Set[str] = set()
    consent_seen = False

    # Track ordering for consent-update detection:
    # Each entry is ("google_hit", gcs_val) or ("ad_hit", None)
    timeline: List[Tuple[str, Optional[str]]] = []

    for raw_url in requests:
        if not isinstance(raw_url, str) or not raw_url:
            continue
        try:
            parts = urlsplit(raw_url)
        except ValueError:
            continue
        host = (parts.hostname or "").lower().rstrip(".")
        path = (parts.path or "").lower()
        params = dict(parse_qsl(parts.query, keep_blank_values=False))

        is_google_hit = False
        gcs_val = params.get("gcs", "").strip()
        gcd_val = params.get("gcd", "").strip()

        # 1. GTM container load: googletagmanager.com/gtm.js?id=GTM-...
        if (host == "googletagmanager.com" or host.endswith(".googletagmanager.com")) and path.endswith("/gtm.js"):
            raw_id = params.get("id", "").strip().upper()
            if _GTM_ID_RE.fullmatch(raw_id):
                gtm_loads.append(raw_id)

        # 2. Google tag script load: googletagmanager.com/gtag/js?id=G-...
        if (host == "googletagmanager.com" or host.endswith(".googletagmanager.com")) and path.endswith("/gtag/js"):
            raw_id = params.get("id", "").strip().upper()
            if _GA4_ID_RE.fullmatch(raw_id):
                ga4_script_loads.append(raw_id)
                ga4_all.append(raw_id)
            is_google_hit = True

        # 3. GA4 collect beacon: */g/collect?...&tid=G-...
        if (
            host == "google-analytics.com"
            or host.endswith(".google-analytics.com")
            or host == "analytics.google.com"
            or host.endswith(".analytics.google.com")
        ) and "/g/collect" in path:
            tid = params.get("tid", "").strip().upper()
            if _GA4_ID_RE.fullmatch(tid):
                ga4_all.append(tid)
            is_google_hit = True

        if is_google_hit:
            if gcs_val or gcd_val:
                consent_seen = True
            timeline.append(("google_hit", gcs_val or None))

        # 4. Meta Pixel: facebook.com/tr?id=<digits>
        if (host == "facebook.com" or host.endswith(".facebook.com")) and (path == "/tr" or path.startswith("/tr/")):
            pid = params.get("id", "").strip()
            if _PIXEL_ID_RE.fullmatch(pid):
                meta_all.append(pid)
                timeline.append(("ad_hit", None))

        # 5. TikTok & Snap pixels
        if host == "analytics.tiktok.com" or host.endswith(".analytics.tiktok.com"):
            other.add("tiktok")
            timeline.append(("ad_hit", None))
        if host == "tr.snapchat.com" or host.endswith(".tr.snapchat.com") or host == "sc-static.net":
            if "snapchat.com" in host:
                other.add("snap")
                timeline.append(("ad_hit", None))

    # Detect whether an ad/analytics hit fired before a consent-update transition, if detectable
    # A transition is detectable when we observe an initial default-denied Google hit (gcs=="G100")
    # followed later by a granted Google hit (e.g. gcs in {"G101", "G110", "G111"}).
    hit_before_consent: Optional[bool] = None
    first_denied_idx: Optional[int] = None
    first_granted_idx: Optional[int] = None
    for idx, (kind, gcs) in enumerate(timeline):
        if kind == "google_hit" and gcs:
            if gcs == "G100" and first_denied_idx is None:
                first_denied_idx = idx
            elif gcs != "G100" and gcs.startswith("G1") and first_granted_idx is None:
                first_granted_idx = idx
    if first_denied_idx is not None and first_granted_idx is not None and first_granted_idx > first_denied_idx:
        # Check if any ad_hit fired before first_granted_idx
        early_ad = any(kind == "ad_hit" for kind, _ in timeline[:first_granted_idx])
        hit_before_consent = early_ad

    return gtm_loads, ga4_script_loads, ga4_all, meta_all, other, consent_seen, hit_before_consent


def _unique_in_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def analyze_captures(
    captures: Sequence[PageCapture],
    gtm_public_id: Optional[str] = None,
    ga4_measurement_id: Optional[str] = None,
    meta_pixel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure analyzer that produces the storefront probe JSON dict from one or more PageCaptures."""
    if not captures:
        return _unknown_result("FETCH_FAILED")

    pages_out: List[Dict[str, Any]] = []
    for cap in captures:
        pages_out.append(
            {
                "url": sanitize_url(cap.url),
                "http_status": cap.http_status,
            }
        )

    first = captures[0]
    if _is_bot_challenge(first):
        return _unknown_result("BOT_CHALLENGE", pages=pages_out)
    if first.http_status is None or first.http_status >= 400:
        return _unknown_result("FETCH_FAILED", pages=pages_out)

    all_gtm: List[str] = []
    all_ga4: List[str] = []
    all_meta: List[str] = []
    all_other: Set[str] = set()
    any_consent_seen = False
    combined_hit_before_consent: Optional[bool] = None

    dup_gtm: List[str] = []
    dup_ga4: List[str] = []
    dup_meta: List[str] = []

    for cap in captures:
        if cap.http_status is None or cap.http_status >= 400 or _is_bot_challenge(cap):
            continue
        gtm_loads, ga4_loads, ga4_ids, meta_ids, other, consent_seen, hit_before = _extract_from_page_requests(
            cap.requests
        )
        all_gtm.extend(gtm_loads)
        all_ga4.extend(ga4_ids)
        all_meta.extend(meta_ids)
        all_other |= other
        if consent_seen:
            any_consent_seen = True
        if hit_before is not None:
            combined_hit_before_consent = bool(combined_hit_before_consent or hit_before)

        # Same ID loaded more than once on the SAME page (e.g. duplicate snippet + app embed)
        for gid in _unique_in_order(gtm_loads):
            if gtm_loads.count(gid) > 1 and gid not in dup_gtm:
                dup_gtm.append(gid)
        for aid in _unique_in_order(ga4_loads):
            if ga4_loads.count(aid) > 1 and aid not in dup_ga4:
                dup_ga4.append(aid)
        for pid in _unique_in_order(meta_ids):
            if meta_ids.count(pid) > 1 and pid not in dup_meta:
                dup_meta.append(pid)

    gtm_ids_seen = _unique_in_order(all_gtm)
    ga4_ids_seen = _unique_in_order(all_ga4)
    meta_pixel_ids_seen = _unique_in_order(all_meta)

    # Distinct competing containers/tags on the same storefront are also flagged in duplicates
    if len(gtm_ids_seen) > 1:
        for gid in gtm_ids_seen:
            if gid not in dup_gtm:
                dup_gtm.append(gid)
    if len(ga4_ids_seen) > 1:
        for aid in ga4_ids_seen:
            if aid not in dup_ga4:
                dup_ga4.append(aid)
    if len(meta_pixel_ids_seen) > 1:
        for pid in meta_pixel_ids_seen:
            if pid not in dup_meta:
                dup_meta.append(pid)

    gtm_target = gtm_public_id.strip().upper() if isinstance(gtm_public_id, str) and gtm_public_id.strip() else None
    ga4_target = (
        ga4_measurement_id.strip().upper()
        if isinstance(ga4_measurement_id, str) and ga4_measurement_id.strip()
        else None
    )
    pixel_target = meta_pixel_id.strip() if isinstance(meta_pixel_id, str) and meta_pixel_id.strip() else None

    expected = {
        "gtm_loaded": (gtm_target in gtm_ids_seen) if gtm_target else None,
        "ga4_seen": (ga4_target in ga4_ids_seen) if ga4_target else None,
        "pixel_seen": (pixel_target in meta_pixel_ids_seen) if pixel_target else None,
    }

    return {
        "status": "OK",
        "pages": pages_out,
        "gtm_ids_seen": gtm_ids_seen,
        "ga4_ids_seen": ga4_ids_seen,
        "meta_pixel_ids_seen": meta_pixel_ids_seen,
        "other_pixels": sorted(all_other),
        "expected": expected,
        "duplicates": {
            "gtm": dup_gtm,
            "ga4": dup_ga4,
            "meta": dup_meta,
        },
        "consent_signals_seen": any_consent_seen,
        "hit_before_consent": combined_hit_before_consent,
    }


def _classify_exception(exc: Exception) -> str:
    if isinstance(exc, ProbeFailure):
        return exc.reason
    msg = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, ssl.SSLError) or "ssl" in msg or "cert" in msg or "tls" in msg:
        return "TLS_ERROR"
    if isinstance(exc, TimeoutError) or "timeout" in msg or "timed out" in msg:
        return "TIMEOUT"
    return "FETCH_FAILED"


def _playwright_runner(url: str, timeout_ms: int = PAGE_TIMEOUT_MS) -> PageCapture:
    """Thin headless Chromium runner using Playwright with TLS verification enabled and no cookie persistence."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ProbeFailure("FETCH_FAILED", "Playwright is not installed on this host.") from exc

    requests_seen: List[str] = []
    links: List[str] = []
    status_code: Optional[int] = None
    final_url = url
    title = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            # ignore_https_errors=False keeps TLS verification strictly ON
            context = browser.new_context(ignore_https_errors=False)
            page = context.new_page()
            page.on("request", lambda req: requests_seen.append(sanitize_url(req.url)))
            resp = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if resp is not None:
                status_code = resp.status
            final_url = page.url or url
            try:
                title = page.title() or ""
            except Exception:  # noqa: BLE001
                title = ""
            try:
                raw_links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
                if isinstance(raw_links, list):
                    links = [str(x) for x in raw_links if x]
            except Exception:  # noqa: BLE001
                links = []
            context.close()
        finally:
            browser.close()

    return PageCapture(
        url=final_url,
        http_status=status_code,
        requests=[r for r in requests_seen if r],
        links=links,
        title=title,
    )


def probe_storefront(
    store_url: str,
    gtm_public_id: Optional[str] = None,
    ga4_measurement_id: Optional[str] = None,
    meta_pixel_id: Optional[str] = None,
    runner: Optional[PageRunnerFn] = None,
    resolver: Optional[ResolverFn] = None,
) -> Dict[str, Any]:
    """Entry point for `hermes_storefront_probe`."""
    try:
        clean_home = validate_and_resolve_store_url(store_url, resolver=resolver)
    except Exception as exc:  # noqa: BLE001
        return _unknown_result(_classify_exception(exc))

    run_page = runner or _playwright_runner
    captures: List[PageCapture] = []

    try:
        home_cap = run_page(clean_home, PAGE_TIMEOUT_MS)
        # Re-validate final URL after any redirects (SSRF redirect guard)
        validate_and_resolve_store_url(home_cap.url, resolver=resolver)
        home_cap.url = sanitize_url(home_cap.url)
        home_cap.requests = [sanitize_url(r) for r in home_cap.requests if sanitize_url(r)]
        captures.append(home_cap)
    except Exception as exc:  # noqa: BLE001
        return _unknown_result(_classify_exception(exc))

    # If home page succeeded and is not a bot challenge, also probe the first product page if found
    if home_cap.http_status and 200 <= home_cap.http_status < 400 and not _is_bot_challenge(home_cap):
        prod_url = find_product_url(home_cap.url, home_cap.links)
        if prod_url:
            try:
                valid_prod = validate_and_resolve_store_url(prod_url, resolver=resolver)
                prod_cap = run_page(valid_prod, PAGE_TIMEOUT_MS)
                validate_and_resolve_store_url(prod_cap.url, resolver=resolver)
                prod_cap.url = sanitize_url(prod_cap.url)
                prod_cap.requests = [sanitize_url(r) for r in prod_cap.requests if sanitize_url(r)]
                captures.append(prod_cap)
            except ProbeFailure as exc:
                if exc.reason == "BLOCKED_HOST":
                    return _unknown_result("BLOCKED_HOST", pages=[{"url": home_cap.url, "http_status": home_cap.http_status}])
            except Exception:  # noqa: BLE001
                # Home page loaded fine; product page load failure does not invalidate home page observations
                pass

    return analyze_captures(
        captures,
        gtm_public_id=gtm_public_id,
        ga4_measurement_id=ga4_measurement_id,
        meta_pixel_id=meta_pixel_id,
    )
