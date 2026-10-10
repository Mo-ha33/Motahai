"""Unit tests for .github/scripts/project_status.py: parsing, option matching, annotations and
the per-item run loop. No network: gh calls are replaced by a fake."""

import importlib.util
import subprocess
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


# --- annotations -----------------------------------------------------------------------------


def test_escape_annotation_follows_actions_rules():
    assert ps.escape_annotation("50% done\r\nnext") == "50%25 done%0D%0Anext"


def test_report_error_prints_titled_annotation(capsys):
    ps.report_error("boom\nline2")
    assert capsys.readouterr().out == "::error title=project-status::boom%0Aline2\n"


def test_report_item_error_format(capsys):
    ps.report_item_error(7, "gh api failed: 100%")
    assert capsys.readouterr().out == "::error::#7: gh api failed: 100%25\n"


def test_gh_failure_includes_stderr_truncated_to_500(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="E" * 900)

    monkeypatch.setattr(ps.subprocess, "run", fake_run)
    with pytest.raises(ps.StatusError) as info:
        ps._gh_json(["api", "repos/x/issues/1"])
    message = str(info.value)
    assert message.startswith("gh api repos/x/issues/1 failed: ")
    assert "E" * 500 in message
    assert "E" * 501 not in message


def test_gh_failure_falls_back_to_stdout_when_stderr_empty(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="from stdout\n", stderr="")

    monkeypatch.setattr(ps.subprocess, "run", fake_run)
    with pytest.raises(ps.StatusError, match="from stdout"):
        ps._gh_json(["api", "graphql"])


# --- main(): fake gh -------------------------------------------------------------------------


class FakeGh:
    """Stands in for ps._gh_json and answers the three gh calls the script makes."""

    def __init__(self, fail_lookup=(), fail_add=(), fail_set=(), no_project=False):
        self.fail_lookup = {str(n) for n in fail_lookup}
        self.fail_add = {str(n) for n in fail_add}
        self.fail_set = {str(n) for n in fail_set}
        self.no_project = no_project
        self.calls = []
        self.set_items = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["api", "graphql"]:
            variables = {}
            for flag, pair in zip(args[2::2], args[3::2]):
                if flag == "-f":
                    key, _, value = pair.partition("=")
                    variables[key] = value
            return self._graphql(variables.pop("query"), variables)
        number = args[1].rsplit("/", 1)[-1]
        if number in self.fail_lookup:
            raise ps.StatusError("gh api failed: Not Found")
        return {"node_id": f"node-{number}"}

    def _graphql(self, query, variables):
        if "projectV2(number" in query:
            if self.no_project:
                return {"data": {"user": {"projectV2": None}}}
            field = {"id": "field", "options": OPTIONS}
            return {"data": {"user": {"projectV2": {"id": "proj", "field": field}}}}
        if "addProjectV2ItemById" in query:
            number = variables["content"].rsplit("-", 1)[-1]
            if number in self.fail_add:
                return {"errors": [{"message": "Could not resolve to a node"}]}
            return {"data": {"addProjectV2ItemById": {"item": {"id": f"item-{number}"}}}}
        item = variables["item"]
        if item.rsplit("-", 1)[-1] in self.fail_set:
            return {"errors": [{"message": "boom"}]}
        self.set_items.append(item)
        return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item}}}}

    def wrote_anything(self):
        return any("mutation" in " ".join(call) for call in self.calls)


def _run(monkeypatch, fake, items):
    monkeypatch.setattr(ps, "_gh_json", fake)
    return ps.main({"GH_TOKEN": "t", "REPO": "Mo-ha33/Motahai", "ITEMS": items})


def test_main_all_items_succeed(monkeypatch, capsys):
    fake = FakeGh()
    assert _run(monkeypatch, fake, "1=Done,2=In Progress") == 0
    out = capsys.readouterr().out
    assert "::notice::#1 -> Done\n" in out
    assert "::notice::#2 -> In Progress\n" in out
    assert "::error" not in out
    assert fake.set_items == ["item-1", "item-2"]


def test_main_validation_failure_writes_nothing(monkeypatch, capsys):
    fake = FakeGh()
    assert _run(monkeypatch, fake, "1=Done,2=Bogus") == 1
    out = capsys.readouterr().out
    assert "::error title=project-status::#2: no Status option matches" in out
    assert not fake.wrote_anything()


def test_main_lookup_failure_continues_with_remaining_items(monkeypatch, capsys):
    fake = FakeGh(fail_lookup={2})
    assert _run(monkeypatch, fake, "1=Done,2=Todo,3=In Progress") == 1
    out = capsys.readouterr().out
    assert "::error::#2: gh api failed: Not Found\n" in out
    assert "::notice::#1 -> Done\n" in out
    assert "::notice::#3 -> In Progress\n" in out
    assert "::notice::#2" not in out
    assert fake.set_items == ["item-1", "item-3"]


def test_main_add_item_failure_is_per_item(monkeypatch, capsys):
    fake = FakeGh(fail_add={1})
    assert _run(monkeypatch, fake, "1=Done,2=Done") == 1
    out = capsys.readouterr().out
    assert "::error::#1: graphql error: Could not resolve to a node\n" in out
    assert "::notice::#2 -> Done\n" in out
    assert fake.set_items == ["item-2"]


def test_main_set_status_failure_is_per_item(monkeypatch, capsys):
    fake = FakeGh(fail_set={3})
    assert _run(monkeypatch, fake, "1=Done,3=Todo") == 1
    out = capsys.readouterr().out
    assert "::error::#3: graphql error: boom\n" in out
    assert "::notice::#1 -> Done\n" in out
    assert fake.set_items == ["item-1"]


def test_main_unexpected_response_is_per_item(monkeypatch, capsys):
    class Odd(FakeGh):
        def __call__(self, args):
            if args[:2] != ["api", "graphql"] and args[1].endswith("/5"):
                return {"no_node_id": True}
            return super().__call__(args)

    assert _run(monkeypatch, Odd(), "5=Done,6=Done") == 1
    out = capsys.readouterr().out
    assert "::error::#5: unexpected gh response: KeyError" in out
    assert "::notice::#6 -> Done\n" in out


def test_main_project_lookup_failure_is_fatal(monkeypatch, capsys):
    fake = FakeGh(no_project=True)
    assert _run(monkeypatch, fake, "1=Done") == 1
    out = capsys.readouterr().out
    assert "::error title=project-status::project Mo-ha33/3 not found" in out
    assert not fake.wrote_anything()


def test_main_skips_without_token(monkeypatch, capsys):
    fake = FakeGh()
    monkeypatch.setattr(ps, "_gh_json", fake)
    assert ps.main({"ITEMS": "1=Done"}) == 0
    assert "::notice::ADD_TO_PROJECT_PAT secret not set" in capsys.readouterr().out
    assert fake.calls == []
