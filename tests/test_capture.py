"""
test_capture.py — FX-2: secure storefront capture (quarantine + total-checked merge).

Route tests run against a minimal FastAPI app containing only the capture router (so the project's global
CORSMiddleware is out of the picture); one test checks the real app for route wiring and the removed legacy routes.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce import capture_routes
from src.ameen_workforce.capture import merge_pending_captures
from src.ameen_workforce.capture_routes import router as capture_router
from src.ameen_workforce.checkout_context import capture_checkout_context, load_checkout_context
from src.ameen_workforce.credentials import decrypt_value, encrypt_value
from src.ameen_workforce.db import Order, PendingCapture, create_tenant, utcnow
from src.ameen_workforce.webhook_routes import get_session_factory_dep

ORIGIN = "https://shop.example.com"
CONN_IP = "197.35.120.40"
UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36"
FBP = "fb.1.1700000000000.1234567890"
FBC = "fb.1.1700000000000.ABC_DEF"


class _ClientAddr:
    """ASGI wrapper that sets the peer address (TestClient cannot)."""
    def __init__(self, app, holder):
        self.app, self.holder = app, holder

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(self.holder["ip"], 50000))
        await self.app(scope, receive, send)


@pytest.fixture(autouse=True)
def _reset_limiters(monkeypatch):
    monkeypatch.delenv(capture_routes.TRUSTED_PROXY_ENV, raising=False)
    capture_routes.ip_limiter.reset()
    capture_routes.tenant_limiter.reset()
    yield
    capture_routes.ip_limiter.reset()
    capture_routes.tenant_limiter.reset()


@pytest.fixture
def salla_tenant(db_session):
    return create_tenant(db_session, name="Salla Shop", platform="salla", shop_domain="778899", currency="SAR",
                         country="SA", storefront_url="https://shop.example.com/ar/")


@pytest.fixture
def other_tenant(db_session):
    return create_tenant(db_session, name="Other Shop", platform="salla", shop_domain="112233", currency="SAR",
                         country="SA", storefront_url="https://other.example.com")


@pytest.fixture
def addr():
    return {"ip": CONN_IP}


@pytest.fixture
def client(db_session, fernet_key, addr):
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(capture_router)
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    return TestClient(_ClientAddr(app, addr))


def body(**kw):
    data = {"order_id": "5001", "order_total": 350.5, "currency": "SAR",
            "attribution": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "1001", "utm_content": "2002",
                            "ad_id": "2002", "fbp": FBP, "fbc": FBC, "ttclid": "TT-1", "sccid": "SC-1"}}
    data.update(kw)
    return data


def post(client, payload=None, key="778899", origin=ORIGIN, headers=None, **kw):
    h = {"User-Agent": UA}
    if origin is not None:
        h["Origin"] = origin
    h.update(headers or {})
    if "content" in kw:
        return client.post(f"/v1/capture/{key}", headers=h, **kw)
    return client.post(f"/v1/capture/{key}", json=payload if payload is not None else body(), headers=h)


def make_order(session, tenant, order_id="5001", value=350.5, currency="SAR", **kw):
    order = Order(tenant_id=tenant.id, platform_order_id=order_id, value=value, order_total=value, currency=currency,
                  current_status="pending", **kw)
    session.add(order)
    session.commit()
    return order


def add_capture(session, tenant, order_id="5001", total=350.5, currency="SAR", ip=CONN_IP, ua=UA, received_at=None,
                **attr):
    now = received_at or utcnow()
    row = PendingCapture(tenant_id=tenant.id, platform_order_id=order_id, order_total=total, currency=currency,
                         ip_ciphertext=encrypt_value(ip) if ip else None,
                         ua_ciphertext=encrypt_value(ua) if ua else None,
                         received_at=now, expires_at=now + timedelta(hours=48), **attr)
    session.add(row)
    session.commit()
    return row


# --- route: acceptance and storage ------------------------------------------------------------------------

def test_accepted_capture_stored_encrypted_and_no_order_created(client, db_session, salla_tenant):
    res = post(client)
    assert res.status_code == 204
    assert res.content == b""
    assert res.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in res.headers

    row = db_session.scalar(select(PendingCapture))
    assert row.tenant_id == salla_tenant.id and row.platform_order_id == "5001"
    assert row.order_total == 350.5 and row.currency == "SAR"
    assert (row.utm_source, row.ad_id, row.fbp, row.fbc, row.ttclid, row.sccid) == (
        "meta", "2002", FBP, FBC, "TT-1", "SC-1")
    assert 47 * 3600 < (row.expires_at - row.received_at).total_seconds() <= 48 * 3600
    assert decrypt_value(row.ip_ciphertext) == CONN_IP
    assert decrypt_value(row.ua_ciphertext) == UA
    for column in sa_inspect(PendingCapture).columns:  # raw IP/UA are in no column
        value = getattr(row, column.key)
        assert CONN_IP not in str(value) and UA not in str(value)
    assert db_session.scalar(select(Order)) is None


def test_accepts_text_plain_like_sendbeacon_and_integer_order_id(client, db_session, salla_tenant):
    res = post(client, content='{"order_id": 5002, "order_total": 10, "currency": "sar"}',
               headers={"Content-Type": "text/plain;charset=UTF-8"})
    assert res.status_code == 204
    row = db_session.scalar(select(PendingCapture))
    assert row.platform_order_id == "5002" and row.currency == "SAR"
    assert row.fbp is None


def test_body_supplied_ip_and_user_agent_are_ignored(client, db_session, salla_tenant):
    payload = body(client_ip="1.2.3.4", ip="5.6.7.8", user_agent="Evil/1.0", merchant="999")
    payload["attribution"]["client_ip"] = "9.9.9.9"
    assert post(client, payload).status_code == 204
    row = db_session.scalar(select(PendingCapture))
    assert decrypt_value(row.ip_ciphertext) == CONN_IP
    assert decrypt_value(row.ua_ciphertext) == UA


def test_attribution_is_sanitized(client, db_session, salla_tenant):
    payload = body()
    payload["attribution"].update(fbp="not-a-cookie", ad_id="12abc", utm_source="x\x00y")
    assert post(client, payload).status_code == 204
    row = db_session.scalar(select(PendingCapture))
    assert row.fbp is None and row.ad_id is None and row.utm_source == "xy" and row.fbc == FBC


def test_capture_without_attribution_is_accepted(client, db_session, salla_tenant):
    assert post(client, {"order_id": "7", "order_total": 1, "currency": "SAR"}).status_code == 204
    assert db_session.scalar(select(PendingCapture)).fbp is None


# --- route: origin / CORS -----------------------------------------------------------------------------------

@pytest.mark.parametrize("origin", [None, "https://evil.example.com", "http://shop.example.com",
                                    "https://shop.example.com:8443", "https://shop.example.com.evil.com", "null", "*"])
def test_wrong_or_missing_origin_is_403_and_nothing_stored(client, db_session, salla_tenant, origin):
    res = post(client, origin=origin)
    assert res.status_code == 403
    assert "access-control-allow-origin" not in res.headers
    assert db_session.scalar(select(PendingCapture)) is None


def test_origin_match_is_normalized(client, db_session, salla_tenant):
    assert post(client, origin="HTTPS://Shop.Example.com:443").status_code == 204


def test_salla_tenant_without_storefront_url_accepts_nothing(client, db_session):
    create_tenant(db_session, name="No URL", platform="salla", shop_domain="555", currency="SAR")
    assert post(client, key="555", origin=ORIGIN).status_code == 403


def test_shopify_tenant_defaults_to_https_shop_domain_origin(client, db_session):
    create_tenant(db_session, name="Shopify", platform="shopify", shop_domain="demo.myshopify.com", currency="EGP")
    assert post(client, key="demo.myshopify.com", origin="https://demo.myshopify.com").status_code == 204
    assert post(client, key="demo.myshopify.com", origin=ORIGIN).status_code == 403


def test_unknown_or_inactive_tenant_is_404(client, db_session, salla_tenant):
    assert post(client, key="nope").status_code == 404
    create_tenant(db_session, name="Off", platform="salla", shop_domain="404404", currency="SAR",
                  storefront_url=ORIGIN, active=False)
    assert post(client, key="404404").status_code == 404


def test_preflight_allowed_origin_and_denied_origin(client, salla_tenant):
    ok = client.options("/v1/capture/778899", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 204
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in ok.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in ok.headers
    bad = client.options("/v1/capture/778899", headers={"Origin": "https://evil.example.com",
                                                        "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 403
    assert client.options("/v1/capture/778899").status_code == 403


# --- route: validation, size, rate limit ----------------------------------------------------------------------

def test_oversized_body_is_413(client, db_session, salla_tenant):
    big = body()
    big["pad"] = "x" * 5000
    assert post(client, big).status_code == 413
    assert db_session.scalar(select(PendingCapture)) is None


def test_oversized_chunked_body_without_content_length_is_413(client, db_session, salla_tenant):
    def gen():
        yield b'{"order_id": "1", "order_total": 1, "currency": "SAR", "pad": "'
        yield b"x" * 5000
        yield b'"}'
    assert post(client, content=gen(), headers={"Content-Type": "text/plain"}).status_code == 413


@pytest.mark.parametrize("payload", [
    {"order_total": 1, "currency": "SAR"},
    {"order_id": "1", "currency": "SAR"},
    {"order_id": "1", "order_total": 1},
    {"order_id": "1", "order_total": -1, "currency": "SAR"},
    {"order_id": "1", "order_total": "12", "currency": "SAR"},
    {"order_id": "1", "order_total": True, "currency": "SAR"},
    {"order_id": "1", "order_total": 1, "currency": "SA"},
    {"order_id": "1", "order_total": 1, "currency": "S1R"},
    {"order_id": "x" * 65, "order_total": 1, "currency": "SAR"},
    {"order_id": "", "order_total": 1, "currency": "SAR"},
    {"order_id": ["1"], "order_total": 1, "currency": "SAR"},
    {"order_id": "1", "order_total": 1, "currency": "SAR", "attribution": "x"},
])
def test_invalid_payload_is_422(client, db_session, salla_tenant, payload):
    assert post(client, payload).status_code == 422
    assert db_session.scalar(select(PendingCapture)) is None


def test_non_json_and_wrong_content_type(client, db_session, salla_tenant):
    assert post(client, content="not json", headers={"Content-Type": "text/plain"}).status_code == 422
    assert post(client, content="[1,2]", headers={"Content-Type": "text/plain"}).status_code == 422
    assert post(client, content="a=1", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415


def test_per_ip_rate_limit_returns_429(client, db_session, salla_tenant, addr):
    for _ in range(capture_routes.IP_LIMIT_PER_MIN):
        assert post(client).status_code == 204
    res = post(client)
    assert res.status_code == 429 and res.headers["retry-after"]
    addr["ip"] = "197.35.120.41"  # a different client is unaffected
    assert post(client).status_code == 204


def test_per_tenant_rate_limit_returns_429(client, db_session, salla_tenant, other_tenant, addr, monkeypatch):
    monkeypatch.setattr(capture_routes, "tenant_limiter", capture_routes.SlidingWindowLimiter(3))
    for i in range(3):
        addr["ip"] = f"197.35.120.{50 + i}"
        assert post(client).status_code == 204
    addr["ip"] = "197.35.120.60"
    assert post(client).status_code == 429
    assert post(client, key="112233", origin="https://other.example.com").status_code == 204  # other tenant unaffected


def test_forbidden_requests_do_not_consume_tenant_budget(client, salla_tenant, monkeypatch, addr):
    monkeypatch.setattr(capture_routes, "tenant_limiter", capture_routes.SlidingWindowLimiter(1))
    for i in range(5):
        addr["ip"] = f"197.35.121.{i}"
        assert post(client, origin="https://evil.example.com").status_code == 403
    addr["ip"] = "197.35.121.99"
    assert post(client).status_code == 204


def test_sliding_window_expires():
    limiter = capture_routes.SlidingWindowLimiter(2, window=60)
    assert limiter.allow("k", now=0) and limiter.allow("k", now=1)
    assert not limiter.allow("k", now=2)
    assert limiter.allow("k", now=61.5)


# --- route: IP derivation -------------------------------------------------------------------------------------

def test_x_forwarded_for_ignored_unless_trusted_proxy(client, db_session, salla_tenant):
    assert post(client, headers={"X-Forwarded-For": "197.35.120.77, 10.0.0.1"}).status_code == 204
    assert decrypt_value(db_session.scalar(select(PendingCapture)).ip_ciphertext) == CONN_IP


def test_x_forwarded_for_first_hop_honored_with_trusted_proxy(client, db_session, salla_tenant, monkeypatch):
    monkeypatch.setenv(capture_routes.TRUSTED_PROXY_ENV, "1")
    assert post(client, headers={"X-Forwarded-For": "197.35.120.77, 10.0.0.1"}).status_code == 204
    assert decrypt_value(db_session.scalar(select(PendingCapture)).ip_ciphertext) == "197.35.120.77"


def test_trusted_proxy_with_garbage_forwarded_value_stores_no_ip(client, db_session, salla_tenant, monkeypatch):
    monkeypatch.setenv(capture_routes.TRUSTED_PROXY_ENV, "1")
    assert post(client, headers={"X-Forwarded-For": "not-an-ip"}).status_code == 204
    assert db_session.scalar(select(PendingCapture)).ip_ciphertext is None


def test_non_public_connection_address_is_not_stored(client, db_session, salla_tenant, addr):
    addr["ip"] = "10.0.0.5"  # e.g. an unconfigured reverse proxy
    assert post(client).status_code == 204
    row = db_session.scalar(select(PendingCapture))
    assert row.ip_ciphertext is None and decrypt_value(row.ua_ciphertext) == UA


def test_without_fernet_key_attribution_kept_and_ip_ua_skipped(db_session, salla_tenant, addr, monkeypatch):
    monkeypatch.delenv("MOTAHAI_FERNET_KEY", raising=False)
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(capture_router)
    app.dependency_overrides[get_session_factory_dep] = lambda: factory
    assert post(TestClient(_ClientAddr(app, addr))).status_code == 204
    row = db_session.scalar(select(PendingCapture))
    assert row.fbp == FBP and row.ip_ciphertext is None and row.ua_ciphertext is None


# --- wiring / legacy routes ----------------------------------------------------------------------------------

def test_real_app_has_new_route_and_legacy_routes_are_gone(db_session, monkeypatch):
    from src.ameen_workforce.service import app
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setitem(app.dependency_overrides, get_session_factory_dep, lambda: factory)
    c = TestClient(app)
    # Empty test DB => shop unknown => a wired route answers 404 "Unknown shop"; a missing route answers "Not Found".
    res = c.options("/v1/capture/demo-store.myshopify.com")
    assert res.status_code == 404 and res.json() == {"detail": "Unknown shop"}
    for path in ("/storefront/capture", "/webhooks/salla/capture"):
        res = c.post(path, json={"merchant": "778899", "order_id": "1", "client_ip": "1.2.3.4"})
        assert res.status_code in (404, 405)


# --- merge ---------------------------------------------------------------------------------------------------

def test_matching_total_fills_empty_attribution_and_creates_context(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant)
    cap = add_capture(db_session, salla_tenant, utm_source="meta", ad_id="2002", fbp=FBP, fbc=FBC)
    result = merge_pending_captures(db_session)
    assert result == {"merged": 1, "rejected": 0, "expired_deleted": 0, "waiting": 0}
    db_session.refresh(order)
    db_session.refresh(cap)
    assert (order.utm_source, order.ad_id, order.fbp, order.fbc) == ("meta", "2002", FBP, FBC)
    assert load_checkout_context(db_session, order.id) == (CONN_IP, UA)
    assert cap.merged_at is not None and cap.ip_ciphertext is None and cap.ua_ciphertext is None


@pytest.mark.parametrize("stored,captured,currency,ok", [
    (1000.0, 1009.0, "SAR", True),     # within 1%
    (1000.0, 1011.0, "SAR", False),    # beyond 1%
    (50.0, 50.9, "SAR", True),         # small order: 1.0 unit floor
    (50.0, 51.5, "SAR", False),
    (1000.0, 1000.0, "EGP", False),    # currency mismatch
    (1000.0, 1000.0, "sar", True),     # currency compare is case-insensitive
])
def test_total_second_factor(db_session, fernet_key, salla_tenant, stored, captured, currency, ok):
    order = make_order(db_session, salla_tenant, value=stored)
    add_capture(db_session, salla_tenant, total=captured, currency=currency, fbp=FBP)
    result = merge_pending_captures(db_session)
    db_session.refresh(order)
    if ok:
        assert result["merged"] == 1 and order.fbp == FBP
    else:
        assert result["rejected"] == 1 and result["merged"] == 0
        assert order.fbp is None and load_checkout_context(db_session, order.id) == (None, None)
        row = db_session.scalar(select(PendingCapture))
        assert row.rejected_reason == "total_mismatch" and row.ip_ciphertext is None


def test_total_may_match_net_value_when_full_total_differs(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant, value=300.0)
    order.order_total = 500.0
    db_session.commit()
    add_capture(db_session, salla_tenant, total=500.0, fbp=FBP)
    assert merge_pending_captures(db_session)["merged"] == 1


def test_existing_attribution_and_context_are_never_overwritten(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant, fbp="fb.1.1600000000000.999", utm_source="google")
    capture_checkout_context(db_session, order.id, "41.40.30.20", "Shopify-UA/1.0")
    db_session.commit()
    add_capture(db_session, salla_tenant, utm_source="meta", utm_medium="paid", fbp=FBP, fbc=FBC)
    assert merge_pending_captures(db_session)["merged"] == 1
    db_session.refresh(order)
    assert order.fbp == "fb.1.1600000000000.999" and order.utm_source == "google"   # kept
    assert order.utm_medium == "paid" and order.fbc == FBC                           # empty ones filled
    assert load_checkout_context(db_session, order.id) == ("41.40.30.20", "Shopify-UA/1.0")


def test_capture_before_order_waits_then_merges(db_session, fernet_key, salla_tenant):
    cap = add_capture(db_session, salla_tenant, fbp=FBP)
    assert merge_pending_captures(db_session) == {"merged": 0, "rejected": 0, "expired_deleted": 0, "waiting": 1}
    db_session.refresh(cap)
    assert cap.merged_at is None and cap.rejected_reason is None and cap.ip_ciphertext is not None
    order = make_order(db_session, salla_tenant)
    assert merge_pending_captures(db_session)["merged"] == 1
    db_session.refresh(order)
    assert order.fbp == FBP


def test_expired_captures_are_deleted_even_if_order_exists(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant)
    old = utcnow() - timedelta(hours=49)
    add_capture(db_session, salla_tenant, received_at=old, fbp=FBP)
    result = merge_pending_captures(db_session)
    assert result["expired_deleted"] == 1 and result["merged"] == 0
    assert db_session.scalar(select(PendingCapture)) is None
    db_session.refresh(order)
    assert order.fbp is None


def test_merged_and_rejected_rows_deleted_at_expiry(db_session, fernet_key, salla_tenant):
    make_order(db_session, salla_tenant)
    add_capture(db_session, salla_tenant, fbp=FBP)
    merge_pending_captures(db_session)
    assert db_session.scalar(select(PendingCapture)) is not None
    later = utcnow() + timedelta(hours=49)
    assert merge_pending_captures(db_session, now=later)["expired_deleted"] == 1
    assert db_session.scalar(select(PendingCapture)) is None


def test_duplicate_captures_earliest_valid_wins(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant)
    t0 = utcnow() - timedelta(minutes=30)
    add_capture(db_session, salla_tenant, total=9999.0, received_at=t0, fbp="fb.1.1700000000000.1111")   # forged, bad total
    good = add_capture(db_session, salla_tenant, received_at=t0 + timedelta(minutes=1), fbp=FBP)
    late = add_capture(db_session, salla_tenant, received_at=t0 + timedelta(minutes=2), fbp="fb.1.1700000000000.2222")
    result = merge_pending_captures(db_session)
    assert result == {"merged": 1, "rejected": 2, "expired_deleted": 0, "waiting": 0}
    db_session.refresh(order)
    db_session.refresh(good)
    db_session.refresh(late)
    assert order.fbp == FBP and good.merged_at is not None
    assert late.rejected_reason == "duplicate_capture"
    reasons = sorted(r.rejected_reason for r in db_session.scalars(select(PendingCapture)) if r.rejected_reason)
    assert reasons == ["duplicate_capture", "total_mismatch"]


def test_capture_arriving_after_a_merge_is_a_duplicate(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant)
    add_capture(db_session, salla_tenant, fbp=FBP)
    merge_pending_captures(db_session)
    late = add_capture(db_session, salla_tenant, fbp="fb.1.1700000000000.3333")
    assert merge_pending_captures(db_session)["rejected"] == 1
    db_session.refresh(late)
    db_session.refresh(order)
    assert late.rejected_reason == "duplicate_capture" and order.fbp == FBP


def test_cross_tenant_capture_never_merges(db_session, fernet_key, salla_tenant, other_tenant):
    other_order = make_order(db_session, other_tenant, order_id="5001")
    cap = add_capture(db_session, salla_tenant, order_id="5001", fbp=FBP)   # same id, same total, different tenant
    assert merge_pending_captures(db_session) == {"merged": 0, "rejected": 0, "expired_deleted": 0, "waiting": 1}
    db_session.refresh(other_order)
    assert other_order.fbp is None and load_checkout_context(db_session, other_order.id) == (None, None)
    own_order = make_order(db_session, salla_tenant, order_id="5001")
    assert merge_pending_captures(db_session)["merged"] == 1
    db_session.refresh(other_order)
    db_session.refresh(own_order)
    assert other_order.fbp is None and own_order.fbp == FBP
    assert cap.tenant_id == salla_tenant.id


def test_merge_is_idempotent(db_session, fernet_key, salla_tenant):
    make_order(db_session, salla_tenant)
    add_capture(db_session, salla_tenant, fbp=FBP)
    merge_pending_captures(db_session)
    assert merge_pending_captures(db_session) == {"merged": 0, "rejected": 0, "expired_deleted": 0, "waiting": 0}


def test_merge_with_unreadable_ciphertext_still_merges_attribution(db_session, fernet_key, salla_tenant):
    order = make_order(db_session, salla_tenant)
    cap = add_capture(db_session, salla_tenant, fbp=FBP)
    cap.ip_ciphertext = "garbage"
    db_session.commit()
    assert merge_pending_captures(db_session)["merged"] == 1
    db_session.refresh(order)
    assert order.fbp == FBP and load_checkout_context(db_session, order.id) == (None, None)


def test_end_to_end_post_then_webhook_order_then_merge(client, db_session, salla_tenant):
    assert post(client, body(order_id="8001", order_total=200)).status_code == 204
    assert db_session.scalar(select(Order)) is None                      # quarantined, no order created
    order = make_order(db_session, salla_tenant, order_id="8001", value=200.0)  # signed webhook creates it later
    assert merge_pending_captures(db_session)["merged"] == 1
    db_session.refresh(order)
    assert order.fbp == FBP
    assert load_checkout_context(db_session, order.id) == (CONN_IP, UA)


def test_pending_capture_has_no_pii_columns():
    names = {c.key for c in sa_inspect(PendingCapture).columns}
    assert not any(p in n for n in names for p in ("email", "phone", "name", "address"))
    assert {"ip_ciphertext", "ua_ciphertext", "received_at", "expires_at", "merged_at", "rejected_reason"} <= names
    assert "ix_pending_captures_expires_at" in {i.name for i in PendingCapture.__table__.indexes}
    assert "ix_pending_captures_tenant_order" in {i.name for i in PendingCapture.__table__.indexes}
