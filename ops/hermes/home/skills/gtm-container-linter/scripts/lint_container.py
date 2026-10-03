#!/usr/bin/env python3
"""
gtm-container-linter — deterministic static analysis of a GTM web container.

Accepts either:
  * a GTM UI export file  ({"exportFormatVersion": 2, "containerVersion": {...}})
  * a raw ContainerVersion from the Tag Manager API v2 (versions:live)

Emits a JSON (or Markdown) report with findings, a 0-100 health score and a
stable config fingerprint for drift detection. No network, no LLM, stdlib only.

Exit codes:
  0  no findings at or above --fail-on
  1  findings at or above --fail-on
  2  input / usage error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any


LINTER_VERSION = "1.0.0"

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
SEVERITY_PENALTY = {"critical": 25, "high": 10, "medium": 4, "low": 1, "info": 0}

# Built-in triggers (All Pages, Initialization, Consent Initialization) use
# reserved IDs in this range; they never appear in containerVersion.trigger.
BUILTIN_TRIGGER_MIN_ID = 2147479000
CONSENT_INIT_TRIGGER_ID = "2147479572"

# Tag types with Google's built-in consent checks (Consent Mode aware).
GOOGLE_TAG_TYPES = {
    "googtag", "gaawc", "gaawe", "ua", "awct", "sp", "gclidw",
    "flc", "fls", "awud", "awcc", "awcr",
}
# Tag types that fire arbitrary/third-party code and need explicit consent.
NON_GOOGLE_TAG_TYPES = {"html", "img"}
CONSENT_NAME_HINT = re.compile(r"consent|cookiebot|onetrust|cmp|usercentrics|didomi|complianz", re.I)

META_CONVERSION_EVENTS = {
    "Purchase", "AddToCart", "InitiateCheckout", "AddPaymentInfo", "Lead",
    "CompleteRegistration", "Subscribe", "StartTrial", "Contact", "SubmitApplication",
}

# Keys that change on every save/export and must not affect fingerprints or
# duplicate detection.
VOLATILE_KEYS = {
    "accountId", "containerId", "containerVersionId", "workspaceId", "fingerprint",
    "tagManagerUrl", "path", "parentFolderId", "notes", "name", "tagId", "triggerId",
    "variableId", "templateId", "folderId", "monitoringMetadata", "monitoringMetadataTagNameKey",
}

VAR_REF = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
FBQ_TRACK = re.compile(r"""fbq\(\s*['"](?:track|trackSingle)['"]\s*,\s*(?:['"][^'"]+['"]\s*,\s*)?['"](\w+)['"]""")
FBQ_INIT = re.compile(r"""fbq\(\s*['"]init['"]\s*,\s*['"]?(\d{6,20})""")
META_TOKEN = re.compile(r"EAA[A-Za-z0-9]{30,}")
SECRET_HINT = re.compile(r"""(access_token|api_secret|client_secret)\s*[:=]\s*['"][^'"]{12,}['"]""", re.I)
INSECURE_SRC = re.compile(r"""src\s*=\s*['"]http://""", re.I)


@dataclass
class Finding:
    rule: str
    severity: str
    entity_type: str
    entity_id: str
    entity_name: str
    message: str
    fix: str
    evidence: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────── helpers ────────────────────────────────

def load_container(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise UI export vs API ContainerVersion into one shape."""
    cv = raw.get("containerVersion", raw)
    if not isinstance(cv, dict) or not any(k in cv for k in ("tag", "trigger", "variable")):
        raise ValueError("Input is not a GTM export or ContainerVersion (no tag/trigger/variable keys).")
    return cv


def walk_strings(node: Any) -> Iterable[str]:
    """Yield every string value inside a nested GTM parameter structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_strings(v)


def walk_params_of_type(node: Any, ptype: str) -> Iterable[str]:
    """Yield `value` of every parameter whose `type` == ptype (e.g. triggerReference)."""
    if isinstance(node, dict):
        if node.get("type") == ptype and isinstance(node.get("value"), str):
            yield node["value"]
        for v in node.values():
            yield from walk_params_of_type(v, ptype)
    elif isinstance(node, list):
        for v in node:
            yield from walk_params_of_type(v, ptype)


def param(tag: dict[str, Any], key: str) -> str | None:
    for p in tag.get("parameter", []) or []:
        if p.get("key") == key:
            return p.get("value")
    return None


def strip_volatile(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: strip_volatile(v) for k, v in sorted(node.items()) if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [strip_volatile(v) for v in node]
    return node


def canonical(node: Any) -> str:
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(cv: dict[str, Any]) -> str:
    """Stable hash of the functional config (ignores IDs, names, notes, folders)."""
    def by_name(items: list[dict[str, Any]]) -> list[Any]:
        return [strip_volatile(i) | {"_n": i.get("name")} for i in sorted(items, key=lambda x: x.get("name", ""))]

    material = {
        "tag": by_name(cv.get("tag", []) or []),
        "trigger": by_name(cv.get("trigger", []) or []),
        "variable": by_name(cv.get("variable", []) or []),
        "builtInVariable": sorted(b.get("name", "") for b in cv.get("builtInVariable", []) or []),
        "customTemplate": by_name(cv.get("customTemplate", []) or []),
    }
    return hashlib.sha256(canonical(material).encode("utf-8")).hexdigest()


def redact(s: str) -> str:
    return s[:6] + "…[REDACTED]" if len(s) > 6 else "[REDACTED]"


# ─────────────────────────────── linter ─────────────────────────────────

class Linter:
    def __init__(self, cv: dict[str, Any]):
        self.cv = cv
        self.tags: list[dict[str, Any]] = cv.get("tag", []) or []
        self.triggers: list[dict[str, Any]] = cv.get("trigger", []) or []
        self.variables: list[dict[str, Any]] = cv.get("variable", []) or []
        self.builtins = {b.get("name", "") for b in cv.get("builtInVariable", []) or []}
        self.trigger_by_id = {t.get("triggerId"): t for t in self.triggers}
        self.var_names = {v.get("name", "") for v in self.variables}
        self.findings: list[Finding] = []

    def add(self, rule: str, sev: str, etype: str, ent: dict[str, Any], id_key: str, msg: str, fix: str, **ev: Any) -> None:
        self.findings.append(Finding(rule, sev, etype, str(ent.get(id_key, "")), ent.get("name", ""), msg, fix, ev))

    # ── references ──
    def _referenced_trigger_ids(self) -> set[str]:
        refs: set[str] = set()
        for t in self.tags:
            refs.update(t.get("firingTriggerId", []) or [])
            refs.update(t.get("blockingTriggerId", []) or [])
        for tr in self.triggers:  # trigger groups
            refs.update(walk_params_of_type(tr.get("parameter", []), "triggerReference"))
        return refs

    def _sequenced_tag_names(self) -> set[str]:
        names: set[str] = set()
        for t in self.tags:
            for key in ("setupTag", "teardownTag"):
                for s in t.get(key, []) or []:
                    if s.get("tagName"):
                        names.add(s["tagName"])
            names.update(walk_params_of_type(t.get("parameter", []), "tagReference"))
        return names

    def _var_refs(self, entity: dict[str, Any]) -> set[str]:
        refs: set[str] = set()
        for s in walk_strings({k: v for k, v in entity.items() if k not in ("name", "notes")}):
            refs.update(m.strip() for m in VAR_REF.findall(s))
        return refs

    # ── rules ──
    def check_tags(self) -> None:
        sequenced = self._sequenced_tag_names()
        for t in self.tags:
            ttype = t.get("type", "")
            if t.get("paused"):
                self.add("tag.paused", "low", "tag", t, "tagId",
                         "Paused tag still ships in the container (bloat, confusion during audits).",
                         "Delete it, or move it to an 'Archive' folder and document why it is kept.")
                continue
            if not (t.get("firingTriggerId") or []) and t.get("name") not in sequenced:
                self.add("tag.no_firing_trigger", "medium", "tag", t, "tagId",
                         "Tag has no firing trigger and is not used in tag sequencing — it can never fire.",
                         "Delete the tag or attach the intended trigger.")
            for tid in (t.get("firingTriggerId") or []) + (t.get("blockingTriggerId") or []):
                if tid not in self.trigger_by_id and not _is_builtin_trigger(tid):
                    self.add("tag.missing_trigger", "high", "tag", t, "tagId",
                             f"Tag references trigger ID {tid} which does not exist in this version.",
                             "Re-attach a valid trigger; republishing with a dangling reference breaks firing.",
                             trigger_id=tid)
            if ttype == "ua":
                self.add("tag.universal_analytics", "medium", "tag", t, "tagId",
                         "Universal Analytics tag — UA stopped processing data in July 2024; this is dead weight.",
                         "Delete; ensure an equivalent GA4 event exists.")
            if ttype == "gaawe":
                self._check_ga4_event(t)
            if ttype == "html":
                self._check_custom_html(t)

    def _check_ga4_event(self, t: dict[str, Any]) -> None:
        event = (param(t, "eventName") or "").strip()
        if event not in ("purchase", "refund"):
            return
        if (param(t, "sendEcommerceData") or "").lower() == "true":
            return  # transaction_id/currency/value come from the dataLayer ecommerce object
        blob = " ".join(walk_strings(t.get("parameter", [])))
        if "transaction_id" not in blob:
            self.add("ga4.purchase_no_transaction_id", "high", "tag", t, "tagId",
                     f"GA4 '{event}' tag sends neither ecommerce data nor a transaction_id — GA4 cannot dedupe repeat fires.",
                     "Enable 'Send Ecommerce data' (Data Layer source) or map transaction_id explicitly.")
        if "currency" not in blob:
            self.add("ga4.purchase_no_currency", "medium", "tag", t, "tagId",
                     f"GA4 '{event}' tag has no currency parameter — revenue is dropped when value is set without currency.",
                     "Map currency (ISO 4217, e.g. EGP) from the dataLayer.")

    def _check_custom_html(self, t: dict[str, Any]) -> None:
        html = param(t, "html") or ""
        for tok in META_TOKEN.findall(html):
            self.add("security.meta_token_in_html", "critical", "tag", t, "tagId",
                     "A Meta access token is embedded in client-side HTML — anyone can read it and send events as you.",
                     "Revoke the token in Meta Events Manager NOW, then move CAPI calls server-side (sGTM/Stape).",
                     token=redact(tok))
        for m in SECRET_HINT.finditer(html):
            self.add("security.secret_in_html", "critical", "tag", t, "tagId",
                     f"Possible secret ('{m.group(1)}') hard-coded in a Custom HTML tag shipped to every browser.",
                     "Rotate the secret and move the call server-side.")
        if INSECURE_SRC.search(html):
            self.add("html.insecure_src", "high", "tag", t, "tagId",
                     "Custom HTML loads a resource over http:// — blocked as mixed content on HTTPS pages.",
                     "Switch to https:// or remove.")
        if "document.write" in html:
            self.add("html.document_write", "medium", "tag", t, "tagId",
                     "document.write in Custom HTML can blank the page or be ignored after load.",
                     "Use DOM APIs (createElement/appendChild) instead.")
        if re.search(r"\beval\s*\(", html):
            self.add("html.eval", "medium", "tag", t, "tagId",
                     "eval() in Custom HTML — CSP-hostile and an injection risk.",
                     "Remove eval; use explicit logic.")
        tracked = set(FBQ_TRACK.findall(html)) & META_CONVERSION_EVENTS
        if tracked and "eventID" not in html:
            self.add("meta.pixel_no_event_id", "high", "tag", t, "tagId",
                     f"Meta Pixel conversion(s) {sorted(tracked)} sent without eventID — browser/CAPI deduplication impossible.",
                     "Pass {eventID: {{DLV - event_id}}} as the 4th fbq argument and send the same event_id via CAPI.",
                     events=sorted(tracked))

    def check_meta_pixel_inits(self) -> None:
        inits: dict[str, list[str]] = defaultdict(list)
        for t in self.tags:
            if t.get("type") == "html" and not t.get("paused"):
                for pid in FBQ_INIT.findall(param(t, "html") or ""):
                    inits[pid].append(t.get("name", ""))
        for pid, names in inits.items():
            if len(names) > 1:
                self.findings.append(Finding(
                    "meta.duplicate_pixel_init", "medium", "tag", "", ", ".join(names),
                    f"Meta Pixel {pid} is initialised in {len(names)} Custom HTML tags — risk of double PageView/events.",
                    "Initialise once (single base tag on All Pages / Initialization); other tags only call fbq('track').",
                    {"pixel_id": pid, "tags": names}))

    def check_google_tag_duplicates(self) -> None:
        by_id: dict[str, list[str]] = defaultdict(list)
        for t in self.tags:
            if t.get("paused"):
                continue
            if t.get("type") in ("googtag", "gaawc"):
                mid = param(t, "tagId") or param(t, "measurementId")
                if mid and "{{" not in mid:
                    by_id[mid].append(t.get("name", ""))
        for mid, names in by_id.items():
            if len(names) > 1:
                self.findings.append(Finding(
                    "google.duplicate_google_tag", "high", "tag", "", ", ".join(names),
                    f"Google tag {mid} is configured by {len(names)} tags — duplicate page_view / config hits.",
                    "Keep one Google tag per ID on Initialization - All Pages; move settings into it.",
                    {"tag_id": mid, "tags": names}))

    def check_exact_duplicates(self) -> None:
        tag_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in self.tags:
            if not t.get("paused"):
                tag_groups[canonical(strip_volatile(t))].append(t)
        for group in tag_groups.values():
            if len(group) > 1:
                self.findings.append(Finding(
                    "tag.exact_duplicate", "high", "tag", ",".join(str(g.get("tagId")) for g in group),
                    ", ".join(g.get("name", "") for g in group),
                    f"{len(group)} tags have identical configuration AND triggers — every event is sent {len(group)}x.",
                    "Delete all but one.", {}))

        trig_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for tr in self.triggers:
            trig_groups[canonical(strip_volatile(tr))].append(tr)
        for group in trig_groups.values():
            if len(group) > 1:
                self.findings.append(Finding(
                    "trigger.duplicate", "low", "trigger", ",".join(str(g.get("triggerId")) for g in group),
                    ", ".join(g.get("name", "") for g in group),
                    f"{len(group)} triggers have identical conditions.",
                    "Consolidate onto one trigger and repoint tags.", {}))

    def check_orphans(self) -> None:
        referenced = self._referenced_trigger_ids()
        for tr in self.triggers:
            if tr.get("triggerId") not in referenced:
                self.add("trigger.unused", "low", "trigger", tr, "triggerId",
                         "Trigger is not used by any tag or trigger group.",
                         "Delete it.")

        used: dict[str, int] = defaultdict(int)
        for ent in self.tags + self.triggers:
            for r in self._var_refs(ent):
                used[r] += 1
        for v in self.variables:
            for r in self._var_refs(v):
                if r != v.get("name"):
                    used[r] += 1
        for v in self.variables:
            if used.get(v.get("name", ""), 0) == 0:
                self.add("variable.unused", "low", "variable", v, "variableId",
                         "Variable is never referenced by a tag, trigger or other variable.",
                         "Delete it (check custom templates that read it via API first).")

        known = self.var_names | self.builtins
        seen: set[tuple[str, str]] = set()
        for etype, items, id_key in (("tag", self.tags, "tagId"), ("trigger", self.triggers, "triggerId"),
                                     ("variable", self.variables, "variableId")):
            for ent in items:
                for r in self._var_refs(ent):
                    if r in known or r.startswith("_"):  # {{_event}} etc. are internal
                        continue
                    if (ent.get("name", ""), r) in seen:
                        continue
                    seen.add((ent.get("name", ""), r))
                    self.add("variable.undefined_reference", "high", etype, ent, id_key,
                             f"References {{{{{r}}}}} which is neither a user-defined nor an enabled built-in variable.",
                             "Create/enable the variable or fix the name (names are case-sensitive).",
                             variable=r)

    def check_consent(self) -> None:
        consent_tags = [
            t for t in self.tags
            if CONSENT_INIT_TRIGGER_ID in (t.get("firingTriggerId") or [])
            or CONSENT_NAME_HINT.search(t.get("name", ""))
            or (t.get("type", "").startswith("cvt_") and CONSENT_NAME_HINT.search(t.get("name", "")))
        ]
        ad_tags = [t for t in self.tags if not t.get("paused") and t.get("type") not in ("gclidw",)]
        if ad_tags and not consent_tags:
            self.findings.append(Finding(
                "consent.no_default_detected", "high", "container", "", "",
                "No tag fires on 'Consent Initialization - All Pages' and none looks like a CMP — "
                "Consent Mode v2 default state is probably not set before tags fire (heuristic).",
                "Add the CMP template (Cookiebot/OneTrust/etc.) or a consent-default tag on Consent Initialization, "
                "setting ad_storage, analytics_storage, ad_user_data, ad_personalization.", {}))

        for t in self.tags:
            if t.get("paused") or t in consent_tags:
                continue
            ttype = t.get("type", "")
            if ttype in GOOGLE_TAG_TYPES:
                continue
            if ttype in NON_GOOGLE_TAG_TYPES or ttype.startswith("cvt_"):
                status = (t.get("consentSettings") or {}).get("consentStatus", "notSet")
                if status == "notSet":
                    self.add("consent.non_google_tag_unchecked", "medium", "tag", t, "tagId",
                             "Non-Google tag has no 'Additional consent checks' — it fires regardless of the user's choice.",
                             "Set Consent Settings → Require additional consent (e.g. ad_storage for pixels), "
                             "or 'No additional consent required' if it is strictly necessary.")

    def check_size(self) -> None:
        size = len(canonical(self.cv).encode("utf-8"))
        html_bytes = sum(len((param(t, "html") or "").encode("utf-8")) for t in self.tags if t.get("type") == "html")
        if size > 150_000:
            self.findings.append(Finding(
                "container.size", "medium" if size < 200_000 else "high", "container", "", "",
                f"Export size ≈ {size // 1024} KB (proxy for the 200 KB GTM web container limit).",
                "Remove dead tags/variables, move large Custom HTML to templates or server-side.",
                {"export_bytes": size, "custom_html_bytes": html_bytes}))

    # ── run ──
    def run(self) -> list[Finding]:
        self.check_tags()
        self.check_meta_pixel_inits()
        self.check_google_tag_duplicates()
        self.check_exact_duplicates()
        self.check_orphans()
        self.check_consent()
        self.check_size()
        self.findings.sort(key=lambda f: (-SEVERITY_ORDER[f.severity], f.rule, f.entity_name))
        return self.findings


def _is_builtin_trigger(tid: str) -> bool:
    return tid.isdigit() and int(tid) >= BUILTIN_TRIGGER_MIN_ID


# ─────────────────────────────── report ─────────────────────────────────

def build_report(cv: dict[str, Any], findings: list[Finding], max_findings: int) -> dict[str, Any]:
    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in findings:
        counts[f.severity] += 1
    score = max(0, 100 - sum(SEVERITY_PENALTY[f.severity] for f in findings))
    container = cv.get("container", {}) or {}
    return {
        "linter": "gtm-container-linter",
        "linter_version": LINTER_VERSION,
        "container": {
            "container_id": str(container.get("containerId", cv.get("containerId", ""))),
            "public_id": container.get("publicId", ""),
            "version_id": str(cv.get("containerVersionId", "")),
            "gtm_fingerprint": str(cv.get("fingerprint", "")),
        },
        "config_fingerprint": fingerprint(cv),
        "inventory": {
            "tags": len(cv.get("tag", []) or []),
            "triggers": len(cv.get("trigger", []) or []),
            "variables": len(cv.get("variable", []) or []),
            "custom_templates": len(cv.get("customTemplate", []) or []),
        },
        "health_score": score,
        "counts": counts,
        "findings_total": len(findings),
        "findings_truncated": len(findings) > max_findings,
        "findings": [asdict(f) for f in findings[:max_findings]],
    }


def to_markdown(r: dict[str, Any]) -> str:
    c = r["container"]
    lines = [
        f"# GTM Lint Report — {c['public_id'] or c['container_id']} (v{c['version_id']})",
        "",
        f"**Health score:** {r['health_score']}/100 · **Fingerprint:** `{r['config_fingerprint'][:16]}`",
        f"**Inventory:** {r['inventory']['tags']} tags · {r['inventory']['triggers']} triggers · "
        f"{r['inventory']['variables']} variables",
        "**Counts:** " + " · ".join(f"{k}: {v}" for k, v in r["counts"].items()),
        "",
        "| Sev | Rule | Entity | Problem | Fix |",
        "|---|---|---|---|---|",
    ]
    for f in r["findings"]:
        ent = f"{f['entity_type']} `{f['entity_name'] or f['entity_id']}`"
        lines.append(f"| {f['severity']} | `{f['rule']}` | {ent} | {f['message']} | {f['fix']} |")
    if r["findings_truncated"]:
        lines.append(f"\n_Truncated: showing {len(r['findings'])} of {r['findings_total']} findings._")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic GTM container linter.")
    ap.add_argument("path", help="Container JSON file, or '-' for stdin")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    ap.add_argument("--fail-on", choices=("critical", "high", "medium", "low", "never"), default="high")
    ap.add_argument("--max-findings", type=int, default=150, help="Cap findings in output (token budget).")
    args = ap.parse_args(argv)

    try:
        if args.path == "-":
            raw = json.load(sys.stdin)
        else:
            with open(args.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        cv = load_container(raw)
    except (OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc)}) + "\n")
        return 2

    findings = Linter(cv).run()
    report = build_report(cv, findings, args.max_findings)
    out = to_markdown(report) if args.format == "md" else json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    sys.stdout.write(out)

    if args.fail_on == "never":
        return 0
    threshold = SEVERITY_ORDER[args.fail_on]
    return 1 if any(SEVERITY_ORDER[f.severity] >= threshold for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
