"""
tests/test_pilot_preflight.py — scripts/pilot_preflight.py: read-only S2-7 pre-flight checks on a seeded SQLite file.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from scripts.pilot_preflight import main
from src.ameen_workforce.capi_service import CONFIRMED_EVENT_NAME, DELIVERED_EVENT_NAME, EVENT_TYPES, hash_sha256
from src.ameen_workforce.credentials import store_credential
from src.ameen_workforce.db import CapiEvent, Order, WebhookDelivery, create_tenant, init_db, make_engine
from src.ameen_workforce.db import set_meta_test_event_code
from src.ameen_workforce.webhook_routes import BOSTA_SECRET_KIND
from src.ameen_workforce.order_pipeline import META_CAPI_TOKEN_KIND

DOMAIN = "pilot.myshopify.com"
TOKEN = "EAAB-PREFLIGHT-SECRET-DO-NOT-PRINT"
SHOPIFY_SECRET = "shpss_PREFLIGHT_SECRET"


@pytest.fixture
def preflight_env(monkeypatch, fernet_key):
    monkeypatch.setenv("SHOPIFY_APP_SECRET", SHOPIFY_SECRET)
    monkeypatch.delenv("SALLA_WEBHOOK_SECRET", raising=False)
    return fernet_key


def _seed(db_url, *, with_token=True):
    engine = make_engine(db_url)
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    tenant = create_tenant(session, name="Pilot", platform="shopify", shop_domain=DOMAIN,
                           meta_dataset_id="111222333444555", settlement_hours=0)
    set_meta_test_event_code(session, tenant, "TEST12345")
    if with_token:
        store_credential(session, tenant.id, META_CAPI_TOKEN_KIND, TOKEN)
    store_credential(session, tenant.id, BOSTA_SECRET_KIND, "bosta-secret-value")
    placed = datetime.now(timezone.utc) - timedelta(hours=2)
    phone_hash = hash_sha256("201001234567")
    order = Order(tenant_id=tenant.id, platform_order_id="1001", is_cod=True, value=850.0, currency="EGP",
                  current_status="paid", created_at_platform=placed, phone_hash=phone_hash)
    session.add(order)
    session.commit()
    for name in (CONFIRMED_EVENT_NAME, DELIVERED_EVENT_NAME):
        session.add(CapiEvent(tenant_id=tenant.id, order_id=order.id, event_name=name,
                              event_id=EVENT_TYPES[name].event_id(order.id), status="shadow", quality_flags=None))
    session.add(WebhookDelivery(tenant_id=tenant.id, platform="shopify", topic="orders/create",
                                dedupe_key="shopify:wh-1", delivery_id="wh-1", signature_ok=True,
                                processed_ok=True, payload_sha256="0" * 64))
    session.commit()
    session.close()
    engine.dispose()


@pytest.fixture
def db_file(tmp_path):
    return f"sqlite:///{tmp_path / 'pilot.db'}"


def _file_digest(url):
    return hashlib.sha256(open(url[len("sqlite:///"):], "rb").read()).hexdigest()


def test_all_pass_tenant_exits_zero(db_file, preflight_env, capsys):
    _seed(db_file)
    before = _file_digest(db_file)

    code = main(["--tenant", DOMAIN, "--db-url", db_file])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "FAIL  " not in out
    assert "PASS  1.1 meta CAPI token" in out
    assert "PASS  1.1 phone normalization" in out
    assert "MANUAL  1.2 test events sent" in out
    assert "SUMMARY tenant=pilot.myshopify.com" in out and "result=NO_BLOCKERS" in out
    assert TOKEN not in out  # the token value is never printed
    assert _file_digest(db_file) == before  # read-only: the database file is untouched


def test_missing_token_exits_one_with_fail_line(db_file, preflight_env, capsys):
    _seed(db_file, with_token=False)

    code = main(["--tenant", DOMAIN, "--db-url", db_file])
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  1.1 meta CAPI token  no meta_capi_token credential stored" in out
    assert "result=BLOCKED" in out


def test_unknown_tenant_exits_one(db_file, preflight_env, capsys):
    _seed(db_file)

    code = main(["--tenant", "nobody.myshopify.com", "--db-url", db_file])
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  1.0 tenant exists  no tenant with shop_domain 'nobody.myshopify.com'" in out
    assert "MANUAL  1.2 test events sent" in out


def test_missing_database_is_not_created(tmp_path, preflight_env, capsys):
    path = tmp_path / "absent.db"

    code = main(["--tenant", DOMAIN, "--db-url", f"sqlite:///{path}"])
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  1.0 database" in out
    assert not path.exists()


def test_bad_phone_hash_is_blocking(db_file, preflight_env, capsys):
    _seed(db_file)
    engine = make_engine(db_file)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.query(Order).update({"phone_hash": "not-a-sha256"})
    session.commit()
    session.close()
    engine.dispose()

    code = main(["--tenant", DOMAIN, "--db-url", db_file])
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  1.1 order phone hashes  1 of 1 are not 64-char lowercase hex" in out
