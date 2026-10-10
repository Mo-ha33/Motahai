#!/usr/bin/env python3
"""Set the Status field of issues on the Mo-ha33 Project v2 board (#3).

Stdlib only. Shells out to `gh api` (REST and GraphQL). GH_TOKEN must hold the
ADD_TO_PROJECT_PAT secret (classic PAT with repo + project scopes).

Environment:
  GH_TOKEN      token used by gh (required; empty means skip with a notice)
  REPO          owner/name of the repo that holds the issues
  ITEMS         "NUMBER=Status,..." e.g. "24=Done,26=In Progress,33=Todo"
  EVENT_NAME    GitHub event name; used when ITEMS is empty
  EVENT_ACTION  event action (closed / reopened) for issues events
  ISSUE_NUMBER  issue number for issues events

Exit codes: 0 ok or skipped, 1 failure (bad input, no matching Status option, gh error).
"""

import json
import os
import subprocess
import sys

OWNER = "Mo-ha33"
PROJECT_NUMBER = 3
STATUS_FIELD = "Status"

# Status names that mean the same thing (lowercase, whitespace-normalised).
STATUS_ALIASES = ({"review", "in review"},)

# Status applied when an issues event fires with no explicit ITEMS.
EVENT_STATUS = {"closed": "Done", "reopened": "Todo"}

PROJECT_QUERY = (
    'query{user(login:"Mo-ha33"){projectV2(number:3){id field(name:"Status")'
    "{... on ProjectV2SingleSelectField{id options{id name}}}}}}"
)

ADD_ITEM_MUTATION = (
    "mutation($project: ID!, $content: ID!) {"
    " addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } } }"
)

SET_STATUS_MUTATION = (
    "mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {"
    " updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,"
    " value: {singleSelectOptionId: $option}}) { projectV2Item { id } } }"
)


class StatusError(Exception):
    """Bad input, no matching Status option, or a failed gh call."""


def parse_items(spec):
    """Parse "24=Done,26=In Progress" into [(24, "Done"), (26, "In Progress")]."""
    pairs = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        number, sep, status = raw.partition("=")
        number = number.strip().lstrip("#")
        status = status.strip()
        if not sep or not number.isdigit() or not status:
            raise StatusError(f"bad item {raw!r}: expected NUMBER=Status")
        pairs.append((int(number), status))
    if not pairs:
        raise StatusError("no items given")
    return pairs


def resolve_requests(items_input, event_name, action, issue_number):
    """Explicit ITEMS win; otherwise an issues event maps its action to a Status."""
    if items_input.strip():
        return parse_items(items_input)
    if event_name == "issues" and action in EVENT_STATUS and str(issue_number).isdigit():
        return [(int(issue_number), EVENT_STATUS[action])]
    raise StatusError(f"nothing to do for event {event_name!r} action {action!r}")


def _norm(name):
    return " ".join(name.split()).lower()


def match_option(wanted, options):
    """Return the option matching `wanted` (case-insensitive, then alias groups), or None.

    `options` is a list of {"id": ..., "name": ...} dicts as returned by GraphQL.
    """
    target = _norm(wanted)
    for option in options:
        if _norm(option["name"]) == target:
            return option
    for group in STATUS_ALIASES:
        if target in group:
            for option in options:
                if _norm(option["name"]) in group:
                    return option
    return None


def _gh_json(args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise StatusError(f"gh {' '.join(args[:2])} failed: {detail}")
    return json.loads(proc.stdout)


def graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-f", f"{key}={value}"]
    data = _gh_json(args)
    if data.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in data["errors"])
        raise StatusError(f"graphql error: {messages}")
    return data["data"]


def load_status_field():
    """Return (project_id, status_field_id, options) for the Status single-select field."""
    user = graphql(PROJECT_QUERY).get("user") or {}
    project = user.get("projectV2")
    if not project:
        raise StatusError(f"project {OWNER}/{PROJECT_NUMBER} not found (check token scopes)")
    field = project.get("field")
    if not field or "options" not in field:
        raise StatusError(f'no single-select field "{STATUS_FIELD}" on project {OWNER}/{PROJECT_NUMBER}')
    return project["id"], field["id"], field["options"]


def main(env=os.environ):
    if not env.get("GH_TOKEN"):
        print("::notice::ADD_TO_PROJECT_PAT secret not set; skipping project status update.")
        return 0
    try:
        requests = resolve_requests(
            env.get("ITEMS", ""),
            env.get("EVENT_NAME", ""),
            env.get("EVENT_ACTION", ""),
            env.get("ISSUE_NUMBER", ""),
        )
        repo = env.get("REPO", "")
        if not repo:
            raise StatusError("REPO is not set")

        project_id, field_id, options = load_status_field()

        # Validate every item before touching the board, so a typo does not leave a partial update.
        plan = []
        for number, wanted in requests:
            option = match_option(wanted, options)
            if option is None:
                available = ", ".join(o["name"] for o in options)
                raise StatusError(f'#{number}: no Status option matches "{wanted}". Available: {available}')
            plan.append((number, option))

        for number, option in plan:
            node_id = _gh_json(["api", f"repos/{repo}/issues/{number}"])["node_id"]
            # addProjectV2ItemById returns the existing item when the issue is already on the board.
            item_id = graphql(ADD_ITEM_MUTATION, project=project_id, content=node_id)[
                "addProjectV2ItemById"
            ]["item"]["id"]
            graphql(
                SET_STATUS_MUTATION,
                project=project_id,
                item=item_id,
                field=field_id,
                option=option["id"],
            )
            print(f"#{number} -> {option['name']}")
    except StatusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
