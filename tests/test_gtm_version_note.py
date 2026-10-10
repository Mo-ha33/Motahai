"""
test_gtm_version_note.py — issue #47: the GTM container-version note must state only what the code verifies.

The note is written to the client's GTM version history on every live publish. It must not claim autonomy or
Zero-PII / Consent Mode verification (nothing in the code checks those), and it must say that a human approved.
Reuses the consultation fixtures and the D-003 token issuer from test_hitl_tokens.py (no network is touched).
"""

import json
from pathlib import Path

from test_hitl_tokens import (  # noqa: F401  (pytest fixtures and helpers)
    CONTAINER,
    WORKSPACE,
    call,
    consultation,
    env,
    fake_google,
    tools,
)
from src.ameen_workforce import hitl_tokens

CONSULTATION_SOURCE = Path(__file__).resolve().parents[1] / "ops" / "hermes" / "consultation.py"


def test_note_states_human_approval_and_not_verified_claims(consultation):
    note = consultation.build_version_note()
    assert "human operator approved" in note
    assert "Not independently verified for PII or Consent Mode" in note
    assert len(note) <= 200


def test_note_makes_no_autonomy_or_verification_claims(consultation):
    for approved in (True, False):
        note = consultation.build_version_note(approved_by_token=approved)
        assert "autonomous" not in note.lower()
        assert "verified by Tariq" not in note
        assert "Zero-PII" not in note
        assert "enforced" not in note.lower()


def test_note_without_approval_says_so(consultation):
    note = consultation.build_version_note(approved_by_token=False)
    assert "human operator approved" not in note
    assert "No human approval token was recorded" in note


def test_note_contains_no_token_or_secret_material(consultation, env):
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    payload, _, signature = token.partition(".")
    for note in (consultation.build_version_note(), consultation.build_version_note(approved_by_token=False)):
        assert token not in note
        assert payload not in note and signature not in note
        assert "/home/" not in note and "@" not in note


def test_old_autonomous_note_is_gone_from_source():
    source = CONSULTATION_SOURCE.read_text(encoding="utf-8")
    assert "Autonomously deployed" not in source
    assert "with Zero-PII and Consent Mode v2" not in source


def test_publish_path_writes_the_honest_note(env, tools, fake_google, consultation, monkeypatch):
    bodies = []
    original = fake_google.create_version

    def capture(path, body):
        bodies.append(body)
        return original(path, body)

    monkeypatch.setattr(fake_google, "create_version", capture)
    token = hitl_tokens.issue_publish_token(CONTAINER, WORKSPACE)
    out = json.loads(call(tools, "hermes_gtm_cloud_publish", token=token))
    assert out["status"] == "success"
    assert bodies == [{"name": "v-test", "notes": consultation.build_version_note(approved_by_token=True)}]
    assert "human operator approved" in bodies[0]["notes"]
