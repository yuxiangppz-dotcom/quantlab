"""Validate the sealed input-readiness result without refreshing its source snapshot."""

from quantlab.data.models import DataValidationError
from quantlab.research.cost_input_sources import CONFIG, MANIFEST, OUTPUT
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries


def read_report(root):
    out = root / OUTPUT
    if not (out / "report.json").exists():
        return None
    report = sealed_read(out / "report.json")
    if report["contract_sha256"] != _sha(root / CONFIG) or report["source_manifest_sha256"] != _sha(
        root / MANIFEST
    ):
        raise DataValidationError("cost input report source/contract identity changed")
    for flag in (
        "net_cost_ready",
        "performance_evidence",
        "execution_authority",
        "fresh_forward_evidence",
    ):
        if report.get(flag) is not False:
            raise DataValidationError("input-readiness report cannot gain financial authority")
    if report.get("new_fit_attempts") != 0 or report.get("complete_cost") is not None:
        raise DataValidationError("cost input audit cannot fit models or certify full costs")
    verify_entries(out, report["artifacts"])
    request = sealed_read(out / "acquisition_request.json")
    if request != {
        "report_fingerprint": report["fingerprint"],
        **report["acquisition_request"],
        "fingerprint": request["fingerprint"],
    }:
        raise DataValidationError("acquisition request does not match the sealed audit")
    return report
