"""Tests for gtm-container-linter. Run: python -m pytest tests/ -q"""

import json
import sys
from pathlib import Path


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import lint_container as lc  # noqa: E402


FIXTURE = HERE / "fixtures" / "sample_container.json"


def _report():
    cv = lc.load_container(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return cv, lc.build_report(cv, lc.Linter(cv).run(), max_findings=500)


def _rules(report):
    return {(f["rule"], f["entity_name"]) for f in report["findings"]}


def test_detects_planted_defects():
    _, r = _report()
    rules = {f["rule"] for f in r["findings"]}
    for expected in (
        "security.meta_token_in_html", "ga4.purchase_no_transaction_id", "google.duplicate_google_tag",
        "meta.pixel_no_event_id", "tag.exact_duplicate", "tag.missing_trigger",
        "variable.undefined_reference", "tag.universal_analytics", "tag.no_firing_trigger",
        "trigger.unused", "variable.unused", "consent.no_default_detected", "meta.duplicate_pixel_init",
    ):
        assert expected in rules, expected


def test_no_false_positives_on_known_good_entities():
    _, r = _report()
    rules = _rules(r)
    assert ("tag.no_firing_trigger", "Setup helper") not in rules  # used via setupTag
    assert ("variable.unused", "DLV - value") not in rules  # referenced inside Custom HTML
    assert not any(f["rule"] == "variable.undefined_reference" and f["evidence"].get("variable") == "_event"
                   for f in r["findings"])
    assert not any(f["rule"] == "tag.missing_trigger" and f["evidence"].get("trigger_id") == "2147479553"
                   for f in r["findings"])


def test_token_is_redacted():
    _, r = _report()
    blob = json.dumps(r)
    assert "EAABsbCS1iHgBAOZBxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in blob
    assert "[REDACTED]" in blob


def test_fingerprint_ignores_names_and_ids_order():
    cv, _ = _report()
    fp1 = lc.fingerprint(cv)
    shuffled = json.loads(json.dumps(cv))
    shuffled["tag"].reverse()
    shuffled["fingerprint"] = "999"
    assert lc.fingerprint(shuffled) == fp1
    shuffled["tag"][0]["parameter"].append({"type": "template", "key": "new", "value": "1"})
    assert lc.fingerprint(shuffled) != fp1


def test_api_shape_and_exit_codes(tmp_path, capsys):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["containerVersion"]
    p = tmp_path / "api.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert lc.main([str(p), "--fail-on", "critical"]) == 1
    assert lc.main([str(p), "--fail-on", "never"]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert lc.main([str(bad)]) == 2
    capsys.readouterr()


def test_max_findings_truncates():
    cv, _ = _report()
    r = lc.build_report(cv, lc.Linter(cv).run(), max_findings=3)
    assert len(r["findings"]) == 3 and r["findings_truncated"] is True
    assert r["findings_total"] > 3


CLEAN = HERE / "fixtures" / "clean_container.json"


def test_clean_container_scores_100_and_single_mutation_is_caught():
    cv = lc.load_container(json.loads(CLEAN.read_text(encoding="utf-8")))
    r = lc.build_report(cv, lc.Linter(cv).run(), max_findings=500)
    assert r["findings_total"] == 0 and r["health_score"] == 100

    # Live-demo mutation: drop eventID from the Meta Purchase tag → exactly one new finding.
    mutated = json.loads(json.dumps(cv))
    tag = next(t for t in mutated["tag"] if t["name"] == "Meta Pixel - Purchase")
    tag["parameter"][0]["value"] = tag["parameter"][0]["value"].replace(",{eventID:{{DLV - event_id}}}", "")
    r2 = lc.build_report(mutated, lc.Linter(mutated).run(), max_findings=500)
    assert [f["rule"] for f in r2["findings"]] == ["meta.pixel_no_event_id", "variable.unused"]
    assert r2["config_fingerprint"] != r["config_fingerprint"]
