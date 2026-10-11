"""
test_gtm_api.py - headless GTM provisioning tools (ops/hermes/gtm_api.py + their wrappers in consultation.py).

No network: a fake Tag Manager `service` object stands in for googleapiclient, and the Google auth/discovery
entry points are patched. Covers the error classifier, hermes_gtm_verify_access, hermes_gtm_workspace_apply
(idempotent upsert, trigger-name resolution, linter gate) and the hermes_gtm_cloud_publish fixes (scopes,
compiler/merge-error refusal).
"""

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.ameen_workforce import hitl_tokens

ROOT = Path(__file__).resolve().parents[1]
SA_EMAIL = "tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com"
PUBLIC = "GTM-ABC1234"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gtm = _load("hermes_gtm_api_under_test", ROOT / "ops" / "hermes" / "gtm_api.py")


# ---------------------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------------------
class FakeHttpError(Exception):
    """Same attributes as googleapiclient.errors.HttpError (resp.status, content)."""

    def __init__(self, status, message="boom", reason="", content=None):
        super().__init__(f"<HttpError {status}: {message}>")
        self.resp = types.SimpleNamespace(status=status)
        self.content = (content if content is not None else json.dumps(
            {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}})).encode()


class _Req:
    def __init__(self, fn, tm=None, name=""):
        self.fn, self.tm, self.name = fn, tm, name

    def execute(self, num_retries=0):
        if self.tm is not None:
            self.tm.retries.append((self.name, num_retries))
        out = self.fn()
        if isinstance(out, Exception):
            raise out
        return out


class FakeTagManager:
    """In-memory Tag Manager v2: accounts -> containers -> workspaces -> tags/triggers/variables, with paging."""

    def __init__(self, accounts=None, page_size=100):
        # accounts: [{"accountId": "1", "containers": [{"containerId","publicId","name","usageContext","workspaces":[...]}]}]
        self.accounts_data = accounts if accounts is not None else []
        self.page_size = page_size
        self.writes = []            # (verb, kind, name_or_path)
        self.calls = []             # every API method touched
        self.retries = []           # (API method, num_retries passed to execute())
        self.errors = {}            # "tags.create" / "accounts.list" / "create_version" / "publish" -> Exception
        self.store = {}             # ws_path -> {"tag": [...], "trigger": [...], "variable": [...], "builtInVariable": [...]}
        self.create_version_response = None
        self.published = []
        self.workspaces_created = []
        self.statuses = {}          # ws_path -> getStatus response
        self._seq = 100

    # -- helpers
    def ws(self, path):
        return self.store.setdefault(path, {"tag": [], "trigger": [], "variable": [], "builtInVariable": []})

    def _page(self, items, key, token):
        start = int(token) if token else 0
        chunk = items[start:start + self.page_size]
        res = {key: chunk}
        if start + self.page_size < len(items):
            res["nextPageToken"] = str(start + self.page_size)
        return res

    def _req(self, name, fn):
        self.calls.append(name)
        err = self.errors.get(name)
        return _Req(lambda: err if err else fn(), self, name)

    # -- API surface
    def accounts(self):
        return _Accounts(self)


class _Accounts:
    def __init__(self, tm):
        self.tm = tm

    def list(self, pageToken=None):
        tm = self.tm
        return tm._req("accounts.list", lambda: tm._page(
            [{"accountId": a["accountId"], "name": a.get("name", "")} for a in tm.accounts_data], "account", pageToken))

    def containers(self):
        return _Containers(self.tm)


class _Containers:
    def __init__(self, tm):
        self.tm = tm

    def list(self, parent, pageToken=None):
        tm = self.tm
        acc = next(a for a in tm.accounts_data if f"accounts/{a['accountId']}" == parent)
        items = [{k: v for k, v in c.items() if k != "workspaces"} | {"accountId": acc["accountId"]} for c in acc["containers"]]
        return tm._req("containers.list", lambda: tm._page(items, "container", pageToken))

    def workspaces(self):
        return _Workspaces(self.tm)

    def versions(self):
        return _Versions(self.tm)


class _Versions:
    def __init__(self, tm):
        self.tm = tm

    def publish(self, path):
        tm = self.tm

        def run():
            tm.published.append(path)
            return {"containerVersion": {"containerVersionId": "9", "name": "v-test", "path": path}}
        return tm._req("publish", run)


class _Workspaces:
    def __init__(self, tm):
        self.tm = tm

    def list(self, parent, pageToken=None):
        tm = self.tm
        for acc in tm.accounts_data:
            for c in acc["containers"]:
                if parent == f"accounts/{acc['accountId']}/containers/{c['containerId']}":
                    return tm._req("workspaces.list", lambda c=c: tm._page(c["workspaces"], "workspace", pageToken))
        raise AssertionError(parent)

    def create(self, parent, body):
        tm = self.tm

        def run():
            for acc in tm.accounts_data:
                for c in acc["containers"]:
                    if parent == f"accounts/{acc['accountId']}/containers/{c['containerId']}":
                        new_id = str(max(int(w["workspaceId"]) for w in c["workspaces"]) + 1)
                        ws = {"workspaceId": new_id, "name": body["name"]}
                        c["workspaces"].append(ws)
                        tm.workspaces_created.append(body["name"])
                        return dict(ws)
            raise AssertionError(parent)
        return tm._req("workspaces.create", run)

    def getStatus(self, path):
        tm = self.tm
        return tm._req("workspaces.getStatus", lambda: tm.statuses.get(path, {}))

    def tags(self):
        return _Collection(self.tm, "tag", "tags", "tagId")

    def triggers(self):
        return _Collection(self.tm, "trigger", "triggers", "triggerId")

    def variables(self):
        return _Collection(self.tm, "variable", "variables", "variableId")

    def built_in_variables(self):
        tm = self.tm
        return types.SimpleNamespace(list=lambda parent, pageToken=None: tm._req(
            "built_in_variables.list", lambda: tm._page(tm.ws(parent)["builtInVariable"], "builtInVariable", pageToken)))

    def create_version(self, path, body):
        tm = self.tm
        tm.calls_kwargs = {"path": path, "body": body}
        return tm._req("create_version", lambda: tm.create_version_response if tm.create_version_response is not None
                       else {"containerVersion": {"path": f"{path}/versions/9"}})


