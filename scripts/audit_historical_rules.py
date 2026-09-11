"""Run the pinned four-scope calendar review once; no data-provider/trading API."""

import os

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[name] = "2"

import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import UTC, date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import pyarrow as pa  # noqa: E402

pa.set_cpu_count(2)
pa.set_io_thread_count(2)

from quantlab.data.models import DataValidationError  # noqa: E402
from quantlab.execution.rules import default_a_share_rule_book  # noqa: E402
from quantlab.research.alpha158_store import (  # noqa: E402
    Budget,
    atomic_seal,
    exclusive_job,
    peak_rss_bytes,
)
from quantlab.research.input_audit import _sha  # noqa: E402
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read  # noqa: E402
from quantlab.research.rule_evidence import (  # noqa: E402
    CONFIG,
    MANIFEST,
    OUTPUT,
    audit_dates,
    baseline_fingerprint,
    load_catalogue,
)


def main():
    root = Path(__file__).resolve().parents[1]
    catalogue = load_catalogue(root)
    contract = catalogue.contract
    out = root / OUTPUT
    with exclusive_job(out):
        if (out / "started.json").exists():
            raise DataValidationError("audit already started; preserve it and inspect its receipt")
        budget = Budget(out, contract["resources"])
        budget.check(projected_bytes=8 * 1024**2)
        binding = InputBinding(root)
        head = code_binding(root, binding)
        upstream = subprocess.check_output(
            ["git", "rev-parse", "@{upstream}"], cwd=root, text=True,
        ).strip()
        if head != upstream:
            raise DataValidationError("audit contract must be committed and pushed before review")
        inventory = sealed_read(root / contract["calendar"]["path"])
        if inventory["fingerprint"] != contract["calendar"]["fingerprint"]:
            raise DataValidationError("sealed source calendar changed")
        sessions = [date.fromisoformat(day) for day in inventory["sessions"]
                    if contract["audit_interval"][0] <= day <= contract["audit_interval"][1]]
        if len(sessions) != contract["calendar"]["expected_sessions"]:
            raise DataValidationError("fixed calendar session count changed")
        baseline = default_a_share_rule_book()
        if baseline_fingerprint(baseline) != contract["baseline_fingerprint"]:
            raise DataValidationError("frozen default rule book changed")
        atomic_seal(out / "started.json", {
            "started_at": datetime.now(UTC).isoformat(),
            "source_head": head, "catalogue_sha256": _sha(root / CONFIG),
            "source_manifest_sha256": _sha(root / MANIFEST), "execution_authority": False,
        })
        before = time.monotonic()
        try:
            with budget.watchdog():
                result = audit_dates(catalogue, sessions, baseline)
                binding.check()
                budget.check(projected_bytes=8 * 1024**2)
                report = atomic_seal(out / "report.json", {
                    "completed_at": datetime.now(UTC).isoformat(),
                    "version": contract["version"], "source_head": head,
                    "catalogue_sha256": _sha(root / CONFIG),
                    "source_manifest_sha256": _sha(root / MANIFEST),
                    "inputs": catalogue.manifest["files"],
                    "baseline_fingerprint": contract["baseline_fingerprint"],
                    "calendar_fingerprint": inventory["fingerprint"],
                    "execution_authority": False, "performance_evidence": False,
                    "audit_interval": contract["audit_interval"], **result,
                    "source_discrepancies": contract["discrepancies"],
                    "limitations": contract["limitations"],
                    "resources": {"seconds": time.monotonic() - before,
                                  "peak_rss_bytes": peak_rss_bytes(),
                                  "bytes_before_report": budget.check(),
                                  "arrow_cpu_threads": pa.cpu_count(),
                                  "arrow_io_threads": pa.io_thread_count()},
                })
            print(json.dumps({key: report[key] for key in
                              ("fingerprint", "summary", "resources")}, indent=2))
        except Exception as exc:
            atomic_seal(out / "failed.json", {"error": str(exc), "source_head": head})
            raise


if __name__ == "__main__":
    main()
