"""Tests for S2-6 audience exports (refusers exclusion list, delivered-buyer lookalike seed)."""

import csv
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from src.ameen_workforce.audiences import (
    MIN_LIST_ROWS,
    delivered_buyer_hashes,
    export_tenant_audiences,
    refuser_hashes,
    write_customer_list_csv,
)
from src.ameen_workforce.db import CapiEvent, Order, OrderStatusEvent, create_tenant

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def H(raw: str) -> str:
    """SHA-256 hex, the only form in which the DB stores identifiers."""
    return hashlib.sha256(raw.encode()).hexdigest()


def make_order(session, tenant, platform_id, *, status, events=(), is_cod=True, value=100.0,
               phone=None, email=None, created=None, platform_created=None):
    order = Order(
        tenant_id=tenant.id, platform_order_id=platform_id, is_cod=is_cod, value=value, currency="EGP",
        current_status=status, phone_hash=phone, email_hash=email,
        created_at=created or NOW - timedelta(days=5), created_at_platform=platform_created,
    )
    session.add(order)
    session.flush()
    for event_status in events:
        session.add(OrderStatusEvent(order_id=order.id, status=event_status, source="webhook"))
    session.flush()
    return order


def make_refusal(session, tenant, platform_id, phone, *, email=None, created=None, platform_created=None):
    """COD order dispatched, then refused (cancelled after shipping)."""
    return make_order(session, tenant, platform_id, status="cancelled", events=("pending", "shipped"),
                      phone=phone, email=email, created=created, platform_created=platform_created)


def make_delivered(session, tenant, platform_id, phone, *, value=100.0, email=None, created=None,
                   platform_created=None):
    return make_order(session, tenant, platform_id, status="delivered", events=("pending", "shipped", "delivered"),
                      phone=phone, email=email, value=value, created=created, platform_created=platform_created)


@pytest.fixture
def tenant(shadow_tenant):
    return shadow_tenant


# --- refusal definition -----------------------------------------------------------------------------------

def test_refusal_requires_prior_shipped_status(db_session, tenant):
    phone = H("+201000000001")
    make_refusal(db_session, tenant, "R1", phone)
    make_refusal(db_session, tenant, "R2", phone)
    # Cancel before dispatch is not a refusal: history has no shipped event.
    other = H("+201000000002")
    for i in range(3):
        make_order(db_session, tenant, f"C{i}", status="cancelled", events=("pending",), phone=other)
    # A prepaid (non-COD) order refused after shipping is not a COD refusal.
    make_order(db_session, tenant, "P1", status="refunded", events=("paid", "shipped"), is_cod=False,
               phone=other)

    rows = refuser_hashes(db_session, tenant.id, min_refusals=1, now=NOW)

    assert [r["phone_hash"] for r in rows] == [phone]
    assert rows[0]["refusals"] == 2


def test_held_paid_unfulfilled_cancel_is_not_a_refusal(db_session, tenant):
    phone = H("+201000000004")
    # Held COD order (paid, never dispatched) that the merchant cancelled: not a door refusal.
    make_order(db_session, tenant, "PU1", status="cancelled", events=("paid_unfulfilled",), phone=phone)
    make_order(db_session, tenant, "PU2", status="cancelled", events=("paid_unfulfilled",), phone=phone)

    assert refuser_hashes(db_session, tenant.id, min_refusals=1, now=NOW) == []


def test_refunded_after_shipping_counts_as_refusal(db_session, tenant):
    phone = H("+201000000003")
    make_order(db_session, tenant, "F1", status="refunded", events=("shipped",), phone=phone)
    make_order(db_session, tenant, "F2", status="refunded", events=("paid_unfulfilled", "shipped"), phone=phone)

    rows = refuser_hashes(db_session, tenant.id, min_refusals=2, now=NOW)

    assert rows == [{"phone_hash": phone, "email_hash": None, "refusals": 2}]


# --- threshold, window, email ----------------------------------------------------------------------------

def test_min_refusals_threshold(db_session, tenant):
    once = H("+201000000010")
    twice = H("+201000000011")
    make_refusal(db_session, tenant, "T1", once)
    make_refusal(db_session, tenant, "T2", twice)
    make_refusal(db_session, tenant, "T3", twice)

    assert [r["phone_hash"] for r in refuser_hashes(db_session, tenant.id, min_refusals=2, now=NOW)] == [twice]
    assert {r["phone_hash"] for r in refuser_hashes(db_session, tenant.id, min_refusals=1, now=NOW)} == {once, twice}


