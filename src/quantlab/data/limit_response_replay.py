"""Revalidate pinned, already observed limit responses without provider access."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from quantlab.data.context_backfill import _ExclusiveStorage, _hash_file, _write_json
from quantlab.data.enrichment import sync_daily_price_limits
from quantlab.data.models import DailyPriceLimit, DataValidationError, canonical_payload_fingerprint
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import DuplicateDataError, ParquetStorage


def _verified_report(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    fingerprint = report.pop("fingerprint")
    if fingerprint != canonical_payload_fingerprint(report):
        raise DataValidationError("cached source report fingerprint mismatch")
    return report


def _restore(value):
    if isinstance(value, dict) and set(value) == {"nonfinite"}:
        if value["nonfinite"] not in {"nan", "inf", "-inf"}:
            raise DataValidationError("invalid diagnostic numeric encoding")
        return float(value["nonfinite"])
    return value


def replay_limit_responses(
    source_reports: list[Path],
    storage: ParquetStorage,
    history_path: Path,
    output_root: Path,
    *,
    code_head: str,
) -> tuple[Path, dict]:
    import re

    if not re.fullmatch(r"[0-9a-f]{40}", code_head):
        raise DataValidationError("a full replay code commit is required")
    selected = {}
    bound = {
        str(history_path): _hash_file(history_path),
        str(storage.securities_path): _hash_file(storage.securities_path),
        str(storage.calendar_path): _hash_file(storage.calendar_path),
    }
    for report_path in source_reports:
        source = _verified_report(report_path)
        bound[str(report_path)] = _hash_file(report_path)
        original_inputs = {
            **source["source_manifest"],
            **source.get("original_limits", source.get("original_limits_unchanged", {})),
        }
        for path, sha in original_inputs.items():
            if not Path(path).exists() or _hash_file(Path(path)) != sha:
                raise DataValidationError("cached response fixed inputs have changed")
            bound[path] = sha
        if source.get("schema") == "quantlab_historical_limit_backfill_v1":
            for item in source["rejected"]:
                path = Path(item["path"])
                if _hash_file(path) != item["sha256"]:
                    raise DataValidationError("cached diagnostic response hash mismatch")
                entry = json.loads(path.read_text(encoding="utf-8"))
                selected.setdefault(item["date"], (entry["request"], entry["normalized_records"]))
                bound[str(path)] = item["sha256"]
        elif "results" in source and source.get("canonical_writes") is False:
            for item in source["results"]:
                path = Path(item["normalized_response_path"])
                if _hash_file(path) != item["normalized_response_sha256"]:
                    raise DataValidationError("cached preflight response hash mismatch")
                rows = json.loads(path.read_text(encoding="utf-8"))
                selected.setdefault(item["date"], (item, rows))
                bound[str(path)] = item["normalized_response_sha256"]
        else:
            raise DataValidationError("unsupported cached response report")
    run = output_root / uuid4().hex
    run.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "quantlab_limit_response_revalidation_v1",
        "code_head": code_head,
        "started_at": datetime.now(UTC).isoformat(),
        "source_manifest": bound,
        "selected_dates": sorted(selected),
        "selection": "first_report_wins_per_date",
        "provider_calls": 0,
        "full_tradability_verified": False,
        "performance_eligible": False,
        "execution_authority": False,
    }
    _write_json(run / "plan.json", report)
    report.update(status="complete", accepted=[], rejected=[], reused_existing=[])
    changes = load_security_code_changes(history_path)

    def accepted(path, count):
        item = {"path": str(path), "rows": count, "sha256": _hash_file(path)}
        report["accepted"].append(item)
        _write_json(run / f"accepted-{path.stem}.json", item)

    exclusive = _ExclusiveStorage(storage.base_dir, accepted)
    try:
        for day_text, (receipt, records) in sorted(selected.items()):
            day = date.fromisoformat(day_text)
            if not date(2020, 1, 2) <= day <= date(2026, 9, 3):
                raise DataValidationError("cached limit date exceeds commissioned bounds")
            for key in ("requested_at", "observed_at"):
                if datetime.fromisoformat(receipt[key]).utcoffset() is None:
                    raise DataValidationError("cached observation time is not timezone aware")
            if datetime.fromisoformat(receipt["requested_at"]) > datetime.fromisoformat(
                receipt["observed_at"]
            ):
                raise DataValidationError("cached observation time order is invalid")
            if (
                "normalized_records_sha256" in receipt
                and canonical_payload_fingerprint({"rows": records})
                != receipt["normalized_records_sha256"]
            ):
                raise DataValidationError("cached normalized response fingerprint mismatch")
            daily_path = storage.daily_bars_path(day)
            sha = _hash_file(daily_path)
            if "daily_sha256" in receipt and sha != receipt["daily_sha256"]:
                raise DataValidationError("cached response daily input changed")
            bound[str(daily_path)] = sha
            if storage.daily_price_limit_exists(day):
                report["reused_existing"].append(day_text)
                bound[str(storage.daily_price_limit_path(day))] = _hash_file(
                    storage.daily_price_limit_path(day)
                )
                continue
            rows = [
                DailyPriceLimit(
                    **{
                        key: date.fromisoformat(value) if key == "trade_date" else _restore(value)
                        for key, value in row.items()
                    }
                )
                for row in records
            ]
            original_observation = {
                "date": day_text,
                "requested_at": receipt["requested_at"],
                "observed_at": receipt["observed_at"],
                "revalidated_at": datetime.now(UTC).isoformat(),
                "provider_called": False,
            }
            _write_json(run / f"observation-{day}.json", original_observation)
            cached = SimpleNamespace(
                get_daily_price_limits_by_date=lambda _, response=rows: response
            )
            try:
                sync_daily_price_limits(cached, exclusive, day, security_code_changes=changes)
            except (DataValidationError, DuplicateDataError) as exc:
                report["rejected"].append({"date": day_text, "error": str(exc)})
    except Exception as exc:
        report.update(status="partial_failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        bound.update({item["path"]: item["sha256"] for item in report["accepted"]})
        changed = [
            path
            for path, sha in bound.items()
            if not Path(path).exists() or _hash_file(Path(path)) != sha
        ]
        report["changed_sources"] = changed
        if changed:
            report["status"] = "source_drift"
        elif report["status"] == "complete" and report["rejected"]:
            report["status"] = "complete_with_unresolved_dates"
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["fingerprint"] = canonical_payload_fingerprint(report)
        _write_json(run / "report.json", report)
    return run / "report.json", report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, action="append", required=True)
    parser.add_argument("--authorized-canonical-write", action="store_true", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise RuntimeError("response replay requires clean code")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise RuntimeError("response replay requires pushed code")
    path, report = replay_limit_responses(
        args.source_report,
        ParquetStorage(root / "data/canonical"),
        root / "config/security_code_changes.csv",
        root / "data/products/limit_response_replay",
        code_head=head,
    )
    print(
        json.dumps(
            {
                "path": str(path),
                "status": report["status"],
                "accepted": len(report["accepted"]),
                "rejected": len(report["rejected"]),
                "provider_calls": 0,
            }
        )
    )
    if report["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
