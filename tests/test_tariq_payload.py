"""Consistency tests for Tariq's instruction payload (scripts/build_tariq_payload.py)."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_FILE = ROOT / "tariq_instructions_payload.json"
CONSULTATION = ROOT / "ops" / "hermes" / "consultation.py"
PLAYBOOK = ROOT / "docs" / "tariq" / "CLIENT_PLAYBOOK_AR.md"
BUILD_SCRIPT = ROOT / "scripts" / "build_tariq_payload.py"
# Self-imposed test cap, raised per docs/tariq/HANDOFF_TARIQ_M2M.md section 2 ("raise the cap ... rather than trimming rules").
# The Wesam field limit is unverified, so this is not a known platform limit.
INNER_BUDGET_BYTES = 18 * 1024

REQUIRED_KEYS = {
    "agent_profile",
    "identity_and_credentials",
    "strict_guardrails",
    "mandatory_tool_routing",
    "workflows",
    "telemetry_standards",
    "error_handling",
    "client_messages",
}
ALLOWED_TOOLS = {
    "hermes_gtm_verify_access",
    "hermes_gtm_workspace_apply",
    "hermes_gtm_cloud_publish",
    "hermes_audit_code",
    "hermes_run_task",
    "hermes_consult",
}
SA_EMAIL = "tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com"
TOOL_RE = re.compile(r"hermes_[a-z_]+")


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_tariq_payload", ROOT / "scripts" / "build_tariq_payload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def builder():
    return _load_builder()


@pytest.fixture(scope="module")
def outer():
    return json.loads(PAYLOAD_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prompt(outer):
    return json.loads(outer["instructions"])


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _strings_with_path(obj, path=()):
    """Yield (path_of_keys, string) for every string leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings_with_path(v, path + (k,))
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings_with_path(v, path)
    elif isinstance(obj, str):
        yield path, obj


def test_outer_file_shape(outer):
    assert set(outer) == {"instructions"}
    assert isinstance(outer["instructions"], str)


def test_inner_has_required_keys(prompt):
    assert isinstance(prompt, dict)
    assert REQUIRED_KEYS <= set(prompt)


def test_file_not_stale(builder, tmp_path, outer):
    out = tmp_path / "payload.json"
    built = builder.build(out)
    assert built == outer, "tariq_instructions_payload.json is stale: run python scripts/build_tariq_payload.py"
    assert out.read_text(encoding="utf-8") == PAYLOAD_FILE.read_text(encoding="utf-8")
    assert json.loads(built["instructions"]) == builder.TARIQ_PROMPT


def test_inner_size_budget(outer):
    assert len(outer["instructions"].encode("utf-8")) < INNER_BUDGET_BYTES


def test_routing_and_workflows_reference_only_allowed_tools(prompt):
    for section in ("mandatory_tool_routing", "workflows"):
        used = set(TOOL_RE.findall(_dump(prompt[section])))
        assert used <= ALLOWED_TOOLS, f"{section}: unexpected tools {used - ALLOWED_TOOLS}"
    # every routing entry names an allowed tool
    for entry in prompt["mandatory_tool_routing"]["routing_matrix"]:
        assert entry["tool"] in ALLOWED_TOOLS


def test_every_allowed_tool_is_routed(prompt):
    used = set(TOOL_RE.findall(_dump(prompt["mandatory_tool_routing"])))
    assert used == ALLOWED_TOOLS


def test_deploy_only_in_prohibited_lists_and_skill_execute_nowhere(outer, prompt):
    assert "hermes_skill_execute" not in outer["instructions"]
    assert "hermes_skill_execute" not in PAYLOAD_FILE.read_text(encoding="utf-8")
    hits = [(p, s) for p, s in _strings_with_path(prompt) if "hermes_gtm_deploy" in s]
    assert hits, "legacy deploy tool should be listed as prohibited"
    for path, _ in hits:
        assert any(k.startswith("prohibited") for k in path), f"hermes_gtm_deploy outside a prohibited list: {path}"