def test_since_days_window(db_session, tenant):
    phone = H("+201000000020")
    make_refusal(db_session, tenant, "W1", phone, created=NOW - timedelta(days=200))  # outside 180d
    make_refusal(db_session, tenant, "W2", phone, created=NOW - timedelta(days=10))   # inside

    assert refuser_hashes(db_session, tenant.id, since_days=180, min_refusals=2, now=NOW) == []
    inside_long_window = refuser_hashes(db_session, tenant.id, since_days=365, min_refusals=2, now=NOW)
    assert inside_long_window[0]["refusals"] == 2


def test_window_uses_platform_time_over_ingest_time(db_session, tenant):
    phone = H("+201000000021")
    # Ingested recently (created_at inside the window) but the platform order is 200 days old: outside.
    make_refusal(db_session, tenant, "PT1", phone, created=NOW - timedelta(days=10),
                 platform_created=NOW - timedelta(days=200))
    make_refusal(db_session, tenant, "PT2", phone, created=NOW - timedelta(days=10),
                 platform_created=NOW - timedelta(days=200))

    assert refuser_hashes(db_session, tenant.id, since_days=180, min_refusals=1, now=NOW) == []
    assert refuser_hashes(db_session, tenant.id, since_days=365, min_refusals=2, now=NOW)[0]["refusals"] == 2


def test_window_falls_back_to_created_at_when_platform_time_missing(db_session, tenant):
    phone = H("+201000000022")
    make_refusal(db_session, tenant, "FB-R1", phone, created=NOW - timedelta(days=10), platform_created=None)
    make_refusal(db_session, tenant, "FB-R2", phone, created=NOW - timedelta(days=10), platform_created=None)

    assert refuser_hashes(db_session, tenant.id, since_days=180, min_refusals=2, now=NOW)[0]["refusals"] == 2


def test_delivered_window_uses_platform_time(db_session, tenant):
    phone = H("+201000000023")
    make_delivered(db_session, tenant, "PD1", phone, created=NOW - timedelta(days=10),
                   platform_created=NOW - timedelta(days=400))

    assert delivered_buyer_hashes(db_session, tenant.id, since_days=365, now=NOW) == []


def test_refuser_email_is_most_recent_non_null(db_session, tenant):
    phone = H("+201000000030")
    old_email, new_email = H("old@example.com"), H("new@example.com")
    make_refusal(db_session, tenant, "E1", phone, email=old_email, created=NOW - timedelta(days=20))
    make_refusal(db_session, tenant, "E2", phone, email=None, created=NOW - timedelta(days=10))
    make_refusal(db_session, tenant, "E3", phone, email=new_email, created=NOW - timedelta(days=2))
    make_refusal(db_session, tenant, "E4", phone, email=None, created=NOW - timedelta(days=1))

    rows = refuser_hashes(db_session, tenant.id, min_refusals=2, now=NOW)

    assert rows[0]["email_hash"] == new_email


def test_refusals_without_phone_are_skipped(db_session, tenant):
    make_refusal(db_session, tenant, "N1", None, email=H("nophone@example.com"))
    make_refusal(db_session, tenant, "N2", None, email=H("nophone@example.com"))

    assert refuser_hashes(db_session, tenant.id, min_refusals=1, now=NOW) == []


# --- delivered buyers ------------------------------------------------------------------------------------

def test_delivered_buyers_aggregate_value(db_session, tenant):
    phone = H("+201000000040")
    email = H("buyer@example.com")
    make_delivered(db_session, tenant, "D1", phone, value=100.0, email=email)
    make_delivered(db_session, tenant, "D2", phone, value=250.5, email=email)
    make_order(db_session, tenant, "D3", status="pending", events=("pending",), phone=phone, value=999.0)

    rows = delivered_buyer_hashes(db_session, tenant.id, now=NOW)

    assert rows == [{"phone_hash": phone, "email_hash": email, "value": 350.5, "orders": 2}]


