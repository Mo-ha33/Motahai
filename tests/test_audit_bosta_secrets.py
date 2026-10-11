"""
test_audit_bosta_secrets.py — the pre-deploy Bosta secret audit (scripts/audit_bosta_secrets.py).
"""

import pytest
from sqlalchemy.orm import sessionmaker

from scripts import audit_bosta_secrets as audit_script
from src.ameen_workforce.credentials import get_credential, store_credential
from src.ameen_workforce.db import create_tenant
from src.ameen_workforce.webhook_signatures import BOSTA_MIN_SECRET_LENGTH

WEAK = "short-secret"
STRONG = "s" * BOSTA_MIN_SECRET_LENGTH


@pytest.fixture
def tenants(db_session, fernet_key, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(audit_script, "get_session_factory", lambda: factory)
    weak = create_tenant(db_session, name="Weak", platform="shopify", shop_domain="weak.myshopify.com",
                         meta_dataset_id="1")
    strong = create_tenant(db_session, name="Strong", platform="shopify", shop_domain="strong.myshopify.com",
                           meta_dataset_id="2")
    create_tenant(db_session, name="No Bosta", platform="salla", shop_domain="999", meta_dataset_id="3")
    store_credential(db_session, weak.id, "bosta_webhook_secret", WEAK)
    store_credential(db_session, strong.id, "bosta_webhook_secret", STRONG)
    return weak, strong


def test_report_flags_weak_secrets_without_printing_them(tenants, capsys):
    weak, strong = tenants
    assert audit_script.main([]) == 1
    out = capsys.readouterr().out
    assert WEAK not in out and STRONG not in out
    assert f"tenant {weak.id:>5}" in out and "WEAK" in out and f"length={len(WEAK)}" in out
    assert "OK" in out and "1 tenant(s) need a new Bosta secret" in out
    assert "999" not in out  # tenants without a Bosta secret are not listed


def test_report_passes_when_nothing_is_weak(db_session, fernet_key, monkeypatch, capsys):
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(audit_script, "get_session_factory", lambda: factory)
    assert audit_script.main([]) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_rotate_replaces_only_the_bosta_secret(tenants, db_session, capsys):
    weak, _ = tenants
    store_credential(db_session, weak.id, "oto_webhook_secret", "oto-unchanged-secret-value")
    assert audit_script.main(["--rotate", str(weak.id)]) == 0
    new_secret = get_credential(db_session, weak.id, "bosta_webhook_secret")
    assert new_secret != WEAK and len(new_secret) >= 43
    assert capsys.readouterr().out.count(new_secret) == 1
    assert get_credential(db_session, weak.id, "oto_webhook_secret") == "oto-unchanged-secret-value"
    assert audit_script.main([]) == 0  # now passes
