"""
Shared fixtures: in-memory SQLite DB, Fernet key, tenants.

shadow_tenant / live_tenant use settlement_hours=0 so an eligible order is dispatched in-line (the pre-S2 "send at
once" behavior the older tests assert). tests/test_s2_engine.py builds default-settlement (12h) tenants itself.
"""

import pytest
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce.credentials import FERNET_KEY_ENV, generate_key, store_credential
from src.ameen_workforce.db import create_tenant, init_db, make_engine


@pytest.fixture(autouse=True)
def _isolate_audience_export_dir(tmp_path, monkeypatch):
    """Keep the weekly audience export out of the repo (default out/audiences) for every test."""
    monkeypatch.setenv("AUDIENCE_EXPORT_DIR", str(tmp_path / "audiences"))


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def fernet_key(monkeypatch):
    key = generate_key()
    monkeypatch.setenv(FERNET_KEY_ENV, key)
    return key


@pytest.fixture
def shadow_tenant(db_session):
    return create_tenant(db_session, name="Shadow Shop", platform="shopify", shop_domain="shadow.myshopify.com",
                         meta_dataset_id="111", settlement_hours=0)


@pytest.fixture
def live_tenant(db_session, fernet_key):
    tenant = create_tenant(db_session, name="Live Shop", platform="shopify", shop_domain="live.myshopify.com",
                           meta_dataset_id="222", mode="live", settlement_hours=0)
    store_credential(db_session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-123")
    return tenant
