"""Shared fixtures: in-memory SQLite DB, Fernet key, tenants."""

import pytest
from sqlalchemy.orm import sessionmaker

from src.ameen_workforce.credentials import FERNET_KEY_ENV, generate_key, store_credential
from src.ameen_workforce.db import create_tenant, init_db, make_engine


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
                         meta_dataset_id="111")


@pytest.fixture
def live_tenant(db_session, fernet_key):
    tenant = create_tenant(db_session, name="Live Shop", platform="shopify", shop_domain="live.myshopify.com",
                           meta_dataset_id="222", mode="live")
    store_credential(db_session, tenant.id, "meta_capi_token", "SECRET-META-TOKEN-123")
    return tenant