def test_allowed_tools_are_registered_in_hermes():
    registered = set(re.findall(r'name="(hermes_[a-z_]+)"', CONSULTATION.read_text(encoding="utf-8")))
    missing = ALLOWED_TOOLS - registered
    assert not missing, f"payload references tools not registered in ops/hermes/consultation.py: {sorted(missing)}"


def test_event_id_templates_have_no_timestamp(prompt):
    text = _dump(prompt["telemetry_standards"])
    templates = re.findall(r"'((?:order|confirmed|delivered)_\{order_id\}[^']*)'", text)
    assert {t.split("_")[0] for t in templates} >= {"order", "confirmed", "delivered"}
    for t in templates:
        assert t in {"order_{order_id}", "confirmed_{order_id}", "delivered_{order_id}"}
        assert "timestamp" not in t.lower()
    assert "{timestamp}" not in _dump(prompt)
    assert "timestamp" not in _dump(prompt["telemetry_standards"]["event_ids_and_dedup"]).lower()


def test_d005_open_question_present(prompt):
    d005 = prompt["telemetry_standards"]["d005_signal_ladder"]
    assert "open_question" in d005
    assert "confirmed_{order_id}" in d005["ConfirmedOrder"]
    assert "delivered_{order_id}" in d005["DeliveredPurchase"]
    assert "order placed" in d005["DeliveredPurchase"]


def test_service_account_email_exact(prompt):
    assert prompt["identity_and_credentials"]["service_account_email"] == SA_EMAIL
    assert SA_EMAIL in prompt["client_messages"]["M1"]["text_ar"]
    assert "private key" in prompt["identity_and_credentials"]["key_handling"]


def test_gtm_regex_in_payload(prompt):
    rx = re.compile(prompt["workflows"]["W1_INTAKE"]["validation"]["gtm_container_id"])
    assert rx.match("GTM-5C5N552P")
    assert not rx.match("GTM-")
    assert not rx.match("G-ABC")


def test_other_validation_patterns(prompt):
    v = prompt["workflows"]["W1_INTAKE"]["validation"]
    assert re.match(v["ga4_measurement_id"], "G-ABC123XYZ9")
    assert not re.match(v["ga4_measurement_id"], "G-AB")
    assert re.match(v["meta_pixel_or_dataset_id"], "123456789012345")
    assert not re.match(v["meta_pixel_or_dataset_id"], "12345")
    assert set(v["platform"]) == {"shopify", "salla", "zid", "woocommerce"}


def test_client_messages_complete(prompt):
    msgs = prompt["client_messages"]
    assert set(msgs) == {"M1", "M2", "M3", "M3-ws", "M3-op"}
    for m in msgs.values():
        assert re.search(r"[؀-ۿ]", m["text_ar"])
    assert "{missing_permission}" in msgs["M3"]["text_ar"]
    assert "{container_name}" in msgs["M2"]["text_ar"] and "{public_id}" in msgs["M2"]["text_ar"]
    assert "{public_id}" in msgs["M3-ws"]["text_ar"]
    # verify_access is read-only: M2 must not claim publish permission
    assert "بصلاحية النشر" not in msgs["M2"]["text_ar"]
    # M3 must work when the SA is not yet in the user list
    assert "Add users" in msgs["M3"]["text_ar"]


def test_m2_eta_text(prompt):
    m2 = prompt["client_messages"]["M2"]["text_ar"]
    assert "24 ساعة عمل" in m2 and "خلال خلال" not in m2


def test_playbook_mirrors_payload(prompt):
    doc = PLAYBOOK.read_text(encoding="utf-8")
    for mid, m in prompt["client_messages"].items():
        assert m["text_ar"] in doc, f"docs/tariq/CLIENT_PLAYBOOK_AR.md is out of sync for {mid}"


def test_client_code_set_matches_tool_contract(prompt):
    codes = prompt["workflows"]["W2_HANDSHAKE"]["client_codes"]
    assert set(codes) == {"NOT_INVITED", "PERMISSION_MISSING", "CONTAINER_NOT_FOUND", "INVALID_ID", "WORKSPACE_LIMIT"}
    assert len(codes) == len(set(codes))
    # operator-side codes must never be listed as client codes
    assert not set(codes) & {"SCOPE_INSUFFICIENT", "API_DISABLED", "RETRYABLE", "UNKNOWN", "CREDENTIALS_PENDING"}