def test_delivered_via_deliveredpurchase_capi_event(db_session, tenant):
    shadow_phone = H("+201000000041")
    pending_phone = H("+201000000042")
    shadow_order = make_order(db_session, tenant, "CE1", status="pending", events=("pending",),
                              phone=shadow_phone, value=80.0)
    pending_order = make_order(db_session, tenant, "CE2", status="pending", events=("pending",),
                               phone=pending_phone, value=70.0)
    db_session.add(CapiEvent(tenant_id=tenant.id, order_id=shadow_order.id, event_name="DeliveredPurchase",
                             event_id=f"delivered_{shadow_order.id}", status="shadow"))
    db_session.add(CapiEvent(tenant_id=tenant.id, order_id=pending_order.id, event_name="DeliveredPurchase",
                             event_id=f"delivered_{pending_order.id}", status="pending"))
    db_session.flush()

    rows = delivered_buyer_hashes(db_session, tenant.id, now=NOW)

    assert [(r["phone_hash"], r["value"]) for r in rows] == [(shadow_phone, 80.0)]


def test_delivered_falls_back_to_email_when_phone_missing(db_session, tenant):
    email = H("nophone-buyer@example.com")
    make_delivered(db_session, tenant, "FB1", None, value=40.0, email=email)

    rows = delivered_buyer_hashes(db_session, tenant.id, now=NOW)

    assert rows == [{"phone_hash": None, "email_hash": email, "value": 40.0, "orders": 1}]


def test_delivered_window_applies(db_session, tenant):
    phone = H("+201000000050")
    make_delivered(db_session, tenant, "OLD1", phone, created=NOW - timedelta(days=400))

    assert delivered_buyer_hashes(db_session, tenant.id, since_days=365, now=NOW) == []
    assert len(delivered_buyer_hashes(db_session, tenant.id, since_days=500, now=NOW)) == 1


def test_customer_with_any_refusal_is_excluded_from_seed(db_session, tenant):
    phone = H("+201000000060")
    make_delivered(db_session, tenant, "X1", phone, value=100.0)
    make_refusal(db_session, tenant, "X2", phone)  # a single refusal is enough (threshold 1)

    assert delivered_buyer_hashes(db_session, tenant.id, now=NOW) == []


def test_refusal_matched_by_email_excludes_buyer(db_session, tenant):
    buyer_phone, buyer_email = H("+201000000070"), H("shared@example.com")
    make_delivered(db_session, tenant, "M1", buyer_phone, email=buyer_email)
    make_refusal(db_session, tenant, "M2", H("+201000000071"), email=buyer_email)  # same email, other phone

    assert delivered_buyer_hashes(db_session, tenant.id, now=NOW) == []


def test_refusal_outside_lookback_does_not_exclude(db_session, tenant):
    phone = H("+201000000080")
    make_delivered(db_session, tenant, "L1", phone)
    make_refusal(db_session, tenant, "L2", phone, created=NOW - timedelta(days=300))

    assert delivered_buyer_hashes(db_session, tenant.id, now=NOW, refusal_since_days=180) != []
    assert delivered_buyer_hashes(db_session, tenant.id, now=NOW, refusal_since_days=None) == []


# --- tenant isolation ------------------------------------------------------------------------------------

def test_cross_tenant_isolation(db_session, tenant):
    other = create_tenant(db_session, name="Other Shop", platform="shopify", shop_domain="other.myshopify.com")
    shared_phone = H("+201000000090")
    other_only_phone = H("+201000000091")
    other_buyer_phone = H("+201000000092")

    # Other tenant: serial refuser on shared_phone and a delivered buyer.
    make_refusal(db_session, other, "O1", shared_phone)
    make_refusal(db_session, other, "O2", shared_phone)
    make_refusal(db_session, other, "O3", other_only_phone)
    make_delivered(db_session, other, "O4", other_buyer_phone, value=500.0)

    # Our tenant: the same customer as a buyer, and nothing else.
    make_delivered(db_session, tenant, "T-1", shared_phone, value=60.0)

    exclude = refuser_hashes(db_session, tenant.id, min_refusals=1, now=NOW)
    seed = delivered_buyer_hashes(db_session, tenant.id, now=NOW)

    assert exclude == []  # other tenant's refusals never appear here
    assert [(r["phone_hash"], r["value"]) for r in seed] == [(shared_phone, 60.0)]  # not excluded by other tenant
    assert other_buyer_phone not in {r["phone_hash"] for r in seed}


