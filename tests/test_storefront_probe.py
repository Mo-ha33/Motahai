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