def test_no_default_workspace_reference(prompt):
    text = _dump(prompt)
    assert not re.search(r"workspace[_ ]?(id)?\s*[=:]?\s*[\"']?2\b", text, re.IGNORECASE)
    steps = " ".join(prompt["workflows"]["W4_APPROVAL_PUBLISH"]["steps"])
    assert "workspace_id returned by workspace_apply" in steps
    assert "NEW token" in steps and "operator_url" in steps


def test_w3_hard_consent_rule(prompt):
    steps = " ".join(prompt["workflows"]["W3_BUILD"]["steps"])
    assert "HARD RULE" in steps and "Consent Initialization - All Pages" in steps


def test_trigger_ids_are_builtin(prompt):
    builtin = {"2147479553", "2147479573", "2147479572"}
    ids = set(re.findall(r"\b2\d{9}\b", _dump(prompt)))
    assert ids <= builtin
    assert "2253783" not in _dump(prompt)
    text = _dump(prompt["telemetry_standards"]["consent_mode_v2"])
    assert "Consent Initialization - All Pages 2147479572" in text
    assert "All Pages 2147479553" in text and "Initialization - All Pages 2147479573" in text


# --- COD signal activation (W6/W7), opt-in via build_prompt(with_cod_workflows=True) ---

MOTAHAI_TOOLS = {"hermes_motahai_tenant_status", "hermes_motahai_signal_health", "hermes_motahai_promote_live"}
COD_WORKFLOW_IDS = {"W6_COD_SIGNAL_ACTIVATION", "W7_SIGNAL_HEALTH_CHECK"}
COD_MESSAGE_IDS = {"M6-1", "M6-2", "M6-3"}
BASE_MESSAGE_IDS = {"M1", "M2", "M3", "M3-ws", "M3-op"}
SECRET_WORDS = (
    "token", "secret", "password", "passcode", "api key", "api_key", "private key", "credential",
    "توكن", "كلمة المرور", "كلمة السر", "كلمة سر", "مفتاح", "الرمز السري", "رمز الوصول",
)
NEGATION = {"never", "not", "no", "لا", "ولا", "لن", "دون", "بدون", "ممنوع"}
CHECKED_KEYS = {"steps", "rules", "preconditions", "note", "trigger", "when", "text_ar"}


def _client_ask_violations(texts):
    """Sentences that name a secret without a prohibition or an operator-side (hermes_/operator) reference."""
    bad = []
    for text in texts:
        for sent in re.split(r"[.;\n]", text):
            low = sent.lower()
            if not any(w in low for w in SECRET_WORDS):
                continue
            words = re.findall(r"[\w'-]+", low)
            if any(w in NEGATION or w.startswith("non-") for w in words):
                continue
            if "hermes_" in low or "operator" in low:
                continue
            bad.append(sent.strip())
    return bad


def _cod_texts(prompt):
    routes = [e for e in prompt["mandatory_tool_routing"]["routing_matrix"] if e["tool"] in MOTAHAI_TOOLS]
    texts = []
    for wid in COD_WORKFLOW_IDS:
        texts += [s for p, s in _strings_with_path(prompt["workflows"][wid]) if p and p[-1] in CHECKED_KEYS]
    texts += [s for p, s in _strings_with_path(routes) if p and p[-1] in CHECKED_KEYS]
    texts += [prompt["client_messages"][m]["text_ar"] for m in COD_MESSAGE_IDS]
    texts += [prompt["client_messages"][m]["when"] for m in COD_MESSAGE_IDS]
    return texts


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture(scope="module")
def cod_prompt(builder):
    return builder.build_prompt(with_cod_workflows=True)


def test_default_prompt_has_no_cod_content(builder):
    default = builder.build_prompt()
    text = _dump(default)
    assert "hermes_motahai_" not in text
    assert "W6_" not in text and "W7_" not in text
    assert "M6-" not in text
    assert default == builder.TARIQ_PROMPT
    assert "W6_COD_SIGNAL_ACTIVATION" not in builder.TARIQ_PROMPT["workflows"]


