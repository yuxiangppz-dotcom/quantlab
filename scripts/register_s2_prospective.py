"""One registration from already saved bytes; no provider or future price access."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.cohort_dividends import pushed_head
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.s2_prospective import (
    CALENDAR,
    CONFIG,
    OUTPUT,
    parse_snapshot,
    validate_config,
    validate_entry_calendar,
    validate_registration,
    valuation_scores,
)


def main():
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root / CONFIG).read_text())
    validate_config(c)
    verify_entries(root, c["inputs"])
    if any((root / p).stat().st_size != e["bytes"] for p, e in c["inputs"].items()):
        raise DataValidationError("prospective source sizes changed")
    result, intent = sealed_read(root / c["result_path"]), sealed_read(root / c["intent_path"])
    if (
        result["status"] != "nonempty"
        or result["rows"] != 5550
        or result["at"] != c["source_received_at"]
        or result["intent_fingerprint"] != intent["fingerprint"]
        or result["wire_sha256"] != _sha(root / c["raw_path"])
        or intent["request"]["id"] != "basic_20260911"
    ):
        raise DataValidationError("saved source intake provenance changed")
    selection = sealed_read(root / c["selection_path"])
    if selection["fingerprint"] != c["selection_fingerprint"]:
        raise DataValidationError("cohort selection changed")
    codes = tuple(selection["instrument_ids"])
    received = datetime.fromisoformat(result["at"])
    validate_registration(datetime.now(UTC), received)
    validate_entry_calendar(pd.read_parquet(root / CALENDAR))
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if head != pushed_head(root):
        raise DataValidationError("source must be clean and pushed")
    config_sha = _sha(root / CONFIG)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, c["resources"])
        budget.check(projected_bytes=4 * 1024**2)
        atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "actual_attempt": 1,
                "code_inputs": binding.entries,
                "config_sha256": config_sha,
                "new_rule_candidate_charged": 1,
            },
        )
        with budget.watchdog():
            frame = parse_snapshot((root / c["raw_path"]).read_bytes())
            scores = valuation_scores(frame, codes)
            created = datetime.now(UTC)
            validate_registration(created, received)
            verify_entries(root, c["inputs"])
            binding.check()
            if pushed_head(root) != head or _sha(root / CONFIG) != config_sha:
                raise DataValidationError("registration source changed")
            report = atomic_seal(
                out / "observation.json",
                {
                    "created_at": created.isoformat(),
                    "source_received_at": result["at"],
                    "source_head": head,
                    "config_sha256": config_sha,
                    "identity": c["identity"],
                    "selection_fingerprint": selection["fingerprint"],
                    "price_as_of": c["price_as_of"],
                    "records": scores.astype(object)
                    .where(pd.notna(scores), None)
                    .to_dict("records"),
                    "cohort_size": len(codes),
                    "scores_known": int(scores.score_known.sum()),
                    "reasons": {str(k): int(v) for k, v in scores.reason.value_counts().items()},
                    "label_definition": (
                        "adjusted close at entry+20 common trading sessions / entry close -1"
                    ),
                    "earliest_entry_session": c["earliest_entry_session"],
                    "future_exit_date": None,
                    "future_calendar_complete": False,
                    "label_status": "pending_future_calendar_and21_session_prices",
                    "future_labels_evaluated": False,
                    "correlation": None,
                    "provider_requests": 0,
                    "economic_paths": 0,
                    "model_fits": 0,
                    "rule_candidates_cumulative": 4,
                    "new_rule_candidates": 1,
                    "historical_pit_certified": False,
                    "historical_performance_eligible": False,
                    "performance_evidence": False,
                    "execution_authority": False,
                    "resources": {
                        "seconds": time.monotonic() - budget.started,
                        "peak_rss_bytes": peak_rss_bytes(),
                        "bytes_before_report": budget.check(projected_bytes=4 * 1024**2),
                    },
                },
            )
            budget.check()
            print(
                json.dumps(
                    {
                        k: report[k]
                        for k in (
                            "fingerprint",
                            "created_at",
                            "scores_known",
                            "reasons",
                            "resources",
                        )
                    }
                )
            )


if __name__ == "__main__":
    main()