class _Collection:
    def __init__(self, tm, key, method, id_field):
        self.tm, self.key, self.method, self.id_field = tm, key, method, id_field

    def list(self, parent, pageToken=None):
        tm = self.tm
        return tm._req(f"{self.method}.list", lambda: tm._page(tm.ws(parent)[self.key], self.key, pageToken))

    def create(self, parent, body):
        tm = self.tm

        def run():
            tm._seq += 1
            entity = dict(body, **{self.id_field: str(tm._seq), "fingerprint": "fp-new",
                                   "path": f"{parent}/{self.method}/{tm._seq}"})
            tm.ws(parent)[self.key].append(entity)
            tm.writes.append(("create", self.key, body["name"], body))
            return entity
        return tm._req(f"{self.method}.create", run)

    def update(self, path, body, fingerprint=None):
        tm = self.tm

        def run():
            ws_path = path.split(f"/{self.method}/")[0]
            for i, existing in enumerate(tm.ws(ws_path)[self.key]):
                if existing["path"] == path:
                    entity = dict(body, **{self.id_field: existing[self.id_field], "path": path, "fingerprint": "fp-upd"})
                    tm.ws(ws_path)[self.key][i] = entity
                    tm.writes.append(("update", self.key, body["name"], body, fingerprint))
                    return entity
            raise FakeHttpError(404, "not found")
        return tm._req(f"{self.method}.update", run)


def container(container_id="555", public=PUBLIC, workspaces=None):
    return {"containerId": container_id, "publicId": public, "name": "Shop", "usageContext": ["web"],
            "workspaces": workspaces if workspaces is not None else [
                {"workspaceId": "3", "name": "Other"}, {"workspaceId": "2", "name": "Default Workspace"}]}


def one_account_service(**kwargs):
    return FakeTagManager([{"accountId": "111", "containers": [container()]}], **kwargs)


@pytest.fixture(autouse=True)
def _isolated_gtm_state(monkeypatch, tmp_path):
    """Per-test resolve-cache dir (never the real ~/.hermes), pacing off, fresh pacing state."""
    monkeypatch.setenv("GTM_CACHE_DIR", str(tmp_path / "gtm-cache"))
    monkeypatch.delenv("GTM_RESOLVE_FRESH_S", raising=False)
    monkeypatch.delenv("GTM_RESOLVE_STALE_S", raising=False)
    monkeypatch.setattr(gtm, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(gtm, "_last_call_at", None)


WS_PATH = "accounts/111/containers/555/workspaces/2"            # the merchant's Default Workspace
TARIQ_WS_NAME = f"Tariq - {PUBLIC}"
TARIQ_PATH = "accounts/111/containers/555/workspaces/4"         # the dedicated workspace the fake creates (next free id)


def service_with_tariq_ws():
    svc = one_account_service()
    svc.accounts_data[0]["containers"][0]["workspaces"].append({"workspaceId": "4", "name": TARIQ_WS_NAME})
    return svc


LIMIT_ERROR = FakeHttpError(400, "Cannot create workspace: container reached the maximum number of workspaces (3).")
CONSENT_TAG = {"name": "Consent Mode Default", "type": "html", "parameter": [{"type": "template", "key": "html", "value": "<script></script>"}],
               "firingTriggerId": ["2147479572"], "consentSettings": {"consentStatus": "notNeeded"}}


def good_spec(**overrides):
    spec = {
        "variables": [{"name": "DLV - event_id", "type": "v", "parameter": [{"type": "template", "key": "name", "value": "event_id"}]}],
        "triggers": [{"name": "CE - purchase", "type": "customEvent",
                      "customEventFilter": [{"type": "equals", "parameter": [
                          {"type": "template", "key": "arg0", "value": "{{_event}}"},
                          {"type": "template", "key": "arg1", "value": "purchase"}]}]}],
        "tags": [
            CONSENT_TAG,
            {"name": "GA4 - purchase", "type": "gaawe", "parameter": [
                {"type": "template", "key": "eventName", "value": "purchase"},
                {"type": "template", "key": "sendEcommerceData", "value": "true"},
                {"type": "template", "key": "eventId", "value": "{{DLV - event_id}}"}],
             "firingTriggerName": ["CE - purchase"], "firingTriggerId": ["2147479553"]},
        ],
    }
    spec.update(overrides)
    return json.dumps(spec)


# ---------------------------------------------------------------------------------------------------------
# T1 - classifier
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,message,reason,code,audience,retryable", [
    (403, "Request had insufficient authentication scopes.", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "SCOPE_INSUFFICIENT", "operator", False),
    (403, "Tag Manager API has not been used in project 123 before or it is disabled.", "accessNotConfigured", "API_DISABLED", "operator", False),
    (403, "x", "SERVICE_DISABLED", "API_DISABLED", "operator", False),
    (403, "The caller does not have permission", "forbidden", "PERMISSION_MISSING", "client", False),
    (403, "User rate limit", "rateLimitExceeded", "RETRYABLE", "operator", True),
    (404, "Not found: container", "notFound", "CONTAINER_NOT_FOUND", "client", False),
    (429, "Quota exceeded", "rateLimitExceeded", "RETRYABLE", "operator", True),
    (500, "Internal", "backendError", "RETRYABLE", "operator", True),
    (503, "Unavailable", "", "RETRYABLE", "operator", True),
    (400, "Bad request", "invalid", "UNKNOWN", "operator", False),
])
def test_classifier_http_branches(status, message, reason, code, audience, retryable):
    out = gtm.classify_gtm_error(FakeHttpError(status, message, reason))
    assert out["status"] == "error"
    assert (out["reason_code"], out["audience"], out["retryable"]) == (code, audience, retryable)
    assert out["detail"].startswith(f"HTTP {status}")
    if audience == "client":
        assert SA_EMAIL in out["client_action_ar"] and SA_EMAIL in out["client_action_en"]
    else:
        assert "client_action_ar" not in out and "client_action_en" not in out


