"""Tests for the MCP gateway policy guard. Run: python -m pytest tests -q"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from policy_guard import PolicyError, PolicyGuard  # noqa: E402


TOKENS = {"MCP_TOK_ARCHITECT": "arch-token", "MCP_TOK_AUDITOR": "aud-token", "MCP_TOK_HUMAN": "human-token"}


@pytest.fixture
def guard(monkeypatch, tmp_path):
    for env, tok in TOKENS.items():
        monkeypatch.setenv(env, hashlib.sha256(tok.encode()).hexdigest())
    pol = yaml.safe_load((HERE.parent / "tool-policy.yaml").read_text(encoding="utf-8"))
    pol["defaults"]["audit_log"] = str(tmp_path / "audit.jsonl")
    pol["tools"]["hermes_consult"]["timeout_s"] = 0.2
    g = PolicyGuard(pol)
    g.tmp = tmp_path
    return g


def deny(fn, status):
    with pytest.raises(PolicyError) as e:
        fn()
    assert e.value.status == status, e.value.message


def test_authentication(guard):
    deny(lambda: guard.authenticate(None, True, 10), 401)
    deny(lambda: guard.authenticate("Bearer nope", True, 10), 401)
    deny(lambda: guard.authenticate("Bearer arch-token", True, 10**6), 413)
    deny(lambda: guard.authenticate("Bearer human-token", True, 10), 403)   # operator role via public edge
    assert guard.authenticate("Bearer human-token", False, 10).name == "operator-human"


def test_role_allowlists_and_tools_list(guard):
    arch = guard.authenticate("Bearer arch-token", True, 10)
    aud = guard.authenticate("Bearer aud-token", True, 10)
    for tool in ("hermes_terminal_exec", "hermes_run_task", "hermes_browser_navigate"):
        deny(lambda t=tool: guard.authorize(arch, t, {}), 403)
    for tool in ("hermes_terminal_exec", "hermes_skill_create", "hermes_memory_update"):
        deny(lambda t=tool: guard.authorize(aud, t, {}), 403)
    names = [t["name"] for t in guard.filter_tools(arch, [{"name": "hermes_consult"}, {"name": "hermes_terminal_exec"}])]
    assert names == ["hermes_consult"]


@pytest.mark.parametrize("url,status", [
    ("http://example.com", 400), ("https://localhost/", 400), ("https://127.0.0.1:58763/", 403),
    ("https://169.254.169.254/latest", 403), ("https://10.0.0.5/", 403), ("https://172.17.0.1:58775/", 403),
])
def test_ssrf(guard, url, status):
    aud = guard.authenticate("Bearer aud-token", True, 10)
    deny(lambda: guard.authorize(aud, "hermes_browser_navigate", {"url": url}), status)


def test_skill_allowlists_for_live_schemas(guard):
    aud = guard.authenticate("Bearer aud-token", True, 10)
    guard.authorize(aud, "hermes_skill_execute", {"skill": "gtm-container-linter"})
    deny(lambda: guard.authorize(aud, "hermes_skill_execute", {"skill": "rm-rf"}), 403)
    guard.authorize(aud, "hermes_run_task", {"task": "audit", "skills": ["gtm-container-linter"]})
    guard.authorize(aud, "hermes_run_task", {"task": "no skills preloaded"})
    deny(lambda: guard.authorize(aud, "hermes_run_task", {"task": "x", "skills": ["gtm-container-linter", "evil"]}), 403)


def test_cost_arguments_are_pinned(guard):
    arch = guard.authenticate("Bearer arch-token", True, 10)
    guard.authorize(arch, "hermes_consult", {"prompt": "q", "model": "gemini-flash-latest", "reasoning_effort": "low"})
    deny(lambda: guard.authorize(arch, "hermes_consult", {"prompt": "q", "reasoning_effort": "max"}), 403)
    deny(lambda: guard.authorize(arch, "hermes_consult", {"prompt": "q", "model": "some-expensive-model"}), 403)


def test_no_unknown_arguments_injected(guard):
    arch = guard.authenticate("Bearer arch-token", True, 10)
    args = {"prompt": "q"}
    guard.authorize(arch, "hermes_consult", args)
    assert args == {"prompt": "q"}   # live schemas have no max_output_tokens


def test_rate_limit_timeout_budget_and_audit(guard):
    aud = guard.authenticate("Bearer aud-token", True, 10)
    for _ in range(4):
        guard.authorize(aud, "hermes_run_task", {"task": "t"})
    deny(lambda: guard.authorize(aud, "hermes_run_task", {"task": "t"}), 429)

    async def slow():
        await asyncio.sleep(1)

    async def fast():
        return "done"

    with pytest.raises(PolicyError) as e:
        asyncio.run(guard.run(aud, "hermes_consult", {}, slow()))
    assert e.value.status == 504
    assert asyncio.run(guard.run(aud, "hermes_consult", {}, fast())) == "done"

    guard.record_tokens(aud, 10**7)
    deny(lambda: guard.authorize(aud, "hermes_consult", {"prompt": "q"}), 429)
    last = json.loads((guard.tmp / "audit.jsonl").read_text().splitlines()[-1])
    assert last["status"] == "denied:budget" and "prompt" not in json.dumps(last)
