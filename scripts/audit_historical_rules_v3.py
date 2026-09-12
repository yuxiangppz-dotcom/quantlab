"""One bounded earlier-rule audit; no historical orders, fills or performance."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.execution.rules import default_a_share_rule_book
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.cohort_dividends import pushed_head
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.rule_evidence import audit_dates, baseline_fingerprint
from quantlab.research.rule_evidence_v2 import fixed_sessions
from quantlab.research.rule_evidence_v3 import (
    CONFIG,
    MANIFEST,
    OUTPUT,
    PARENT_REPORT_PATH,
    compare_to_v2,
    load_catalogue,
)


def main():
    root = Path(__file__).resolve().parents[1]
    catalogue = load_catalogue(root)
    out = root / OUTPUT
    head, config_sha, manifest_sha = pushed_head(root), _sha(root / CONFIG), _sha(root / MANIFEST)
    inventory = sealed_read(root / catalogue.contract["calendar"]["path"])
    sessions = fixed_sessions(catalogue, inventory)
    parent = sealed_read(root / PARENT_REPORT_PATH)
    baseline = default_a_share_rule_book()
    if baseline_fingerprint(baseline) != catalogue.contract["baseline_fingerprint"]:
        raise DataValidationError("frozen execution rule baseline changed")
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, catalogue.contract["resources"])
        budget.check(projected_bytes=10 * 1024**2)
        atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "actual_attempt": 1,
                "catalogue_sha256": config_sha,
                "source_manifest_sha256": manifest_sha,
            },
        )
        try:
            with budget.watchdog():
                result = audit_dates(catalogue, sessions, baseline)
                comparison = compare_to_v2(result, parent, catalogue)
                load_catalogue(root)
                if (
                    pushed_head(root) != head
                    or _sha(root / CONFIG) != config_sha
                    or _sha(root / MANIFEST) != manifest_sha
                ):
                    raise DataValidationError("rule source changed during audit")
                report = atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "catalogue_sha256": config_sha,
                        "source_manifest_sha256": manifest_sha,
                        "version": catalogue.contract["version"],
                        **result,
                        "comparison": comparison,
                        "calendar_fingerprint": inventory["fingerprint"],
                        "sessions": len(sessions),
                        "provider_requests": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "execution_authority": False,
                        "performance_evidence": False,
                        "limitations": catalogue.contract["limitations"],
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes_before_report": budget.check(projected_bytes=10 * 1024**2),
                        },
                    },
                )
                print(
                    json.dumps(
                        {
                            k: report[k]
                            for k in ("fingerprint", "summary", "comparison", "resources")
                        }
                    )
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json", {"error_type": type(exc).__name__, "source_head": head}
            )
            raise


if __name__ == "__main__":
    main()