def test_classifier_permission_missing_tells_client_to_grant_publish():
    out = gtm.classify_gtm_error(FakeHttpError(403, "denied", "forbidden"))
    assert "Publish" in out["client_action_en"] and "User Management" in out["client_action_en"]
    assert "Publish" in out["client_action_ar"]


def test_classifier_non_http_error_is_unknown_operator():
    out = gtm.classify_gtm_error(ValueError("something odd"))
    assert out["reason_code"] == "UNKNOWN" and out["audience"] == "operator" and out["retryable"] is False
    assert "ValueError" in out["detail"]


def test_classifier_never_echoes_raw_body_with_extra_fields():
    err = FakeHttpError(403, "denied", content='{"error":{"message":"denied"},"secret_token":"s3cr3t-value"}')
    assert "s3cr3t-value" not in json.dumps(gtm.classify_gtm_error(err))


# ---------------------------------------------------------------------------------------------------------
# T2 - verify_access
# ---------------------------------------------------------------------------------------------------------
def test_verify_access_connected_picks_default_workspace_by_name(monkeypatch):
    monkeypatch.setenv("GTM_RESOLVE_FRESH_S", "0")          # second call below uses a different fake service
    out = gtm.verify_access(one_account_service(), PUBLIC)
    assert out["status"] == "CONNECTED"
    assert (out["public_id"], out["account_id"], out["container_id"]) == (PUBLIC, "111", "555")
    assert out["container_name"] == "Shop" and out["usage_context"] == ["web"]
    assert out["default_workspace_id"] == "2"
    assert out["tariq_workspace_id"] is None
    assert "PERMISSION_MISSING" in out["note"]
    assert gtm.verify_access(service_with_tariq_ws(), PUBLIC)["tariq_workspace_id"] == "4"


def test_verify_access_without_default_name_uses_lowest_numeric_workspace_id():
    ws = [{"workspaceId": "10", "name": "A"}, {"workspaceId": "9", "name": "B"}]
    svc = FakeTagManager([{"accountId": "111", "containers": [container(workspaces=ws)]}])
    assert gtm.verify_access(svc, PUBLIC)["default_workspace_id"] == "9"


def test_verify_access_paginates_accounts_and_containers():
    accounts = [
        {"accountId": "1", "containers": [container("10", "GTM-AAAA111"), container("11", "GTM-BBBB222")]},
        {"accountId": "2", "containers": [container("20", "GTM-CCCC333")]},
        {"accountId": "3", "containers": [container("30", "GTM-DDDD444"), container("31", PUBLIC)]},
    ]
    svc = FakeTagManager(accounts, page_size=1)
    out = gtm.verify_access(svc, PUBLIC)
    assert out["status"] == "CONNECTED" and out["account_id"] == "3" and out["container_id"] == "31"
    assert svc.calls.count("accounts.list") == 3          # one call per page


def test_verify_access_not_invited():
    svc = FakeTagManager([{"accountId": "1", "containers": [container("10", "GTM-ZZZZ999")]}])
    out = gtm.verify_access(svc, PUBLIC)
    assert (out["status"], out["reason_code"], out["audience"]) == ("error", "NOT_INVITED", "client")
    assert SA_EMAIL in out["client_action_ar"] and SA_EMAIL in out["client_action_en"]
    assert gtm.verify_access(FakeTagManager([]), PUBLIC)["reason_code"] == "NOT_INVITED"


@pytest.mark.parametrize("bad", ["", "gtm-abc1234", "GTM-AB", "GTM-ABCDEFGHIJK", "GTM_ABC1234", "ABC1234", "GTM-ABC 123", None, 123])
def test_verify_access_rejects_invalid_ids_without_api_calls(bad):
    svc = one_account_service()
    out = gtm.verify_access(svc, bad)
    assert out["reason_code"] == "INVALID_ID" and out["audience"] == "client" and "client_action_ar" in out
    assert svc.calls == []


def test_verify_access_403_maps_to_permission_missing():
    svc = one_account_service()
    svc.errors["containers.list"] = FakeHttpError(403, "The caller does not have permission", "forbidden")
    out = gtm.verify_access(svc, PUBLIC)
    assert out["reason_code"] == "PERMISSION_MISSING" and out["audience"] == "client"


# ---------------------------------------------------------------------------------------------------------
# T3 - workspace_apply
# ---------------------------------------------------------------------------------------------------------
def test_apply_creates_in_dependency_order_and_resolves_trigger_names():
    svc = one_account_service()
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["status"] == "applied", out
    assert (out["account_id"], out["container_id"], out["workspace_id"]) == ("111", "555", "4")
    assert (out["workspace_name"], out["workspace_mode"]) == (TARIQ_WS_NAME, "dedicated")
    assert out["created"] == {"variables": 1, "triggers": 1, "tags": 2}
    assert out["updated"] == {"variables": 0, "triggers": 0, "tags": 0}
    assert "hermes_gtm_cloud_publish" in out["next_step"] and "D-003" in out["next_step"]
    assert [w[1] for w in svc.writes] == ["variable", "trigger", "tag", "tag"]
    trigger_id = svc.ws(TARIQ_PATH)["trigger"][0]["triggerId"]
    ga4 = next(w[3] for w in svc.writes if w[2] == "GA4 - purchase")
    assert ga4["firingTriggerId"] == ["2147479553", trigger_id]        # built-in id kept, name resolved to the new id
    assert "firingTriggerName" not in ga4
    consent = next(w[3] for w in svc.writes if w[2] == "Consent Mode Default")
    assert consent["firingTriggerId"] == ["2147479572"]


def test_apply_creates_dedicated_workspace_and_leaves_default_untouched():
    svc = one_account_service()
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert svc.workspaces_created == [TARIQ_WS_NAME]
    assert out["workspace_id"] == "4" and out["workspace_mode"] == "dedicated"
    assert svc.ws(WS_PATH) == {"tag": [], "trigger": [], "variable": [], "builtInVariable": []}
    assert "workspaces.getStatus" not in svc.calls