def test_cod_flag_adds_workflows_and_messages(builder, cod_prompt):
    assert COD_WORKFLOW_IDS <= set(cod_prompt["workflows"])
    assert set(cod_prompt["client_messages"]) == BASE_MESSAGE_IDS | COD_MESSAGE_IDS
    # flag-on keeps every existing section and message text unchanged
    for mid in BASE_MESSAGE_IDS:
        assert cod_prompt["client_messages"][mid] == builder.TARIQ_PROMPT["client_messages"][mid]
    assert cod_prompt["workflows"]["W5_VERIFY"] == builder.TARIQ_PROMPT["workflows"]["W5_VERIFY"]


def test_cod_tool_set_is_routed_and_closed(cod_prompt):
    mode_tools = ALLOWED_TOOLS | MOTAHAI_TOOLS
    routing = cod_prompt["mandatory_tool_routing"]
    assert set(TOOL_RE.findall(_dump(routing))) == mode_tools
    used = set(TOOL_RE.findall(_dump({"routing": routing, "workflows": cod_prompt["workflows"]})))
    assert used <= mode_tools, f"unexpected tools {used - mode_tools}"
    for tool in MOTAHAI_TOOLS:
        assert any(e["tool"] == tool for e in routing["routing_matrix"]), f"{tool} is not routed"
    promote = next(e for e in routing["routing_matrix"] if e["tool"] == "hermes_motahai_promote_live")
    assert "D-003" in promote["note"] and "single-use" in promote["note"]
    assert "promote_live returns LIVE" in _dump(cod_prompt["workflows"]["W6_COD_SIGNAL_ACTIVATION"])


def test_cod_inner_size_budget(builder, cod_prompt):
    assert len(builder.inner_text(cod_prompt).encode("utf-8")) < INNER_BUDGET_BYTES


def test_cod_steps_and_messages_never_ask_for_secrets(cod_prompt):
    assert _client_ask_violations(_cod_texts(cod_prompt)) == []


def test_client_ask_checker_flags_real_requests():
    assert _client_ask_violations(["Send us your Meta token now."]) == ["Send us your Meta token now"]
    assert _client_ask_violations(["أرسل لنا كلمة المرور الآن."]) != []
    assert _client_ask_violations(["Never ask the client for the token."]) == []
    assert _client_ask_violations(["Operator sends the token via hermes_consult."]) == []
    assert _client_ask_violations(["Send NON-SECRET fields only."]) == []


def test_cod_messages_match_playbook_and_payload_file(builder, tmp_path):
    out = tmp_path / "cod_payload.json"
    built = builder.build(out, with_cod_workflows=True)
    prompt = json.loads(built["instructions"])
    assert prompt == builder.build_prompt(with_cod_workflows=True)
    doc = PLAYBOOK.read_text(encoding="utf-8")
    for mid in COD_MESSAGE_IDS:
        text = prompt["client_messages"][mid]["text_ar"]
        assert re.search(r"[؀-ۿ]", text)
        assert text in doc, f"docs/tariq/CLIENT_PLAYBOOK_AR.md is out of sync for {mid}"


def test_cli_cod_flag_without_out_refuses_and_leaves_payload(tmp_path):
    before = PAYLOAD_FILE.read_bytes()
    proc = _run_cli("--with-cod-workflows")
    assert proc.returncode != 0
    assert PAYLOAD_FILE.read_bytes() == before


def test_cli_cod_flag_with_out_writes_only_that_file(tmp_path):
    before = PAYLOAD_FILE.read_bytes()
    out = tmp_path / "cod_payload.json"
    proc = _run_cli("--with-cod-workflows", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert PAYLOAD_FILE.read_bytes() == before
    assert "hermes_motahai_promote_live" in json.loads(out.read_text(encoding="utf-8"))["instructions"]


def test_build_refuses_cod_flag_on_default_payload(builder):
    before = PAYLOAD_FILE.read_bytes()
    with pytest.raises(ValueError):
        builder.build(builder.PAYLOAD_PATH, with_cod_workflows=True)
    proc = _run_cli("--with-cod-workflows", "--out", str(builder.PAYLOAD_PATH))
    assert proc.returncode != 0
    assert PAYLOAD_FILE.read_bytes() == before
