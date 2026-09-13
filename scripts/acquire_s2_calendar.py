"""Two exact requests with durable intent; append planned dates to the S2 observation."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from quantlab.data.dividend_raw import WireClient
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.program_intake import Journal, acquire
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.cohort_dividends import pushed_head
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.s2_calendar import (
    CONFIG,
    OBSERVATION,
    OUTPUT,
    PROOF,
    joint_window,
    parse_calendar,
    profile,
    requests_for,
)


def main():
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root / CONFIG).read_text())
    if (
        c["requests"] != requests_for()
        or c["max_requests"] != 2
        or c["max_retries"] != 0
        or c["endpoint"] != "https://api.tushare.pro"
        or c["per_response_bytes"] != 65536
        or c["max_body_bytes"] != 131072
        or c["execution_authority"] is not False
    ):
        raise DataValidationError("unregistered calendar request scope")
    verify_entries(root, c["inputs"])
    observation, proof = (
        sealed_read(root / c["observation_path"]),
        sealed_read(root / c["proof_path"]),
    )
    if observation["fingerprint"] != OBSERVATION or proof["fingerprint"] != PROOF:
        raise DataValidationError("original observation or proof changed")
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise DataValidationError("configured credential unavailable")
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if head != pushed_head(root):
        raise DataValidationError("source not pushed")
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, c["resources"])
        budget.check(projected_bytes=1048576)
        atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "code_inputs": binding.entries,
                "config_sha256": _sha(root / CONFIG),
                "actual_attempt": 1,
            },
        )
        journal = Journal(
            out, c, requests_for(), canonical_payload_fingerprint(c), inspector=profile
        )
        client = WireClient(token, c["endpoint"])
        try:
            with budget.watchdog():
                stop = acquire(journal, budget, client, gap_seconds=1)
                intake = atomic_seal(out / "intake.json", {**journal.summary(), "stop": stop})
                if len(journal.records) != 2 or any(
                    r["status"] != "nonempty" for _, _, r in journal.records
                ):
                    raise DataValidationError("planned calendar intake incomplete; no retry")
                sessions = [
                    parse_calendar((out / "attempts" / ex / "response.body").read_bytes(), ex)
                    for ex in ("SSE", "SZSE")
                ]
                window = joint_window(*sessions)
                binding.check()
                verify_entries(root, c["inputs"])
                if pushed_head(root) != head:
                    raise DataValidationError("source head changed during calendar acquisition")
                report = atomic_seal(
                    out / "calendar_plan.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "config_sha256": _sha(root / CONFIG),
                        "observation_fingerprint": OBSERVATION,
                        "original_proof_fingerprint": PROOF,
                        "intake_fingerprint": intake["fingerprint"],
                        "planned_label_dates": [str(d) for d in window],
                        "planned_entry": str(window[0]),
                        "planned_end": str(window[-1]),
                        "subsequent_sessions": 20,
                        "request_count": 2,
                        "provider_cumulative": 357,
                        "future_actual_openings_certified": False,
                        "future_labels_evaluated": False,
                        "execution_authority": False,
                        "performance_evidence": False,
                        "requires_calendar_revalidation_at_evaluation": True,
                        "raw_files": {
                            f"attempts/{ex}/response.body": {
                                "sha256": _sha(out / "attempts" / ex / "response.body"),
                                "bytes": (out / "attempts" / ex / "response.body").stat().st_size,
                            }
                            for ex in ("SSE", "SZSE")
                        },
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes_before_report": budget.check(projected_bytes=1048576),
                        },
                    },
                )
                print(
                    json.dumps({k: report[k] for k in ("fingerprint", "planned_end", "resources")})
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {
                    "at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "requests_attempted": len(journal.records),
                },
            )
            raise
        finally:
            client.close()


if __name__ == "__main__":
    main()
