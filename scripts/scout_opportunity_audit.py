"""Seed partial event history from checked local reports; never backfill model ranks."""

import argparse
import json
from collections import Counter
from datetime import date
from hashlib import sha256
from pathlib import Path

from quantlab.scout.models import Evidence, fingerprint, timestamp
from quantlab.scout.opportunities import append_event_snapshot, event_records, load_event_history


def audit(runs: list[Path], output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=False)
    index = output / "event_index"
    results = []
    ordered = []
    for run in runs:
        path = run / "report.json"
        before = sha256(path.read_bytes()).hexdigest()
        report = json.loads(path.read_text(encoding="utf-8"))
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        if manifest["report_sha256"] != fingerprint(report):
            raise ValueError(f"Local source report integrity failure: {run.name}")
        ordered.append((report["finished_at"], run, report, before))
    for _, run, report, before in sorted(ordered):
        cutoff = timestamp(
            report.get("timing", {}).get("information_cutoff") or report["finished_at"]
        )
        evidence = [
            Evidence(
                **{
                    **r,
                    "instrument_ids": tuple(r.get("instrument_ids", [])),
                    "event_dates": tuple(r.get("event_dates", [])),
                    "snapshot_refs": tuple(r.get("snapshot_refs", [])),
                }
            )
            for r in report["evidence"]
        ]
        history = load_event_history(index, cutoff)
        rows, exclusions = event_records(
            evidence, history, date.fromisoformat(report["market"]["session"]), cutoff
        )
        path = append_event_snapshot(index, rows, cutoff)
        source_after = sha256((run / "report.json").read_bytes()).hexdigest()
        if before != source_after:
            raise ValueError("Local report changed during read-only audit")
        results.append(
            {
                "source_run": str(run),
                "source_bytes_sha256": before,
                "source_unchanged": True,
                "report_manifest_checked": True,
                "event_snapshot": str(path),
                "event_count": len(rows),
                "novelty_counts": dict(Counter(r["novelty"] for r in rows)),
                "cutoff_exclusions": exclusions,
                "old_selection_count": len(report["selection"]["selected"]),
                "ranking_compatibility": "old_structure_no_comparison_no_backfill"
                if not report.get("opportunity_freeze")
                else "new_structure",
            }
        )
    result = {
        "status": "local_read_only_mapping_audit",
        "sources": results,
        "history_coverage": "partial_existing_reports_only_not_market_first_knowledge",
        "model_calls": 0,
        "provider_calls": 0,
        "new_predictions": 0,
        "note": ("Event interpretation only. Original reports, selections, "
                 "manifests and TopN observations were not changed."),
    }
    path = output / "local_compatibility_audit.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New directory; refuses overwrite"
    )
    args = parser.parse_args()
    print(audit(args.run, args.output_dir))
