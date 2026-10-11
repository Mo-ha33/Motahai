"""Unit tests for ops/hermes/storefront_probe.py and consultation.py registration (G3 / S3-10)."""

from __future__ import annotations

import importlib.util
import json
import socket
import ssl
import sys
import types
from pathlib import Path
from typing import Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = ROOT / "ops" / "hermes" / "storefront_probe.py"
CONSULTATION_PATH = ROOT / "ops" / "hermes" / "consultation.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("hermes_storefront_probe_test", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()
PageCapture = probe.PageCapture


def public_resolver(host: str, port: int):
    """Fake DNS resolver returning a public IP (23.227.38.65 = Shopify public range)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("23.227.38.65", port))]


def make_runner(pages_by_url: Dict[str, PageCapture]):
    def _runner(url: str, timeout_ms: int) -> PageCapture:
        if url not in pages_by_url:
            raise AssertionError(f"Unexpected URL requested by probe: {url}")
        return pages_by_url[url]

    return _runner


def test_shopify_theme_snippet_fixture():
    home_url = "https://epxsmk-y0.myshopify.com/ar"
    prod_url = "https://epxsmk-y0.myshopify.com/ar/products/arabic-coffee"
    pages = {
        home_url: PageCapture(
            url=home_url + "?utm_source=ig&fbclid=IwAR123secret&email=buyer@example.com",
            http_status=200,
            requests=[
                "https://epxsmk-y0.myshopify.com/ar?email=buyer@example.com",
                "https://www.googletagmanager.com/gtm.js?id=GTM-T443M78T&l=dataLayer",
                "https://www.googletagmanager.com/gtag/js?id=G-ABC123XYZ9&cx=c",
                "https://region1.google-analytics.com/g/collect?v=2&tid=G-ABC123XYZ9&gcs=G111&gcd=13l3l3l2l1&dl=https%3A%2F%2Fepxsmk-y0.myshopify.com",
                "https://www.facebook.com/tr?id=123456789012345&ev=PageView&ud[em]=raw_pii_should_be_stripped",
            ],
            links=["/cart", "/ar/products/arabic-coffee?variant=999888"],
        ),
        prod_url: PageCapture(
            url=prod_url,
            http_status=200,
            requests=[
                "https://www.googletagmanager.com/gtm.js?id=GTM-T443M78T",
                "https://region1.google-analytics.com/g/collect?v=2&tid=G-ABC123XYZ9&gcs=G111&en=view_item",
                "https://www.facebook.com/tr?id=123456789012345&ev=ViewContent",
            ],
            links=[],
        ),
    }

    res = probe.probe_storefront(
        store_url=home_url,
        gtm_public_id="GTM-T443M78T",
        ga4_measurement_id="G-ABC123XYZ9",
        meta_pixel_id="123456789012345",
        runner=make_runner(pages),
        resolver=public_resolver,
    )

    assert res["status"] == "OK"
    assert res["pages"] == [
        {"url": home_url, "http_status": 200},
        {"url": prod_url, "http_status": 200},
    ]
    assert res["gtm_ids_seen"] == ["GTM-T443M78T"]
    assert res["ga4_ids_seen"] == ["G-ABC123XYZ9"]
    assert res["meta_pixel_ids_seen"] == ["123456789012345"]
    assert res["other_pixels"] == []
    assert res["expected"] == {"gtm_loaded": True, "ga4_seen": True, "pixel_seen": True}
    # Normal load once on home + once on product page is NOT a duplicate
    assert res["duplicates"] == {"gtm": [], "ga4": [], "meta": []}
    assert res["consent_signals_seen"] is True
    assert "buyer@example.com" not in json.dumps(res)


def test_shopify_app_embed_custom_pixel_fixture():
    """GTM loaded inside a Shopify web-pixel sandboxed worker plus TikTok & Snap pixels."""
    home_url = "https://modern-store.myshopify.com/"
    pages = {
        home_url: PageCapture(
            url=home_url,
            http_status=200,
            requests=[
                "https://modern-store.myshopify.com/wpm@12345/custom/web-pixel-shopify-custom-pixel@0550/sandbox/modern/",
                "https://www.googletagmanager.com/gtm.js?id=GTM-5C5N552P",
                "https://analytics.tiktok.com/api/v2/pixel",
                "https://tr.snapchat.com/v2/p?pid=snap-1",
            ],
            links=[],
        ),
    }

    res = probe.probe_storefront(
        store_url=home_url,
        gtm_public_id="GTM-5C5N552P",
        runner=make_runner(pages),
        resolver=public_resolver,
    )
    assert res["status"] == "OK"
    assert res["gtm_ids_seen"] == ["GTM-5C5N552P"]
    assert res["other_pixels"] == ["snap", "tiktok"]
    assert res["expected"] == {"gtm_loaded": True, "ga4_seen": None, "pixel_seen": None}
    assert res["consent_signals_seen"] is False
    assert res["hit_before_consent"] is None


def test_salla_native_integration_fixture():
    home_url = "https://salla-merchant.com/"
    prod_url = "https://salla-merchant.com/p198273645"
    pages = {
        home_url: PageCapture(
            url=home_url,
            http_status=200,
            requests=[
                "https://www.googletagmanager.com/gtm.js?id=GTM-SALLA01",
                "https://www.google-analytics.com/g/collect?v=2&tid=G-SALLA9999&gcd=13r3r3r2r5",
            ],
            links=["/login", "/p198273645?ref=home"],
        ),
        prod_url: PageCapture(
            url=prod_url,
            http_status=200,
            requests=[
                "https://www.googletagmanager.com/gtm.js?id=GTM-SALLA01",
                "https://www.facebook.com/tr?id=987654321098765&ev=ViewContent",
            ],
            links=[],
        ),
    }

    res = probe.probe_storefront(
        store_url=home_url,
        gtm_public_id="GTM-SALLA01",
        ga4_measurement_id="G-SALLA9999",
        meta_pixel_id="987654321098765",
        runner=make_runner(pages),
        resolver=public_resolver,
    )
    assert res["status"] == "OK"
    assert len(res["pages"]) == 2 and res["pages"][1]["url"] == prod_url
    assert res["expected"] == {"gtm_loaded": True, "ga4_seen": True, "pixel_seen": True}
    assert res["consent_signals_seen"] is True


def test_duplicate_gtm_and_competing_containers_fixture():
    home_url = "https://dup-store.myshopify.com/"
    pages = {
        home_url: PageCapture(
            url=home_url,
            http_status=200,
            requests=[
                # GTM-AAA1111 loaded twice on the same page, plus a competing GTM-BBB2222 container
                "https://www.googletagmanager.com/gtm.js?id=GTM-AAA1111",
                "https://www.googletagmanager.com/gtm.js?id=GTM-AAA1111",
                "https://www.googletagmanager.com/gtm.js?id=GTM-BBB2222",
                "https://www.googletagmanager.com/gtag/js?id=G-11111111",
                "https://www.googletagmanager.com/gtag/js?id=G-22222222",
                "https://www.facebook.com/tr?id=111111111111111&ev=PageView",
                "https://www.facebook.com/tr?id=222222222222222&ev=PageView",
            ],
            links=[],
        ),
    }

    res = probe.probe_storefront(
        store_url=home_url,
        gtm_public_id="GTM-AAA1111",
        runner=make_runner(pages),
        resolver=public_resolver,
    )
    assert res["status"] == "OK"
    assert res["duplicates"]["gtm"] == ["GTM-AAA1111", "GTM-BBB2222"]
    assert res["duplicates"]["ga4"] == ["G-11111111", "G-22222222"]
    assert res["duplicates"]["meta"] == ["111111111111111", "222222222222222"]


def test_no_gtm_installed_fixture():
    home_url = "https://epxsmk-y0.myshopify.com/ar"
    pages = {
        home_url: PageCapture(
            url=home_url,
            http_status=200,
            requests=[
                "https://epxsmk-y0.myshopify.com/ar",
                "https://www.facebook.com/tr?id=123456789012345&ev=PageView",
            ],
            links=[],
        ),
    }

    res = probe.probe_storefront(
        store_url=home_url,
        gtm_public_id="GTM-T443M78T",
        ga4_measurement_id="G-ABC123XYZ9",
        meta_pixel_id="123456789012345",
        runner=make_runner(pages),
        resolver=public_resolver,
    )
    assert res["status"] == "OK"
    assert res["gtm_ids_seen"] == []
    assert res["ga4_ids_seen"] == []
    assert res["meta_pixel_ids_seen"] == ["123456789012345"]
    assert res["expected"] == {"gtm_loaded": False, "ga4_seen": False, "pixel_seen": True}
    assert res["consent_signals_seen"] is False


@pytest.mark.parametrize(
    "scenario,expected_reason",
    [
        ("http_500", "FETCH_FAILED"),
        ("network_exc", "FETCH_FAILED"),
        ("timeout", "TIMEOUT"),
        ("tls", "TLS_ERROR"),
        ("bot_403", "BOT_CHALLENGE"),
        ("bot_title", "BOT_CHALLENGE"),
    ],
)
def test_failures_return_unknown_and_never_report_false(scenario, expected_reason):
    home_url = "https://epxsmk-y0.myshopify.com/ar"

    def failing_runner(url: str, timeout_ms: int) -> PageCapture:
        if scenario == "http_500":
            return PageCapture(url=url, http_status=500)
        if scenario == "network_exc":
            raise OSError("net::ERR_NAME_NOT_RESOLVED")
        if scenario == "timeout":
            raise TimeoutError("Page.goto: Timeout 20000ms exceeded")
        if scenario == "tls":
            raise ssl.SSLError("SSL: CERTIFICATE_VERIFY_FAILED")
        if scenario == "bot_403":
            return PageCapture(url=url, http_status=403, title="Access Denied")
        return PageCapture(url=url, http_status=200, title="Just a moment... Cloudflare")

    res = probe.probe_storefront(
        store_url=home_url,
        gtm_public_id="GTM-T443M78T",
        ga4_measurement_id="G-ABC123XYZ9",
        meta_pixel_id="123456789012345",
        runner=failing_runner,
        resolver=public_resolver,
    )
    assert res["status"] == "UNKNOWN"
    assert res["reason"] == expected_reason
    # Critical invariant: never report False when the page did not load
    assert res["expected"] == {"gtm_loaded": None, "ga4_seen": None, "pixel_seen": None}
    assert res["consent_signals_seen"] is None
    assert res["hit_before_consent"] is None


@pytest.mark.parametrize(
    "bad_url,private_ip",
    [
        ("http://epxsmk-y0.myshopify.com/", None),
        ("https://localhost/", None),
        ("https://127.0.0.1/", None),
        ("https://169.254.169.254/latest/meta-data", None),
        ("https://[::1]/", None),
        ("https://www.google.com/", None),
        ("https://www.googletagmanager.com/gtm.js?id=GTM-1234", None),
        ("https://www.facebook.com/tr", None),
        ("https://analytics.tiktok.com/", None),
        ("https://user:pass@epxsmk-y0.myshopify.com/", None),
        ("https://epxsmk-y0.myshopify.com:8443/", None),
        ("https://internal-shop.example.com/", "10.0.0.12"),
        ("https://internal-shop.example.com/", "192.168.1.5"),
        ("https://internal-shop.example.com/", "127.0.0.1"),
    ],
)
def test_ssrf_guard_blocks_bad_hosts_and_private_ips(bad_url, private_ip):
    def custom_resolver(host: str, port: int):
        ip = private_ip or "23.227.38.65"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    called = []
    res = probe.probe_storefront(
        store_url=bad_url,
        gtm_public_id="GTM-T443M78T",
        runner=lambda u, t: called.append(u) or PageCapture(url=u, http_status=200),
        resolver=custom_resolver,
    )
    assert res["status"] == "UNKNOWN"
    assert res["reason"] == "BLOCKED_HOST"
    assert called == []


def test_ssrf_guard_rechecks_after_redirect():
    home_url = "https://epxsmk-y0.myshopify.com/"
    # Simulate an open redirect from a public store domain to a blocked host
    res = probe.probe_storefront(
        store_url=home_url,
        runner=lambda u, t: PageCapture(url="https://www.google.com/", http_status=200),
        resolver=public_resolver,
    )
    assert res["status"] == "UNKNOWN"
    assert res["reason"] == "BLOCKED_HOST"


def test_query_string_stripping_removes_pii_and_keeps_allowlist():
    raw = (
        "https://region1.google-analytics.com/g/collect?"
        "v=2&tid=G-ABC123XYZ9&id=GTM-T443M78T&gcs=G100&gcd=13l3l3l2l1"
        "&email=secret@example.com&phone=%2B966500000000&fbclid=IwAR999&dl=https%3A%2F%2Fshop.com#frag"
    )
    cleaned = probe.sanitize_url(raw)
    assert cleaned == (
        "https://region1.google-analytics.com/g/collect?"
        "tid=G-ABC123XYZ9&id=GTM-T443M78T&gcs=G100&gcd=13l3l3l2l1"
    )
    assert "secret@example.com" not in cleaned
    assert "966500000000" not in cleaned
    assert "fbclid" not in cleaned
    assert "frag" not in cleaned


def test_consent_ordering_detection():
    home_url = "https://epxsmk-y0.myshopify.com/"
    # Case 1: Meta Pixel fires BEFORE consent switches from G100 to G111 -> hit_before_consent is True
    early_pages = {
        home_url: PageCapture(
            url=home_url,
            http_status=200,
            requests=[
                "https://region1.google-analytics.com/g/collect?tid=G-ABC123XYZ9&gcs=G100",
                "https://www.facebook.com/tr?id=123456789012345&ev=PageView",
                "https://region1.google-analytics.com/g/collect?tid=G-ABC123XYZ9&gcs=G111",
            ],
        )
    }
    res1 = probe.probe_storefront(home_url, runner=make_runner(early_pages), resolver=public_resolver)
    assert res1["hit_before_consent"] is True

    # Case 2: Meta Pixel fires ONLY AFTER consent switches from G100 to G111 -> hit_before_consent is False
    clean_pages = {
        home_url: PageCapture(
            url=home_url,
            http_status=200,
            requests=[
                "https://region1.google-analytics.com/g/collect?tid=G-ABC123XYZ9&gcs=G100",
                "https://region1.google-analytics.com/g/collect?tid=G-ABC123XYZ9&gcs=G111",
                "https://www.facebook.com/tr?id=123456789012345&ev=PageView",
            ],
        )
    }
    res2 = probe.probe_storefront(home_url, runner=make_runner(clean_pages), resolver=public_resolver)
    assert res2["hit_before_consent"] is False


def test_consultation_registers_hermes_storefront_probe(monkeypatch):
    pkg = types.ModuleType("fakehermes_probe")
    pkg.__path__ = []
    sub = types.ModuleType("fakehermes_probe.tools")
    sub.__path__ = []
    executor = types.ModuleType("fakehermes_probe.executor")
    executor.VPSExecutor = object
    for name, mod in (
        ("fakehermes_probe", pkg),
        ("fakehermes_probe.tools", sub),
        ("fakehermes_probe.executor", executor),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("fakehermes_probe.tools.consultation", CONSULTATION_PATH)
    consultation = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, consultation)
    spec.loader.exec_module(consultation)

    tools = {}

    class DummyServer:
        executor = object()

        def tool(self, name, description, input_schema):
            def dec(fn):
                tools[name] = fn
                return fn

            return dec

    consultation.register_consultation_tools(DummyServer())
    assert "hermes_storefront_probe" in tools
    out = json.loads(tools["hermes_storefront_probe"]("http://not-https.example.com"))
    assert out["status"] == "UNKNOWN" and out["reason"] == "BLOCKED_HOST"


# ---------------------------------------------------------------------------
# SSRF request guard (Playwright route handler), loop-safe worker thread, overall timeout
# ---------------------------------------------------------------------------
import asyncio
import threading
import time


class FakeFrame:
    def __init__(self, parent=None):
        self.parent_frame = parent


class FakeRequest:
    def __init__(self, url, navigation=False, main=True):
        self.url = url
        self._nav = navigation
        self.frame = FakeFrame(None if main else FakeFrame())

    def is_navigation_request(self):
        return self._nav


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.action = None
        self.abort_code = None

    def continue_(self):
        self.action = "continue"

    def abort(self, code="failed"):
        self.action = "abort"
        self.abort_code = code


def _guard(ip="93.184.216.34"):
    def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    return probe.RequestGuard(resolver)


def _run(guard, url, **kw):
    route = FakeRoute(FakeRequest(url, **kw))
    guard.handle(route)
    return route


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://127.0.0.1:8080/admin", "NON_GLOBAL_IP"),
        ("https://127.0.0.1/", "NON_GLOBAL_IP"),
        ("http://10.1.2.3/x", "NON_GLOBAL_IP"),
        ("http://172.16.0.9/", "NON_GLOBAL_IP"),
        ("http://192.168.1.1/", "NON_GLOBAL_IP"),
        ("http://169.254.169.254/latest/meta-data/", "NON_GLOBAL_IP"),
        ("http://[::1]:9000/", "NON_GLOBAL_IP"),
        ("http://[::ffff:127.0.0.1]/", "NON_GLOBAL_IP"),
        ("http://0.0.0.0/", "NON_GLOBAL_IP"),
        ("http://2130706433/", "NON_GLOBAL_IP"),
        ("http://0x7f.0.0.1/", "NON_GLOBAL_IP"),
        ("http://localhost:8000/", "DENIED_HOSTNAME"),
        ("http://foo.localhost/", "DENIED_HOSTNAME"),
        ("http://printer.local/", "DENIED_HOSTNAME"),
        ("http://metadata.google.internal/", "DENIED_HOSTNAME"),
        ("http://intranet/", "DENIED_HOSTNAME"),
        ("file:///etc/passwd", "NON_HTTP_SCHEME"),
        ("ftp://example.com/x", "NON_HTTP_SCHEME"),
        ("chrome://settings", "NON_HTTP_SCHEME"),
        ("ws://example.com/socket", "NON_HTTP_SCHEME"),
    ],
)
def test_guard_blocks_internal_targets(url, reason):
    guard = _guard()
    route = _run(guard, url)
    assert route.action == "abort"
    assert route.abort_code == "blockedbyclient"
    assert guard.blocked_reasons == {reason: 1}
    assert guard.blocked_count == 1
    assert guard.requests == []


def test_guard_allows_public_cdn_and_records_sanitized_url():
    guard = _guard()
    route = _run(guard, "https://cdn.shopify.com/s/files/app.js?email=a@b.com&id=GTM-ABCD12")
    assert route.action == "continue"
    assert guard.blocked_count == 0
    assert guard.requests == ["https://cdn.shopify.com/s/files/app.js?id=GTM-ABCD12"]
    # a public IP literal is fine
    assert _run(guard, "https://8.8.8.8/x").action == "continue"


def test_guard_blocks_document_navigation_resolving_to_private_ip():
    guard = _guard(ip="10.0.0.5")
    route = _run(guard, "https://rebind.attacker.example/", navigation=True)
    assert route.action == "abort"
    assert guard.blocked_reasons == {"RESOLVES_NON_GLOBAL": 1}
    assert guard.main_frame_blocked is True


def test_guard_blocks_subresource_host_resolving_to_loopback_without_main_frame_flag():
    guard = _guard(ip="127.0.0.1")
    route = _run(guard, "https://img.attacker.example/p.png")
    assert route.action == "abort"
    assert guard.blocked_reasons == {"RESOLVES_NON_GLOBAL": 1}
    assert guard.main_frame_blocked is False


def test_guard_iframe_navigation_blocked_is_not_main_frame():
    guard = _guard()
    route = _run(guard, "http://127.0.0.1/", navigation=True, main=False)
    assert route.action == "abort"
    assert guard.main_frame_blocked is False
    assert guard.blocked_count == 1


def test_guard_main_frame_redirect_hop_to_loopback_sets_flag():
    guard = _guard()
    assert _run(guard, "https://shop.example.com/", navigation=True).action == "continue"
    assert _run(guard, "http://127.0.0.1:8765/", navigation=True).action == "abort"
    assert guard.main_frame_blocked is True


def test_guard_dns_failure_aborts_but_is_not_counted_as_block():
    def failing(host, port):
        raise OSError("nxdomain")

    guard = probe.RequestGuard(failing)
    route = _run(guard, "https://nope.example.com/", navigation=True)
    assert route.action == "abort"
    assert guard.blocked_count == 0
    assert guard.main_frame_blocked is False


def test_guard_resolver_cache_resolves_each_host_once():
    calls = []

    def resolver(host, port):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    guard = probe.RequestGuard(resolver)
    for _ in range(3):
        _run(guard, "https://cdn.example.com/a.js")
    assert calls == ["cdn.example.com"]


def test_blocked_main_frame_from_runner_gives_unknown_blocked_host_with_counts():
    def runner(url, timeout_ms):
        raise probe.ProbeFailure(
            "BLOCKED_HOST", "blocked", blocked=probe._blocked_summary(2, {"NON_GLOBAL_IP": 2})
        )

    res = probe.probe_storefront("https://epxsmk-y0.myshopify.com/", runner=runner, resolver=public_resolver)
    assert res["status"] == "UNKNOWN"
    assert res["reason"] == "BLOCKED_HOST"
    assert res["blocked_requests"] == {"count": 2, "reasons": {"NON_GLOBAL_IP": 2}}
    assert res["expected"] == {"gtm_loaded": None, "ga4_seen": None, "pixel_seen": None}


def test_blocked_requests_are_counted_in_ok_result_without_urls():
    home_url = "https://epxsmk-y0.myshopify.com/"
    cap = PageCapture(
        url=home_url,
        http_status=200,
        requests=["https://www.googletagmanager.com/gtm.js?id=GTM-T443M78T"],
        blocked_count=3,
        blocked_reasons={"NON_GLOBAL_IP": 2, "DENIED_HOSTNAME": 1},
    )
    res = probe.probe_storefront(home_url, runner=make_runner({home_url: cap}), resolver=public_resolver)
    assert res["status"] == "OK"
    assert res["blocked_requests"] == {"count": 3, "reasons": {"DENIED_HOSTNAME": 1, "NON_GLOBAL_IP": 2}}
    assert "127.0.0.1" not in json.dumps(res)


def test_clean_result_reports_zero_blocked_requests():
    home_url = "https://epxsmk-y0.myshopify.com/"
    res = probe.probe_storefront(
        home_url, runner=make_runner({home_url: PageCapture(url=home_url, http_status=200)}), resolver=public_resolver
    )
    assert res["blocked_requests"] == {"count": 0, "reasons": {}}


def test_probe_runs_runner_on_worker_thread_inside_running_event_loop():
    home_url = "https://epxsmk-y0.myshopify.com/"
    seen = {}

    def runner(url, timeout_ms):
        seen["thread"] = threading.current_thread()
        try:
            asyncio.get_running_loop()
            seen["loop_running"] = True
        except RuntimeError:
            seen["loop_running"] = False
        return PageCapture(url=url, http_status=200)

    async def main():
        # Same shape as an asyncio MCP server invoking a sync tool on its loop thread.
        return probe.probe_storefront(home_url, runner=runner, resolver=public_resolver)

    res = asyncio.run(main())
    assert res["status"] == "OK"
    assert seen["loop_running"] is False
    assert seen["thread"] is not threading.main_thread()
    assert seen["thread"].name.startswith("storefront-probe")


def test_overall_timeout_returns_unknown_timeout(monkeypatch):
    home_url = "https://epxsmk-y0.myshopify.com/"
    release = threading.Event()
    monkeypatch.setattr(probe, "OVERALL_TIMEOUT_S", 0.2)

    def slow_runner(url, timeout_ms):
        release.wait(5)
        return PageCapture(url=url, http_status=200)

    start = time.monotonic()
    try:
        res = probe.probe_storefront(home_url, runner=slow_runner, resolver=public_resolver)
    finally:
        release.set()
    assert time.monotonic() - start < 3
    assert res["status"] == "UNKNOWN"
    assert res["reason"] == "TIMEOUT"
    assert res["blocked_requests"] == {"count": 0, "reasons": {}}


def test_overall_timeout_is_under_policy_limit():
    assert probe.OVERALL_TIMEOUT_S < 90
    assert probe._EXECUTOR._max_workers == 2


def _install_fake_playwright(monkeypatch, behaviour):
    """Installs a fake playwright.sync_api; `behaviour(ctx, url)` simulates the page navigation."""
    state = {"context_kwargs": None, "routes": []}

    class Page:
        url = "https://shop.example.com/"

        def __init__(self, ctx):
            self.ctx = ctx

        def goto(self, url, wait_until=None, timeout=None):
            return behaviour(self.ctx, url)

        def title(self):
            return "Shop"

        def eval_on_selector_all(self, sel, js):
            return []

    class Context:
        def route(self, pattern, handler):
            state["routes"].append((pattern, handler))
            self.handler = handler

        def new_page(self):
            return Page(self)

        def close(self):
            pass

    class Browser:
        def new_context(self, **kwargs):
            state["context_kwargs"] = kwargs
            return Context()

        def close(self):
            pass

    class PW:
        class chromium:  # noqa: N801
            @staticmethod
            def launch(headless=True):
                return Browser()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: PW()
    pkg = types.ModuleType("playwright")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return state


def test_playwright_runner_context_hardening_and_blocked_main_frame(monkeypatch):
    def behaviour(ctx, url):
        # The navigation redirects to the loopback LLM router; the guard aborts it.
        route = FakeRoute(FakeRequest("http://127.0.0.1:4000/v1", navigation=True))
        ctx.handler(route)
        assert route.action == "abort"
        raise RuntimeError("net::ERR_BLOCKED_BY_CLIENT")

    state = _install_fake_playwright(monkeypatch, behaviour)
    with pytest.raises(probe.ProbeFailure) as ei:
        probe._playwright_runner("https://shop.example.com/", 1000, resolver=public_resolver)
    assert ei.value.reason == "BLOCKED_HOST"
    assert ei.value.blocked == {"count": 1, "reasons": {"NON_GLOBAL_IP": 1}}
    kw = state["context_kwargs"]
    assert kw["ignore_https_errors"] is False
    assert kw["service_workers"] == "block"
    assert kw["accept_downloads"] is False
    assert not kw.get("permissions")
    assert state["routes"] and state["routes"][0][0] == "**/*"


def test_playwright_runner_reports_blocked_subresources_and_keeps_allowed(monkeypatch):
    def behaviour(ctx, url):
        ok = FakeRoute(FakeRequest("https://www.googletagmanager.com/gtm.js?id=GTM-T443M78T"))
        ctx.handler(ok)
        bad = FakeRoute(FakeRequest("http://169.254.169.254/latest/meta-data/"))
        ctx.handler(bad)
        assert (ok.action, bad.action) == ("continue", "abort")

        class Resp:
            status = 200

        return Resp()

    _install_fake_playwright(monkeypatch, behaviour)
    cap = probe._playwright_runner("https://shop.example.com/", 1000, resolver=public_resolver)
    assert cap.http_status == 200
    assert cap.requests == ["https://www.googletagmanager.com/gtm.js?id=GTM-T443M78T"]
    assert cap.blocked_count == 1 and cap.blocked_reasons == {"NON_GLOBAL_IP": 1}