def test_apply_reuses_existing_dedicated_workspace():
    svc = service_with_tariq_ws()
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["status"] == "applied" and out["workspace_id"] == "4"
    assert svc.workspaces_created == [] and "workspaces.create" not in svc.calls
    again = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert again["workspace_id"] == "4" and svc.workspaces_created == []


def test_apply_limit_with_clean_default_falls_back_to_default_workspace():
    svc = one_account_service()
    svc.errors["workspaces.create"] = LIMIT_ERROR
    svc.statuses[WS_PATH] = {"workspaceChange": []}
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["status"] == "applied"
    assert (out["workspace_id"], out["workspace_name"], out["workspace_mode"]) == ("2", "Default Workspace", "default_fallback")
    assert len(svc.ws(WS_PATH)["tag"]) == 2 and "workspaces.getStatus" in svc.calls


def test_apply_limit_with_dirty_default_refuses_and_writes_nothing():
    svc = one_account_service()
    svc.errors["workspaces.create"] = FakeHttpError(403, "Workspace limit exceeded for this container", "forbidden")
    svc.statuses[WS_PATH] = {"workspaceChange": [{"tag": {"name": "merchant draft"}, "changeStatus": "added"}]}
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert (out["status"], out["reason_code"], out["audience"]) == ("error", "WORKSPACE_LIMIT", "client")
    assert "Workspaces" in out["client_action_en"] and "Workspaces" in out["client_action_ar"]
    assert svc.writes == [] and svc.workspaces_created == []
    assert svc.ws(WS_PATH)["tag"] == []


def test_apply_unrelated_workspace_create_error_is_classified_not_a_fallback():
    svc = one_account_service()
    svc.errors["workspaces.create"] = FakeHttpError(403, "The caller does not have permission", "forbidden")
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["reason_code"] == "PERMISSION_MISSING"
    assert "workspaces.getStatus" not in svc.calls and svc.writes == []


def test_apply_is_idempotent_and_updates_by_name_with_fingerprint():
    svc = one_account_service()
    assert gtm.apply_container_spec(svc, PUBLIC, good_spec())["status"] == "applied"
    n_writes = len(svc.writes)
    again = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert again["created"] == {"variables": 0, "triggers": 0, "tags": 0}
    assert again["unchanged"] == {"variables": 1, "triggers": 1, "tags": 2}
    assert len(svc.writes) == n_writes                      # nothing re-written
    assert len(svc.ws(TARIQ_PATH)["tag"]) == 2                 # no duplicates

    changed = json.loads(good_spec())
    changed["variables"][0]["parameter"][0]["value"] = "transaction_id"
    out = gtm.apply_container_spec(svc, PUBLIC, json.dumps(changed))
    assert out["updated"] == {"variables": 1, "triggers": 0, "tags": 0}
    assert out["created"] == {"variables": 0, "triggers": 0, "tags": 0}
    verb, kind, name, _body, fingerprint = svc.writes[-1]
    assert (verb, kind, name, fingerprint) == ("update", "variable", "DLV - event_id", "fp-new")
    assert len(svc.ws(TARIQ_PATH)["variable"]) == 1


def test_apply_resolves_names_to_pre_existing_triggers():
    svc = service_with_tariq_ws()
    svc.ws(TARIQ_PATH)["trigger"].append({"name": "CE - purchase", "triggerId": "77", "type": "customEvent",
                                       "path": f"{TARIQ_PATH}/triggers/77", "fingerprint": "f"})
    spec = json.loads(good_spec())
    spec["triggers"] = []                                   # tag points at a trigger that already exists
    out = gtm.apply_container_spec(svc, PUBLIC, json.dumps(spec))
    assert out["status"] == "applied", out
    ga4 = next(w[3] for w in svc.writes if w[2] == "GA4 - purchase")
    assert ga4["firingTriggerId"] == ["2147479553", "77"]


def test_apply_resolves_builtin_trigger_names():
    svc = one_account_service()
    spec = json.loads(good_spec())
    spec["tags"][0].pop("firingTriggerId")
    spec["tags"][0]["firingTriggerName"] = ["Consent Initialization - All Pages"]
    spec["tags"][1]["firingTriggerName"] = ["CE - purchase", "All Pages"]
    spec["tags"][1].pop("firingTriggerId")
    assert gtm.apply_container_spec(svc, PUBLIC, json.dumps(spec))["status"] == "applied"
    consent = next(w[3] for w in svc.writes if w[2] == "Consent Mode Default")
    assert consent["firingTriggerId"] == ["2147479572"]
    ga4 = next(w[3] for w in svc.writes if w[2] == "GA4 - purchase")
    assert ga4["firingTriggerId"][1] == "2147479553"


def test_apply_unresolvable_trigger_name_refuses_and_writes_nothing():
    svc = one_account_service()
    spec = json.loads(good_spec())
    spec["tags"][1]["firingTriggerName"] = ["No Such Trigger"]
    out = gtm.apply_container_spec(svc, PUBLIC, json.dumps(spec))
    assert out["reason_code"] == "INVALID_SPEC" and "No Such Trigger" in out["detail"]
    assert svc.writes == []


def test_apply_lint_failure_refuses_and_writes_nothing():
    svc = one_account_service()
    leaky = {"name": "Meta Pixel", "type": "html", "firingTriggerId": ["2147479553"],
             "consentSettings": {"consentStatus": "notNeeded"},
             "parameter": [{"type": "template", "key": "html", "value": "<script>var t='EAA" + "B" * 40 + "';</script>"}]}
    spec = json.loads(good_spec())
    spec["tags"].append(leaky)
    out = gtm.apply_container_spec(svc, PUBLIC, json.dumps(spec))
    assert out["status"] == "error" and out["reason_code"] == "LINT_FAILED"
    assert any(f["rule"] == "security.meta_token_in_html" for f in out["findings"])
    assert "EAABBBB" not in json.dumps(out)                 # token stays redacted
    assert svc.writes == []


