"""Read sealed acquisition snapshots; absence and raw coverage never gain authority."""

import hashlib
import json
import re
import subprocess
from collections import Counter

from quantlab.data.dividend_acquisition import CONFIG, OUTPUT
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries

PINNED_CODE = {
    "src/quantlab/data/dividend_acquisition.py",
    "src/quantlab/data/dividend_raw.py",
    "scripts/acquire_dividend_targets.py",
}


def verify_snapshot_inputs(root, inputs, source_head=None):
    """Historical code is checked in Git; data and source documents stay byte-bound."""
    code_inputs = {name: entry for name, entry in inputs.items() if name in PINNED_CODE}
    if not code_inputs or source_head is None:
        verify_entries(root, inputs)
        return
    if not isinstance(source_head, str) or re.fullmatch(r"[0-9a-f]{40}", source_head) is None:
        raise DataValidationError("invalid acquisition source commit")
    for name, entry in code_inputs.items():
        try:
            raw = subprocess.check_output(
                ["git", "show", f"{source_head}:{name}"],
                cwd=root,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            raise DataValidationError("acquisition source history is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != entry["sha256"] or len(raw) != entry["bytes"]:
            raise DataValidationError("historical acquisition source differs from contract")
    verify_entries(root, {name: entry for name, entry in inputs.items() if name not in PINNED_CODE})


def read_acquisition(root):
    out = root / OUTPUT
    if not (out / "latest.json").exists():
        if not (out / "progress.json").exists():
            return None
        report = sealed_read(out / "progress.json")
        kind = "progress"
    else:
        pointer = sealed_read(out / "latest.json")
        path = (out / pointer["report_path"]).resolve()
        if not path.is_relative_to((out / "runs").resolve()):
            raise DataValidationError("invalid acquisition snapshot path")
        report = sealed_read(path)
        if report["fingerprint"] != pointer["report_fingerprint"]:
            raise DataValidationError("acquisition snapshot fingerprint changed")
        verify_entries(out, report["artifacts"])
        kind = "report"
    identity = canonical_payload_fingerprint({"contract_sha256": _sha(root / CONFIG)})
    if report["identity"] != identity:
        raise DataValidationError("acquisition contract changed")
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    source_head = report.get("source_head") if kind == "report" else None
    if kind == "report" and set(config["inputs"]) & PINNED_CODE and source_head is None:
        raise DataValidationError("sealed acquisition report lacks its source commit")
    verify_snapshot_inputs(root, config["inputs"], source_head)
    request = sealed_read(root / config["request_path"])
    if request["fingerprint"] != config["request_fingerprint"]:
        raise DataValidationError("acquisition request changed")
    for key in (
        "complete_event_history_certified",
        "historical_pit_certified",
        "cashflow_eligible",
        "performance_evidence",
        "execution_authority",
    ):
        if report.get(key) is not False:
            raise DataValidationError("raw acquisition cannot gain financial authority")
    rows = report["per_code"]
    if [row["instrument_id"] for row in rows] != request["instrument_ids"]:
        raise DataValidationError("acquisition target population changed")
    if (
        len(rows) != report["instrument_count"]
        or len({r["instrument_id"] for r in rows}) != len(rows)
        or dict(Counter(r["status"] for r in rows)) != report["status_counts"]
        or sum(r["attempts"] for r in rows) != report["attempts"]
        or sum(r["rows"] or 0 for r in rows) != report["returned_rows"]
    ):
        raise DataValidationError("acquisition counts are inconsistent")
    return kind, report


def read_verification(root, report):
    path = root / OUTPUT / "independent_verification.json"
    if not path.exists():
        return None
    result = sealed_read(path)
    expected = {
        "report_fingerprint": report["fingerprint"],
        "exact_codes": report["instrument_count"],
        "attempts": report["attempts"],
        "retries": report["retries"],
        "returned_rows": report["returned_rows"],
        "status_counts": report["status_counts"],
        "charged_body_bytes_as_recorded": report["charged_body_bytes"],
        "performance_evidence": False,
        "execution_authority": False,
    }
    if any(
        result.get(flag) is not False for flag in ("performance_evidence", "execution_authority")
    ):
        raise DataValidationError("independent acquisition verification gained authority")
    if any(result.get(key) != value for key, value in expected.items()):
        raise DataValidationError("independent acquisition verification differs from report")
    charged = result.get("conservative_body_budget_bytes")
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    if (
        type(charged) is not int
        or not report["charged_body_bytes"] <= charged <= config["max_body_bytes"]
    ):
        raise DataValidationError("invalid reconciled acquisition budget")
    if result.get("failure_budget_correction_bytes") != charged - report["charged_body_bytes"]:
        raise DataValidationError("acquisition budget correction does not reconcile")
    return result