def test_export_never_leaks_other_tenant_rows(db_session, tenant, tmp_path):
    other = create_tenant(db_session, name="Other Shop", platform="shopify", shop_domain="leak.myshopify.com")
    foreign_refuser = H("+201000000100")
    foreign_buyer = H("+201000000101")
    make_refusal(db_session, other, "LK1", foreign_refuser)
    make_refusal(db_session, other, "LK2", foreign_refuser)
    make_delivered(db_session, other, "LK3", foreign_buyer)
    make_delivered(db_session, tenant, "OWN1", H("+201000000102"))

    export_tenant_audiences(db_session, tenant.id, tmp_path, now=NOW)

    contents = (tmp_path / f"{tenant.id}_exclude_refusers.csv").read_text() + \
        (tmp_path / f"{tenant.id}_seed_delivered_buyers.csv").read_text()
    assert foreign_refuser not in contents
    assert foreign_buyer not in contents
    assert H("+201000000102") in (tmp_path / f"{tenant.id}_seed_delivered_buyers.csv").read_text()


# --- CSV ---------------------------------------------------------------------------------------------------

def _read_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_csv_header_without_value(tmp_path):
    path = tmp_path / "list.csv"
    rows = [{"phone_hash": H("a"), "email_hash": H("b"), "value": 12.5}]

    count = write_customer_list_csv(rows, path)

    assert count == 1
    assert _read_rows(path) == [["phone", "email"], [H("a"), H("b")]]


def test_csv_header_with_value(tmp_path):
    path = tmp_path / "seed.csv"
    rows = [
        {"phone_hash": H("a"), "email_hash": None, "value": 12.5},
        {"phone_hash": None, "email_hash": H("c"), "value": 3},
    ]

    write_customer_list_csv(rows, path, include_value=True)

    assert _read_rows(path) == [
        ["phone", "email", "value"],
        [H("a"), "", "12.50"],
        ["", H("c"), "3.00"],
    ]


def test_csv_writes_only_hashes_and_refuses_raw_identifiers(tmp_path):
    path = tmp_path / "bad.csv"
    rows = [{"phone_hash": H("ok"), "email_hash": None, "value": 1.0},
            {"phone_hash": "+201001234567", "email_hash": None, "value": 1.0}]

    with pytest.raises(ValueError):
        write_customer_list_csv(rows, path, include_value=True)
    assert not path.exists()  # validated before writing, so no partial file


def test_csv_skips_rows_without_identifiers(tmp_path):
    path = tmp_path / "skip.csv"
    rows = [{"phone_hash": None, "email_hash": None}, {"phone_hash": H("x"), "email_hash": None}]

    assert write_customer_list_csv(rows, path) == 1
    assert _read_rows(path) == [["phone", "email"], [H("x"), ""]]


# --- export and small-list warning -----------------------------------------------------------------------

def test_export_writes_named_files_and_counts(db_session, tenant, tmp_path):
    make_refusal(db_session, tenant, "EX1", H("+201000000110"))
    make_refusal(db_session, tenant, "EX2", H("+201000000110"))
    make_delivered(db_session, tenant, "EX3", H("+201000000111"), value=90.0)

    result = export_tenant_audiences(db_session, tenant.id, tmp_path, now=NOW)

    assert (tmp_path / f"{tenant.id}_exclude_refusers.csv").exists()
    assert (tmp_path / f"{tenant.id}_seed_delivered_buyers.csv").exists()
    assert result["exclude_refusers_count"] == 1
    assert result["seed_delivered_buyers_count"] == 1  # buyer 111 only; refuser 110 is not a buyer
    assert _read_rows(tmp_path / f"{tenant.id}_seed_delivered_buyers.csv")[0] == ["phone", "email", "value"]


def test_small_list_warning_appears(db_session, tenant, tmp_path):
    make_delivered(db_session, tenant, "SW1", H("+201000000120"))

    result = export_tenant_audiences(db_session, tenant.id, tmp_path, now=NOW)

    assert len(result["warnings"]) == 2  # both lists are tiny
    assert all("verify minimum size" in w for w in result["warnings"])


def test_no_warning_when_lists_reach_minimum(db_session, tenant, tmp_path):
    for i in range(MIN_LIST_ROWS):
        make_delivered(db_session, tenant, f"BIG{i}", H(f"+2010002{i:05d}"), value=10.0)

    result = export_tenant_audiences(db_session, tenant.id, tmp_path, now=NOW)

    assert result["seed_delivered_buyers_count"] == MIN_LIST_ROWS
    assert not any("seed_delivered_buyers" in w for w in result["warnings"])
    assert any("exclude_refusers" in w for w in result["warnings"])  # zero refusers is still small


def test_export_unknown_tenant_raises(db_session, tmp_path):
    with pytest.raises(ValueError):
        export_tenant_audiences(db_session, 9999, tmp_path, now=NOW)