def test_apply_missing_consent_default_is_blocking():
    svc = one_account_service()
    spec = json.loads(good_spec())
    spec["tags"] = [spec["tags"][1]]
    out = gtm.apply_container_spec(svc, PUBLIC, json.dumps(spec))
    assert out["reason_code"] == "LINT_FAILED"
    assert any(f["rule"] == "consent.no_default_detected" for f in out["findings"])
    assert svc.writes == []


def test_apply_does_not_block_on_defects_in_untouched_existing_entities():
    svc = service_with_tariq_ws()
    svc.ws(TARIQ_PATH)["tag"].append({"name": "Old UA", "type": "ua", "tagId": "9", "firingTriggerId": ["2147479553"],
                                   "path": f"{TARIQ_PATH}/tags/9", "fingerprint": "f"})
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["status"] == "applied" and out["preexisting_findings_not_blocking"] >= 1


def test_apply_fails_closed_when_linter_unavailable():
    svc = one_account_service()
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec(), linter_loader=lambda: None)
    assert out["reason_code"] == "LINTER_UNAVAILABLE"
    assert svc.writes == [] and svc.calls == []            # refused before any API traffic


def test_load_linter_returns_none_when_not_importable(monkeypatch):
    monkeypatch.delitem(sys.modules, gtm._LINTER_MODULE_NAME, raising=False)
    monkeypatch.setattr(gtm, "_linter_candidates", lambda: [Path("/nonexistent/lint_container.py")])
    assert gtm._load_linter() is None
    out = gtm.apply_container_spec(one_account_service(), PUBLIC, good_spec())
    assert out["reason_code"] == "LINTER_UNAVAILABLE"


def test_load_linter_finds_repo_linter():
    module = gtm._load_linter()
    assert module is not None and hasattr(module, "Linter")


@pytest.mark.parametrize("spec", ["", "not json", "[]", '{"tags": "x"}', '{"nope": []}', '{"tags":[{"name":"a"}]}',
                                  '{"variables":[{"name":"a","type":"v"},{"name":"a","type":"v"}]}', "{}"])
def test_apply_rejects_malformed_specs(spec):
    svc = one_account_service()
    out = gtm.apply_container_spec(svc, PUBLIC, spec)
    assert out["reason_code"] == "INVALID_SPEC" and svc.writes == []


def test_apply_rejects_invalid_id_and_reports_not_invited():
    assert gtm.apply_container_spec(one_account_service(), "nope", good_spec())["reason_code"] == "INVALID_ID"
    svc = FakeTagManager([])
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["reason_code"] == "NOT_INVITED" and svc.writes == []


def test_apply_midway_failure_reports_what_was_already_written():
    svc = one_account_service()
    svc.errors["tags.create"] = FakeHttpError(500, "backend broke", "backendError")
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["reason_code"] == "RETRYABLE" and out["retryable"] is True
    assert out["failed_entity"] == {"type": "tags", "name": "Consent Mode Default"}
    assert out["already_written"]["variables"] == [{"name": "DLV - event_id", "action": "created"}]
    assert out["already_written"]["triggers"] == [{"name": "CE - purchase", "action": "created"}]
    assert out["already_written"]["tags"] == []


def test_apply_403_during_write_is_permission_missing_for_client():
    svc = one_account_service()
    svc.errors["variables.create"] = FakeHttpError(403, "caller lacks permission", "forbidden")
    out = gtm.apply_container_spec(svc, PUBLIC, good_spec())
    assert out["reason_code"] == "PERMISSION_MISSING" and out["audience"] == "client"
    assert out["failed_entity"]["name"] == "DLV - event_id"


# ---------------------------------------------------------------------------------------------------------
# Wrappers in consultation.py (publish fix, scopes, credentials)
# ---------------------------------------------------------------------------------------------------------
class RecordingServer:
    def __init__(self):
        self.executor = object()
        self.tools, self.descriptions = {}, {}

    def tool(self, name, description, input_schema):
        def decorator(fn):
            self.tools[name], self.descriptions[name] = fn, description
            return fn
        return decorator


