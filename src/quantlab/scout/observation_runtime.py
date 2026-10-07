"""Bounded, idempotent, model-free mature-report scanner for local/cloud runners."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from quantlab.scout.models import SHANGHAI, fingerprint
from quantlab.scout.nextday_tracking import observe_nextday, summarize_nextday
from quantlab.scout.ranking_tracking import observe_ranking
from quantlab.scout.tracking import _write_snapshot, observe_run

VERSION = "automatic_mature_observation_scan_v1"


def scan_pending(
    runs_root,
    data_dir,
    output_root,
    *,
    observed_at=None,
    allow_synthetic=False,
    max_reports=200,
    eligible_target_sessions=None,
):
    """Append observations/receipts; retry missing data without touching a prediction.

    Local advisory locks guard two processes and release on exit/crash.
    Cloud callers additionally claim and
    archive this independent job/receipt through their existing durable authority.
    Clock overrides are accepted only when every scanned report is synthetic.
    """
    runs_root, data_dir, output_root = map(Path, (runs_root, data_dir, output_root))
    if output_root.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError("Observation state cannot be inside canonical data")
    if not 1 <= max_reports <= 1000:
        raise ValueError("Observation scan cap must be between 1 and 1000")
    if observed_at is not None and not allow_synthetic:
        raise ValueError("Clock override requires explicit synthetic replay")
    output_root.mkdir(parents=True, exist_ok=True)
    lock = output_root / ".observation-scan.lock"
    import fcntl

    stream = lock.open("a+")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return {
            "version": VERSION,
            "status": "another_observer_active_or_recovery_required",
            "model_calls": 0,
        }
    try:
        now = observed_at or datetime.now(SHANGHAI)
        if now.tzinfo is None:
            raise ValueError("Observation clock must have timezone")
        paths = sorted(runs_root.rglob("report.json"))
        persisted = {}
        for path in (output_root / "nextday").glob("*.json"):
            item = json.loads(path.read_text(encoding="utf-8"))
            previous = persisted.get(item["run_id"])
            if previous is None or item["observed_at"] > previous["observed_at"]:
                persisted[item["run_id"]] = item
        # Mature history must not consume every slot and starve newer pending reports.
        paths.sort(
            key=lambda p: (persisted.get(p.parent.name, {}).get("maturity") == "complete", str(p))
        )
        entries = []
        for report_path in paths[:max_reports]:
            run = report_path.parent
            original = report_path.read_bytes()
            try:
                report = json.loads(original)
                synthetic = report.get("synthetic") or report.get("status") == "demo"
                if observed_at is not None and not synthetic:
                    raise ValueError("Real prediction cannot use replay clock")
                if report.get("nextday_freeze"):
                    path = observe_nextday(
                        run,
                        data_dir,
                        output_root / "nextday",
                        observed_at=observed_at,
                        allow_synthetic=allow_synthetic,
                    )
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    persisted[report["run_id"]] = payload
                    entries.append(
                        {
                            "run_id": report["run_id"],
                            "protocol": "nextday",
                            "snapshot": path.name,
                            "maturity": payload.get("maturity"),
                            "snapshot_sha256": fingerprint(payload),
                        }
                    )
                elif report.get("opportunity_freeze"):
                    path = observe_ranking(
                        run,
                        data_dir,
                        output_root / "legacy_ranking",
                        observed_at=observed_at,
                        allow_synthetic=allow_synthetic,
                    )
                    entries.append(
                        {"run_id": report["run_id"], "protocol": "legacy_H5", "snapshot": path.name}
                    )
                elif not synthetic:
                    path = observe_run(run, data_dir, output_root / "legacy_topn")
                    entries.append(
                        {
                            "run_id": report["run_id"],
                            "protocol": "legacy_topn",
                            "snapshot": path.name,
                        }
                    )
                else:
                    entries.append(
                        {"run_id": report["run_id"], "status": "synthetic_legacy_skipped"}
                    )
            except (ValueError, KeyError, OSError, TypeError) as error:
                entries.append(
                    {
                        "run_id": run.name,
                        "status": "observation_failed_preserved",
                        "error_type": type(error).__name__,
                        "detail": str(error)[:240],
                    }
                )
            if report_path.read_bytes() != original:
                raise ValueError("Original prediction changed during read-only observation")
        # Summary is version-isolated. Duplicate formal dates are a protocol error,
        # never silently pick the best performer or newest regenerated prediction.
        summary = summarize_nextday(
            list(persisted.values()), eligible_target_sessions=eligible_target_sessions
        )
        result = {
            "run_id": "observation-scan",
            "version": VERSION,
            "status": "complete" if len(paths) <= max_reports else "truncated",
            "observed_at": now.isoformat(),
            "model_calls": 0,
            "provider_calls": 0,
            "candidate_reports": len(paths),
            "scanned_reports": len(entries),
            "omitted_reports": len(paths) - len(entries),
            "rows": entries,
            "nextday_summary": summary,
            "claim_scope": "observations_independent_of_paid_prediction_claim_and_notifications",
        }
        receipt = _write_snapshot(result, output_root / "receipts")
        return {**result, "receipt": str(receipt)}
    finally:
        fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-reports", type=int, default=200)
    args = parser.parse_args()
    print(
        json.dumps(
            scan_pending(
                args.runs_root, args.data_dir, args.output_root, max_reports=args.max_reports
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
