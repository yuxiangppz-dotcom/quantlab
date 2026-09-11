"""Read sealed acquisition snapshots; absence and raw coverage never gain authority."""

import json
from collections import Counter

from quantlab.data.dividend_acquisition import CONFIG, OUTPUT
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries


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
    verify_entries(root, config["inputs"])
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
