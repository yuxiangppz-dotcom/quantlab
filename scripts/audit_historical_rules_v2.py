"""Once-only v2 source-gap audit; no market-data download or execution authority."""

import os

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[name] = "2"

import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
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
    audit_dates,
    baseline_fingerprint,
)
from quantlab.research.rule_evidence import (  # noqa: E402
    read_report as read_v1_report,
)
from quantlab.research.rule_evidence_v2 import (  # noqa: E402
    CONFIG,
    MANIFEST,
    OUTPUT,
    compare_to_v1,
    fixed_sessions,
    load_catalogue,
)


def main():
    root = Path(__file__).resolve().parents[1]
    catalogue = load_catalogue(root)
    contract = catalogue.contract
    out = root / OUTPUT
    with exclusive_job(out):
        if (out / "started.json").exists():
            raise DataValidationError("v2 audit already started; preserve and inspect receipts")
        budget = Budget(out, contract["resources"])
        budget.check(projected_bytes=10 * 1024**2)
        binding = InputBinding(root)
        head = code_binding(root, binding)
        upstream = subprocess.check_output(
            ["git", "rev-parse", "@{upstream}"], cwd=root, text=True,
        ).strip()
        if head != upstream:
            raise DataValidationError("v2 audit requires a clean pushed contract")
        parent = read_v1_report(root)
        inventory = sealed_read(root / contract["calendar"]["path"])
        sessions = fixed_sessions(catalogue, inventory)
        baseline = default_a_share_rule_book()
        if baseline_fingerprint(baseline) != contract["baseline_fingerprint"]:
            raise DataValidationError("frozen default rule book changed")
        atomic_seal(out / "started.json", {
            "started_at": datetime.now(UTC).isoformat(), "source_head": head,
            "catalogue_sha256": _sha(root / CONFIG),
            "source_manifest_sha256": _sha(root / MANIFEST), "execution_authority": False,
        })
        before = time.monotonic()
        try:
            with budget.watchdog():
                result = audit_dates(catalogue, sessions, baseline)
                comparison = compare_to_v1(result, parent)
                binding.check()
                load_catalogue(root)  # Recheck external raw sources before publishing.
                report = atomic_seal(out / "report.json", {
                    "completed_at": datetime.now(UTC).isoformat(), "version": contract["version"],
                    "source_head": head, "catalogue_sha256": _sha(root / CONFIG),
                    "source_manifest_sha256": _sha(root / MANIFEST),
                    "execution_authority": False, "performance_evidence": False,
                    "calendar_fingerprint": inventory["fingerprint"],
                    "audit_interval": contract["audit_interval"], **result,
                    "comparison_to_v1": comparison,
                    "source_discrepancies": contract["discrepancies"],
                    "limitations": contract["limitations"],
                    "resources": {"seconds": time.monotonic() - before,
                                  "peak_rss_bytes": peak_rss_bytes(),
                                  "bytes_before_report": budget.check(projected_bytes=10 * 1024**2),
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