@pytest.fixture
def env(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "keys" / "hitl_private.pem"
    key_file.parent.mkdir()
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    if os.name == "posix":
        key_file.chmod(0o600)
    import base64
    pub = base64.urlsafe_b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    monkeypatch.delenv("HITL_VERIFY_PUBLIC_KEY_PATH", raising=False)
    monkeypatch.setenv("HITL_SIGNING_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("HITL_VERIFY_PUBLIC_KEY", pub)
    monkeypatch.setenv("HITL_USED_NONCES_PATH", str(tmp_path / "state" / "nonces.json"))


@pytest.fixture
def consultation(monkeypatch):
    pkg = types.ModuleType("fakehermes2")
    pkg.__path__ = []
    sub = types.ModuleType("fakehermes2.tools")
    sub.__path__ = []
    executor = types.ModuleType("fakehermes2.executor")
    executor.VPSExecutor = object
    for name, mod in (("fakehermes2", pkg), ("fakehermes2.tools", sub), ("fakehermes2.executor", executor)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("fakehermes2.tools.consultation", ROOT / "ops" / "hermes" / "consultation.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(consultation):
    srv = RecordingServer()
    consultation.register_consultation_tools(srv)
    return srv


@pytest.fixture
def fake_build(monkeypatch, consultation, tmp_path):
    """Key file present; gtm_api.build_service replaced so scopes are captured and no Google lib is touched."""
    key_file = tmp_path / "gtm-service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(key_file))
    svc = one_account_service()
    seen = {"scopes": None, "path": None}

    def build_service(path, scopes):
        seen["scopes"], seen["path"] = list(scopes), path
        return svc

    monkeypatch.setattr(consultation._gtm_api(), "build_service", build_service)
    svc.seen = seen
    return svc


def publish(server, token, **kw):
    args = {"account_id": "111", "container_id": "555", "workspace_id": "2", "version_name": "v-test",
            "hitl_approval_token": token}
    args.update(kw)
    return json.loads(server.tools["hermes_gtm_cloud_publish"](**args))


def test_publish_requests_both_required_scopes_and_succeeds(env, server, fake_build):
    token = hitl_tokens.issue_publish_token("555", "2")
    out = publish(server, token)
    assert out["status"] == "success" and out["live_version_id"] == "9"
    assert fake_build.seen["scopes"] == ["https://www.googleapis.com/auth/tagmanager.edit.containerversions",
                                         "https://www.googleapis.com/auth/tagmanager.publish"]
    assert fake_build.published == [f"{WS_PATH}/versions/9"]
    assert "zero_pii" not in out and "consent_mode_v2" not in out
    assert "url" not in out and out["operator_url"].endswith(f"{WS_PATH}/versions/9")
    assert "Zero-PII" not in fake_build.calls_kwargs["body"]["notes"]


@pytest.mark.parametrize("response,code", [
    ({"compilerError": True, "containerVersion": {"path": "p/versions/9"}}, "COMPILER_ERROR"),
    ({"containerVersion": None}, "COMPILER_ERROR"),
    ({}, "COMPILER_ERROR"),
    ({"containerVersion": {"path": "p/versions/9"}, "syncStatus": {"mergeConflict": True}}, "MERGE_CONFLICT"),
    ({"containerVersion": {"path": "p/versions/9"}, "syncStatus": {"syncError": True}}, "MERGE_CONFLICT"),
])
def test_publish_refuses_bad_versions_and_never_publishes(env, server, fake_build, response, code):
    fake_build.create_version_response = response
    out = publish(server, hitl_tokens.issue_publish_token("555", "2"))
    assert out["status"] == "error" and out["reason_code"] == code and out["audience"] == "operator"
    assert fake_build.published == []


def test_publish_errors_are_classified_not_raw(env, server, fake_build):
    fake_build.errors["create_version"] = FakeHttpError(403, "Request had insufficient authentication scopes.",
                                                        "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    out = publish(server, hitl_tokens.issue_publish_token("555", "2"))
    assert out["reason_code"] == "SCOPE_INSUFFICIENT" and "error" not in out


def test_publish_failure_after_version_created_reports_the_version(env, server, fake_build):
    fake_build.errors["publish"] = FakeHttpError(403, "caller lacks permission", "forbidden")
    out = publish(server, hitl_tokens.issue_publish_token("555", "2"))
    assert out["reason_code"] == "PERMISSION_MISSING" and out["version_created_not_published"].endswith("/versions/9")


def test_publish_gate_unchanged_without_token(env, server, fake_build):
    out = publish(server, None)
    assert out["status"] == "AWAITING_HITL_APPROVAL" and out["gate"] == "D-003"
    assert fake_build.calls == []


def test_verify_access_tool_is_read_only_and_needs_no_token(env, server, fake_build):
    out = json.loads(server.tools["hermes_gtm_verify_access"](public_container_id=PUBLIC))
    assert out["status"] == "CONNECTED" and out["default_workspace_id"] == "2"
    assert fake_build.seen["scopes"] == ["https://www.googleapis.com/auth/tagmanager.readonly"]
    assert fake_build.writes == []
    bad = json.loads(server.tools["hermes_gtm_verify_access"](public_container_id="nope"))
    assert bad["reason_code"] == "INVALID_ID"


def test_workspace_apply_tool_uses_edit_scope_and_needs_no_token(env, server, fake_build):
    out = json.loads(server.tools["hermes_gtm_workspace_apply"](public_container_id=PUBLIC, container_spec=good_spec()))
    assert out["status"] == "applied"
    assert "https://www.googleapis.com/auth/tagmanager.edit.containers" in fake_build.seen["scopes"]
    assert not any("publish" in s or "containerversions" in s for s in fake_build.seen["scopes"])
    assert fake_build.published == []                       # a draft write never publishes


def test_new_tools_report_credentials_pending_when_key_missing(env, server, consultation, monkeypatch, tmp_path):
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(tmp_path / "missing.json"))
    for name, kwargs in (("hermes_gtm_verify_access", {"public_container_id": PUBLIC}),
                         ("hermes_gtm_workspace_apply", {"public_container_id": PUBLIC, "container_spec": good_spec()})):
        out = json.loads(server.tools[name](**kwargs))
        assert out["status"] == "pending_credentials" and out["reason_code"] == "CREDENTIALS_PENDING"
        assert out["audience"] == "operator"


def test_build_service_failure_is_classified(env, server, consultation, monkeypatch, tmp_path):
    key_file = tmp_path / "k.json"
    key_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(consultation, "GTM_SERVICE_ACCOUNT_PATH", str(key_file))

    def boom(path, scopes):
        raise ValueError("bad key file")

    monkeypatch.setattr(consultation._gtm_api(), "build_service", boom)
    out = json.loads(server.tools["hermes_gtm_verify_access"](public_container_id=PUBLIC))
    assert out["reason_code"] == "UNKNOWN" and out["audience"] == "operator"


def test_legacy_deploy_description_and_new_tools_registered(server):
    assert server.descriptions["hermes_gtm_deploy"].startswith(
        "LEGACY / OPERATOR-DESKTOP ONLY — headless agents must use hermes_gtm_cloud_publish. ")
    for name in ("hermes_gtm_verify_access", "hermes_gtm_workspace_apply", "hermes_gtm_cloud_publish"):
        assert name in server.tools


# ---------------------------------------------------------------------------------------------------------
# Quota hardening: retries, pacing, 429 origin, stale-if-error resolve cache
# ---------------------------------------------------------------------------------------------------------
def quota_error(status=429, reason="rateLimitExceeded", retry_delay=None,
                message="Quota exceeded for quota metric 'Queries' of tagmanager.googleapis.com"):
    err = {"code": status, "message": message, "status": "RESOURCE_EXHAUSTED", "errors": [{"reason": reason}]}
    if retry_delay is not None:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}]
    return FakeHttpError(status, message, content=json.dumps({"error": err}))


def cache_file(tmp_path, public=PUBLIC):
    return tmp_path / "gtm-cache" / f"{public}.json"


def age_cache(tmp_path, seconds):
    path = cache_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["ts"] -= seconds
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_reads_and_entity_writes_pass_num_retries(monkeypatch):
    monkeypatch.setattr(gtm, "NUM_RETRIES", 3)
    svc = one_account_service()
    assert gtm.apply_container_spec(svc, PUBLIC, good_spec())["status"] == "applied"
    assert svc.retries and all(n == 3 for _name, n in svc.retries)
    assert {"accounts.list", "workspaces.create", "tags.create", "variables.create"} <= {name for name, _n in svc.retries}


def test_update_uses_default_num_retries():
    assert gtm.NUM_RETRIES == 4
    svc = one_account_service()
    gtm.apply_container_spec(svc, PUBLIC, good_spec())
    changed = json.loads(good_spec())
    changed["variables"][0]["parameter"][0]["value"] = "transaction_id"
    svc.retries.clear()
    assert gtm.apply_container_spec(svc, PUBLIC, json.dumps(changed))["updated"]["variables"] == 1
    assert ("variables.update", 4) in svc.retries


def test_create_version_and_publish_never_retry(env, server, fake_build):
    out = publish(server, hitl_tokens.issue_publish_token("555", "2"))
    assert out["status"] == "success"
    assert ("create_version", 0) in fake_build.retries and ("publish", 0) in fake_build.retries
    assert all(n == 0 for name, n in fake_build.retries if name in ("create_version", "publish"))


def test_execute_helper_passes_retries_through():
    seen = []
    req = types.SimpleNamespace(execute=lambda num_retries=None: seen.append(num_retries) or {"ok": True})
    assert gtm._execute(req) == {"ok": True} and gtm._execute(req, num_retries=0) == {"ok": True}
    assert seen == [gtm.NUM_RETRIES, 0]


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 100.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self):
        return 1_000_000.0 + self.now


def test_pacing_sleeps_only_the_remaining_gap(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gtm, "time", clock)
    monkeypatch.setattr(gtm, "MIN_INTERVAL_S", 2.0)
    req = types.SimpleNamespace(execute=lambda num_retries=None: {})
    gtm._execute(req)
    assert clock.sleeps == []                       # first call: nothing to wait for
    clock.now += 0.5
    gtm._execute(req)
    assert clock.sleeps == [pytest.approx(1.5)]     # only the remaining gap
    clock.now += 10
    gtm._execute(req)
    assert clock.sleeps == [pytest.approx(1.5)]     # gap already elapsed


def test_pacing_off_by_default_never_sleeps(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gtm, "time", clock)
    req = types.SimpleNamespace(execute=lambda num_retries=None: {})
    gtm._execute(req)
    gtm._execute(req)
    assert clock.sleeps == []


def test_pacing_applies_to_every_api_call(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gtm, "time", clock)
    monkeypatch.setattr(gtm, "MIN_INTERVAL_S", 1.0)
    svc = one_account_service()
    assert gtm.resolve_container(svc, PUBLIC)["container_id"] == "555"
    assert len(svc.calls) == 3 and len(clock.sleeps) == 2    # accounts, containers, workspaces: a gap between each


def test_quota_429_is_unmistakably_gtm_origin_without_retry_info():
    out = gtm.classify_gtm_error(quota_error(429))
    assert out["reason_code"] == "RETRYABLE" and out["retryable"] is True and out["audience"] == "operator"
    assert out["upstream"] == "tagmanager.googleapis.com" and out["quota_exhausted"] is True
    assert "retry_after_s" not in out
    assert out["detail"].startswith("HTTP 429") and "not an LLM" in out["detail"]


@pytest.mark.parametrize("delay,expected", [("30s", 30), ("1.5s", 1.5)])
def test_quota_429_reports_retry_after_from_retry_info(delay, expected):
    out = gtm.classify_gtm_error(quota_error(429, retry_delay=delay))
    assert out["retry_after_s"] == expected and out["quota_exhausted"] is True


def test_quota_unparseable_retry_delay_is_omitted():
    assert "retry_after_s" not in gtm.classify_gtm_error(quota_error(429, retry_delay="soon"))


def test_quota_403_rate_limit_and_resource_exhausted_body():
    out = gtm.classify_gtm_error(quota_error(403, "rateLimitExceeded", retry_delay="12s"))
    assert (out["reason_code"], out["quota_exhausted"], out["upstream"], out["retry_after_s"]) == (
        "RETRYABLE", True, "tagmanager.googleapis.com", 12)
    assert gtm.classify_gtm_error(FakeHttpError(403, "User rate limit", "quotaExceeded"))["quota_exhausted"] is True
    body_only = FakeHttpError(400, "x", content='{"error":{"message":"x","status":"RESOURCE_EXHAUSTED"}}')
    assert gtm.classify_gtm_error(body_only)["quota_exhausted"] is True


def test_non_quota_errors_carry_no_quota_fields():
    for err in (FakeHttpError(500, "Internal", "backendError"), FakeHttpError(403, "denied", "forbidden"),
                FakeHttpError(404, "nf")):
        out = gtm.classify_gtm_error(err)
        assert "quota_exhausted" not in out and "upstream" not in out


def test_resolve_cache_fresh_hit_makes_zero_api_calls(tmp_path):
    first = gtm.verify_access(one_account_service(), PUBLIC)
    assert first["status"] == "CONNECTED" and "cache" not in first
    stored = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
    assert stored["result"]["container_id"] == "555" and isinstance(stored["ts"], float)
    svc = one_account_service()
    svc.errors["accounts.list"] = AssertionError("API must not be called on a fresh cache hit")
    out = gtm.verify_access(svc, PUBLIC)
    assert out["status"] == "CONNECTED" and out["cache"] == "fresh" and out["container_id"] == "555"
    assert svc.calls == []


def test_resolve_cache_never_contains_credentials(tmp_path):
    gtm.verify_access(one_account_service(), PUBLIC)
    text = cache_file(tmp_path).read_text(encoding="utf-8").lower()
    assert "private_key" not in text and "token" not in text and "secret" not in text
    assert list((tmp_path / "gtm-cache").glob("*.tmp")) == []


def test_resolve_cache_expired_fresh_window_refetches(monkeypatch, tmp_path):
    gtm.verify_access(one_account_service(), PUBLIC)
    age_cache(tmp_path, 60)
    monkeypatch.setenv("GTM_RESOLVE_FRESH_S", "10")
    svc = one_account_service()
    out = gtm.verify_access(svc, PUBLIC)
    assert "cache" not in out and "accounts.list" in svc.calls


def test_resolve_cache_stale_served_on_quota_429(tmp_path):
    gtm.verify_access(one_account_service(), PUBLIC)
    path = age_cache(tmp_path, 3600)             # beyond the 600 s fresh window, inside the 24 h stale window
    svc = one_account_service()
    svc.errors["accounts.list"] = quota_error(429)
    out = gtm.verify_access(svc, PUBLIC)
    assert out["status"] == "CONNECTED" and out["cache"] == "stale" and out["stale_reason"] == "RETRYABLE"
    assert 3590 <= out["cache_age_s"] <= 3700 and out["container_id"] == "555"
    assert path.exists()                         # a stale serve keeps the entry


def test_resolve_cache_not_served_beyond_stale_window(tmp_path):
    gtm.verify_access(one_account_service(), PUBLIC)
    age_cache(tmp_path, 90_000)                  # older than 24 h
    svc = one_account_service()
    svc.errors["accounts.list"] = FakeHttpError(503, "Unavailable")
    out = gtm.verify_access(svc, PUBLIC)
    assert out["status"] == "error" and out["reason_code"] == "RETRYABLE"


def test_resolve_cache_stale_served_on_5xx(tmp_path):
    gtm.verify_access(one_account_service(), PUBLIC)
    age_cache(tmp_path, 3600)
    svc = one_account_service()
    svc.errors["containers.list"] = FakeHttpError(500, "Internal", "backendError")
    assert gtm.verify_access(svc, PUBLIC)["cache"] == "stale"


def test_resolve_quota_without_cache_returns_the_quota_error():
    svc = one_account_service()
    svc.errors["accounts.list"] = quota_error(429, retry_delay="30s")
    out = gtm.verify_access(svc, PUBLIC)
    assert out["reason_code"] == "RETRYABLE" and out["quota_exhausted"] is True and out["retry_after_s"] == 30


@pytest.mark.parametrize("break_it", ["not_invited", "permission", "not_found"])
def test_resolve_cache_client_errors_are_not_masked_and_invalidate(monkeypatch, tmp_path, break_it):
    gtm.verify_access(one_account_service(), PUBLIC)
    path = cache_file(tmp_path)
    assert path.exists()
    monkeypatch.setenv("GTM_RESOLVE_FRESH_S", "0")        # force a live call while keeping the stale window open
    if break_it == "not_invited":
        svc = FakeTagManager([{"accountId": "1", "containers": [container("10", "GTM-ZZZZ999")]}])
        expected = "NOT_INVITED"
    else:
        svc = one_account_service()
        svc.errors["containers.list"] = (FakeHttpError(403, "caller lacks permission", "forbidden")
                                         if break_it == "permission" else FakeHttpError(404, "Not found"))
        expected = "PERMISSION_MISSING" if break_it == "permission" else "CONTAINER_NOT_FOUND"
    out = gtm.verify_access(svc, PUBLIC)
    assert out["reason_code"] == expected and out["audience"] == "client" and "cache" not in out
    assert not path.exists()
    svc2 = one_account_service()
    svc2.errors["accounts.list"] = quota_error(429)
    assert gtm.verify_access(svc2, PUBLIC)["status"] == "error"      # nothing left to serve stale


def test_resolve_cache_disabled_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GTM_RESOLVE_FRESH_S", "0")
    monkeypatch.setenv("GTM_RESOLVE_STALE_S", "0")
    assert gtm.verify_access(one_account_service(), PUBLIC)["status"] == "CONNECTED"
    assert not cache_file(tmp_path).exists()
    svc = one_account_service()
    assert "cache" not in gtm.verify_access(svc, PUBLIC) and svc.calls     # always live


def test_resolve_cache_unwritable_dir_never_raises(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("GTM_CACHE_DIR", str(blocker / "sub"))
    out = gtm.verify_access(one_account_service(), PUBLIC)
    assert out["status"] == "CONNECTED" and "cache" not in out
    svc = one_account_service()
    svc.errors["accounts.list"] = quota_error(429)
    assert gtm.verify_access(svc, PUBLIC)["reason_code"] == "RETRYABLE"     # unreadable cache == miss


def test_resolve_cache_corrupt_file_is_a_miss(tmp_path):
    path = cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    out = gtm.verify_access(one_account_service(), PUBLIC)
    assert out["status"] == "CONNECTED" and "cache" not in out
    assert json.loads(path.read_text(encoding="utf-8"))["result"]["container_id"] == "555"     # healed


def test_apply_and_publish_paths_never_write_the_cache(env, server, fake_build, tmp_path):
    assert gtm.apply_container_spec(one_account_service(), PUBLIC, good_spec())["status"] == "applied"
    assert publish(server, hitl_tokens.issue_publish_token("555", "2"))["status"] == "success"
    assert not (tmp_path / "gtm-cache").exists()


def test_successful_apply_invalidates_resolve_cache(tmp_path):
    svc = one_account_service()
    assert gtm.verify_access(svc, PUBLIC)["tariq_workspace_id"] is None
    assert cache_file(tmp_path).exists()
    assert gtm.apply_container_spec(svc, PUBLIC, good_spec())["status"] == "applied"
    assert not cache_file(tmp_path).exists()
    assert gtm.verify_access(svc, PUBLIC)["tariq_workspace_id"] == "4"      # fresh view, not the pre-apply mapping
