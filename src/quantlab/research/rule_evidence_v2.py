"""Add the reviewed pre-April-2023 source chain without mutating the sealed v1."""

from __future__ import annotations

import json
from datetime import date

from quantlab.data.models import DataValidationError
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.rule_evidence import (
    EvidenceCatalogue,
)
from quantlab.research.rule_evidence import (
    load_catalogue as load_v1,
)
from quantlab.research.rule_evidence import (
    read_report as read_v1_report,
)

CONFIG = "config/historical_rule_catalogue_v2.json"
MANIFEST = "config/historical_rule_sources_v2.json"
OUTPUT = "data/products/rule_evidence/rule_catalogue_v2_20260911"
PARENT_REPORT = "bcf18f0cd996497500fbee8d77ef91d217b70572b9151c5120f2c73b12ae4b93"


def validate_extension(contract, manifest, parent):
    """The old eight intervals are immutable; only the declared early gap can change."""
    if any(manifest["documents"].get(key) != document
           for key, document in parent.manifest["documents"].items()):
        raise DataValidationError("v2 changed inherited source provenance")
    old = {r["id"]: r for r in parent.contract["intervals"]}
    new = {r["id"]: r for r in contract["intervals"]}
    if any(new.get(key) != value for key, value in old.items()):
        raise DataValidationError("v2 changed an existing v1 rule interval")
    if contract["calendar"] != parent.contract["calendar"]:
        raise DataValidationError("v2 changed the frozen calendar contract")
    added = [r for r in contract["intervals"] if r["id"] not in old]
    if len(added) != 4:
        raise DataValidationError("v2 must add exactly the four declared early scopes")
    for entry in added:
        if (entry["from"], entry["through"]) != ("2023-01-01", "2023-04-09"):
            raise DataValidationError("v2 addition extends outside the declared evidence gap")
        events = entry["source_chain"]
        if not events or not any(e["kind"] == "amendment_outside_order_scope" for e in events):
            raise DataValidationError("early rule lacks the reviewed 2022 amendment chain")
        for event in events:
            source = manifest["documents"][event["source_id"]]
            if event["source_id"] not in entry["source_ids"] or not event["scope_note"]:
                raise DataValidationError("unbound chain source or missing scope review")
            if not source["published_on"] <= event["effective_from"] <= entry["from"]:
                raise DataValidationError("source chain publication/effectiveness is inconsistent")
        if max(e["effective_from"] for e in events) != entry["legal_effective_from"]:
            raise DataValidationError("composite applicability must include the latest amendment")
        if entry["superseded_on"] != "2023-04-10":
            raise DataValidationError("early rule must end at the registration-system transition")
    return EvidenceCatalogue(contract, manifest)


def load_catalogue(root):
    parent = load_v1(root)
    manifest = json.loads((root / MANIFEST).read_text())
    if _sha(root / CONFIG) != manifest["contract_sha256"]:
        raise DataValidationError("pinned v2 catalogue contract changed")
    verify_entries(root, manifest["files"])
    for name, entry in manifest["files"].items():
        if (root / name).stat().st_size != entry["bytes"]:
            raise DataValidationError("v2 bound source byte count changed")
    for document in manifest["documents"].values():
        if manifest["files"].get(document["path"]) != {
            "sha256": document["sha256"], "bytes": document["bytes"],
        }:
            raise DataValidationError("unbound v2 source document")
    contract = json.loads((root / CONFIG).read_text())
    new_keys = set(manifest["documents"]) - set(parent.manifest["documents"])
    actual_new_bytes = sum(manifest["documents"][key]["bytes"] for key in new_keys)
    if (new_keys != set(manifest["new_source_ids"])
            or actual_new_bytes != manifest["new_source_bytes"]
            or actual_new_bytes > contract["resources"]["max_source_bytes"]):
        raise DataValidationError("v2 additional source budget exceeded")
    return validate_extension(contract, manifest, parent)


def compare_to_v1(result, parent):
    if parent["fingerprint"] != PARENT_REPORT:
        raise DataValidationError("v1 report identity changed")
    key = lambda r: (r["exchange"], r["board"], r["session"])  # noqa: E731
    before = {key(r): r for r in parent["rows"]}
    after = {key(r): r for r in result["rows"]}
    if (len(before) != len(parent["rows"]) or len(after) != len(result["rows"])
            or before.keys() != after.keys()):
        raise DataValidationError("v1/v2 scope-date populations differ")
    additions, unchanged = [], 0
    for identity, row in after.items():
        prior = before[identity]
        if prior["catalogue_rule"] is not None:
            if (row["catalogue_rule"] != prior["catalogue_rule"]
                    or row["catalogue_values"] != prior["catalogue_values"]):
                raise DataValidationError("v2 changed a previously covered value or identity")
            unchanged += 1
        elif row["catalogue_rule"] is not None:
            if not "2023-01-01" <= row["session"] < "2023-04-10":
                raise DataValidationError("new coverage outside the declared early gap")
            additions.append({"exchange": identity[0], "board": identity[1],
                              "session": identity[2], "catalogue_rule": row["catalogue_rule"]})
    return {"parent_report_fingerprint": PARENT_REPORT, "unchanged_rows": unchanged,
            "newly_covered_rows": len(additions), "additions": additions,
            "still_unknown_rows": sum(r["catalogue_rule"] is None for r in after.values())}


def read_report(root):
    path = root / OUTPUT / "report.json"
    if not path.exists():
        return None
    report = sealed_read(path)
    catalogue = load_catalogue(root)
    if (report["catalogue_sha256"] != _sha(root / CONFIG)
            or report["source_manifest_sha256"] != _sha(root / MANIFEST)):
        raise DataValidationError("v2 report contract/source identity changed")
    if report["version"] != catalogue.contract["version"] or report["execution_authority"]:
        raise DataValidationError("invalid v2 report version or authority")
    parent = read_v1_report(root)
    if compare_to_v1(report, parent) != report["comparison_to_v1"]:
        raise DataValidationError("v2 comparison does not reconcile with frozen v1")
    return report


def fixed_sessions(catalogue, inventory):
    calendar = catalogue.contract["calendar"]
    if inventory["fingerprint"] != calendar["fingerprint"]:
        raise DataValidationError("v2 calendar source changed")
    sessions = [date.fromisoformat(s) for s in inventory["sessions"]
                if catalogue.start <= date.fromisoformat(s) <= catalogue.end]
    if len(sessions) != calendar["expected_sessions"]:
        raise DataValidationError("v2 calendar population changed")
    return sessions
