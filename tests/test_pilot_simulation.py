"""
test_pilot_simulation.py: scripts/run_pilot_simulation.py runs end to end, makes no Meta call, and produces the expected
D-005 outcome for each simulated order.
"""

import importlib.util
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from src.ameen_workforce.credentials import FERNET_KEY_ENV

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_pilot_simulation.py"
PLACED = {
    "SIM-1001": datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
    "SIM-1002": datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
    "SIM-1003": datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc),
    "SIM-1004": datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
}


def _load_script():
    spec = importlib.util.spec_from_file_location("run_pilot_simulation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sim():
    return _load_script()


@pytest.fixture(scope="module")
def result(sim):
    out = io.StringIO()
    res = sim.run_simulation(out=out)
    res["_text"] = out.getvalue()
    return res


def _epoch(dt):
    return int(dt.timestamp())


# --- expected ladder per scenario -----------------------------------------------------------------------------------

def test_sim_1001_cod_confirmed_by_tag_then_delivered(result):
    order = result["orders"]["SIM-1001"]
    assert order["order_status"] == "delivered"
    assert order["confirmed"]["status"] == "shadow"
    assert order["confirmed"]["source"] == "tag"
    assert order["confirmed"]["event_time"] == _epoch(PLACED["SIM-1001"] + timedelta(hours=1))
    assert order["confirmed"]["value"] == 1250.0
    assert order["delivered"]["status"] == "shadow"
    # event_time of DeliveredPurchase is the PLACED time, not the delivery or send time (S2-1)
    assert order["delivered"]["event_time"] == _epoch(PLACED["SIM-1001"])
    assert order["delivered"]["event_id"] == "delivered_SIM-1001"
    assert order["delivered"]["quality_flags"] is None  # user agent was captured
    actions = [s.get("action") for s in order["steps"] if s["kind"] == "webhook"]
    assert actions == ["SHADOW", "SCHEDULED"]


def test_sim_1001_delivered_waits_for_settlement_window(result):
    ticks = [s for s in result["orders"]["SIM-1001"]["steps"] if s["kind"] == "tick"]
    assert ticks[0]["counts"]["due"] == 0  # 6h after delivery: still inside the 12h window
    assert ticks[1]["counts"]["due"] == 1 and ticks[1]["counts"]["shadow"] == 1


def test_sim_1002_shipped_then_refused_has_no_delivered_purchase(result):
    order = result["orders"]["SIM-1002"]
    assert order["order_status"] == "cancelled"
    assert order["confirmed"]["status"] == "shadow"
    assert order["confirmed"]["source"] == "implicit_shipped"
    assert order["delivered"] is None
    actions = [s.get("action") for s in order["steps"] if s["kind"] == "webhook"]
    assert actions == ["SHADOW", "SUPPRESSED"]


def test_sim_1003_prepaid_is_confirmed_and_delivered_with_missing_user_agent_flag(result):
    order = result["orders"]["SIM-1003"]
    assert order["order_status"] == "paid"
    assert order["confirmed"]["status"] == "shadow"
    assert order["confirmed"]["source"] == "implicit_paid"
    assert order["delivered"]["status"] == "shadow"
    assert order["delivered"]["event_time"] == _epoch(PLACED["SIM-1003"])
    assert order["delivered"]["value"] == 1250.0
    assert order["delivered"]["quality_flags"] == "missing_user_agent"
    assert order["confirmed"]["quality_flags"] == "missing_user_agent"


def test_sim_1004_delivered_after_cutoff_is_late_delivery_and_never_sent(result):
    order = result["orders"]["SIM-1004"]
    assert order["confirmed"]["status"] == "shadow"
    assert order["delivered"]["status"] == "late_delivery"
    assert order["delivered"]["error_type"] == "past_cutoff"
    assert order["delivered"]["due_at"] is None
    assert order["delivered"]["event_time"] == _epoch(PLACED["SIM-1004"])
    actions = [s.get("action") for s in order["steps"] if s["kind"] == "webhook"]
    assert actions == ["SHADOW", "LATE_DELIVERY"]


# --- no Meta call, labelled output, exit code ----------------------------------------------------------------------

def test_shadow_tenant_never_calls_the_sender(result):
    assert result["tenant_mode"] == "shadow"
    assert result["recording_sender_calls"] == 0


def test_no_http_request_is_made(sim, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the pilot simulation must not make HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    res = sim.run_simulation(out=io.StringIO())
    assert res["recording_sender_calls"] == 0


def test_output_is_labelled_as_simulation(result):
    text = result["_text"]
    assert "SIMULATION ONLY" in text
    assert "Native Purchase" in text and "does NOT send it" in text
    for code in PLACED:
        assert f"[{code}]" in text


def test_fernet_key_is_restored_after_run(sim, monkeypatch):
    monkeypatch.delenv(FERNET_KEY_ENV, raising=False)
    sim.run_simulation(out=io.StringIO())
    assert FERNET_KEY_ENV not in os.environ


def test_main_returns_zero(sim, capsys):
    assert sim.main() == 0
    assert "SIMULATION ONLY" in capsys.readouterr().out


def test_script_exits_zero_as_a_process():
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT), capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    assert "Meta calls made: 0" in proc.stdout
