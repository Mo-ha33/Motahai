"""Unit tests for .github/scripts/project_status.py: pure parsing and option matching, no network."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "project_status.py"
_spec = importlib.util.spec_from_file_location("project_status", _SCRIPT)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

OPTIONS = [
    {"id": "opt-todo", "name": "Todo"},
    {"id": "opt-prog", "name": "In Progress"},
    {"id": "opt-rev", "name": "In Review"},
    {"id": "opt-done", "name": "Done"},
]


def test_parse_items_workflow_example():
    assert ps.parse_items("24=Done,26=In Progress,33=Todo") == [
        (24, "Done"),
        (26, "In Progress"),
        (33, "Todo"),
    ]


def test_parse_items_tolerates_whitespace_hash_and_empty_segments():
    assert ps.parse_items(" #7 = Done , ,8=Todo,") == [(7, "Done"), (8, "Todo")]


@pytest.mark.parametrize("spec", ["24", "x=Done", "24=", "=Done", "", " , "])
def test_parse_items_rejects_bad_input(spec):
    with pytest.raises(ps.StatusError):
        ps.parse_items(spec)


def test_resolve_requests_prefers_explicit_items():
    assert ps.resolve_requests("5=Done", "issues", "closed", "9") == [(5, "Done")]


def test_resolve_requests_maps_issue_actions():
    assert ps.resolve_requests("", "issues", "closed", "64") == [(64, "Done")]
    assert ps.resolve_requests("", "issues", "reopened", "64") == [(64, "Todo")]


def test_resolve_requests_rejects_unmapped_event():
    with pytest.raises(ps.StatusError):
        ps.resolve_requests("", "issues", "labeled", "64")
    with pytest.raises(ps.StatusError):
        ps.resolve_requests("", "workflow_dispatch", "", "")


@pytest.mark.parametrize("wanted", ["Done", "done", "  DONE  "])
def test_match_option_case_insensitive(wanted):
    assert ps.match_option(wanted, OPTIONS)["id"] == "opt-done"


def test_match_option_multiword_case_insensitive():
    assert ps.match_option("in progress", OPTIONS)["id"] == "opt-prog"
    assert ps.match_option("IN   PROGRESS", OPTIONS)["id"] == "opt-prog"


def test_match_option_review_alias_matches_in_review():
    assert ps.match_option("Review", OPTIONS)["name"] == "In Review"
    assert ps.match_option("In Review", OPTIONS)["name"] == "In Review"


def test_match_option_review_alias_matches_plain_review_option():
    options = [{"id": "r", "name": "Review"}, {"id": "d", "name": "Done"}]
    assert ps.match_option("In Review", options)["id"] == "r"
    assert ps.match_option("review", options)["id"] == "r"


def test_match_option_returns_none_when_no_match():
    assert ps.match_option("Blocked", OPTIONS) is None
    assert ps.match_option("Review", [{"id": "d", "name": "Done"}]) is None
