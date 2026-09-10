"""Bounded missing-only ST/S-R repair using the existing context sync semantics."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from quantlab.data.models import DataValidationError, TradingCalendar, canonical_payload_fingerprint
from quantlab.data.storage import ParquetStorage
from quantlab.data.sync import sync_lifecycle_context

START = date(2025, 1, 2)
END = date(2026, 9, 3)
MAX_CALLS = 812


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    """Exclusive receipts: each filename is written at most once."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def _calendar(storage: ParquetStorage, start: date, end: date) -> tuple[bytes, list]:
    raw = storage.calendar_path.read_bytes()
    frame = pd.read_parquet(io.BytesIO(raw))
    expected = set(pd.date_range(start, end).date)
    if not {"exchange", "trade_date", "is_open"}.issubset(frame.columns):
        raise DataValidationError("incomplete calendar schema")
    parsed = pd.to_datetime(frame.trade_date, errors="raise")
    if (
        parsed.isna().any()
        or parsed.dt.tz is not None
        or not parsed.eq(parsed.dt.normalize()).all()
    ):
        raise DataValidationError("invalid calendar dates")
    frame["trade_date"] = parsed.dt.date
    observed = []
    open_dates = []
    for exchange in ("SSE", "SZSE"):
        subset = frame.loc[frame.exchange.eq(exchange) & frame.trade_date.between(start, end)]
        if (
            set(subset.trade_date) != expected
            or subset.trade_date.duplicated().any()
            or not subset.is_open.isin([True, False, 0, 1]).all()
        ):
            raise DataValidationError("missing, duplicate or unknown exchange calendar dates")
        open_dates.append(set(subset.loc[subset.is_open.eq(1), "trade_date"]))
        observed.extend(
            TradingCalendar(exchange, row.trade_date, bool(row.is_open))
            for row in subset.itertuples()
        )
    if not open_dates[0] or open_dates[0] != open_dates[1]:
        raise DataValidationError("empty or conflicting exchange calendars")
    return raw, observed


class _ExclusiveStorage(ParquetStorage):
    def __init__(self, base_dir, on_write):
        super().__init__(base_dir)
        self.on_write = on_write

    def _write(self, frame: pd.DataFrame, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        os.close(fd)
        try:
            frame.to_parquet(name, index=False)
            with open(name, "rb") as stream:
                os.fsync(stream.fileno())
            os.link(name, path)
            self.on_write(path, len(frame))
        finally:
            Path(name).unlink(missing_ok=True)
        return path


class _ObservedProvider:
    def __init__(self, factory, calendar, planned, run_dir, pause, progress):
        self.factory = factory
        self.delegate = None
        self.calendar = calendar
        self.planned = planned
        self.run_dir = run_dir
        self.pause = pause
        self.progress = progress
        self.calls = []

    def get_trading_calendar(self, start, end):
        return [row for row in self.calendar if start <= row.trade_date <= end]

    def _fetch(self, dataset, day, method):
        key = (dataset, day.isoformat())
        if key not in self.planned or key in [(x["dataset"], x["date"]) for x in self.calls]:
            raise DataValidationError("provider call escaped the missing-only plan")
        if self.delegate is None:
            self.delegate = self.factory()
        self.pause(0.4)  # At most 150 calls/minute, with no automatic retries.
        receipt = {
            "dataset": dataset,
            "date": day.isoformat(),
            "requested_at": datetime.now(UTC).isoformat(),
        }
        self.calls.append(receipt)
        try:
            items = getattr(self.delegate, method)(day)
        except Exception as exc:
            receipt.update(status="provider_failed", error_type=type(exc).__name__)
            raise RuntimeError(f"{dataset} provider request failed on {day}") from None
        else:
            ordered = sorted(
                [asdict(row) for row in items], key=lambda row: str(sorted(row.items()))
            )
            receipt.update(
                status="received_unvalidated",
                rows=len(items),
                normalized_records_sha256=canonical_payload_fingerprint({"rows": ordered}),
            )
            return items
        finally:
            receipt["observed_at"] = datetime.now(UTC).isoformat()
            _write_json(self.run_dir / f"request-{len(self.calls):04d}.json", receipt)
            if self.progress and len(self.calls) % 50 == 0:
                self.progress(len(self.calls), len(self.planned))

    def get_stock_st_by_date(self, day):
        return self._fetch("stock_st", day, "get_stock_st_by_date")

    def get_suspensions_by_date(self, day):
        return self._fetch("suspend_d", day, "get_suspensions_by_date")


def backfill_missing_context(
    provider_factory: Callable,
    storage: ParquetStorage,
    receipt_root: Path,
    *,
    code_head: str,
    start: date = START,
    end: date = END,
    max_calls: int = MAX_CALLS,
    pause: Callable = time.sleep,
    progress: Callable | None = None,
) -> tuple[Path, dict]:
    """Caller must authorize the exact provider/write scope before invoking this job.

    A partial run remains partial. Per-call receipts survive interruptions and
    a subsequent run only requests still-missing partitions. Existing files are
    reused with unknown original observation time, never assigned today's time.
    """
    if not START <= start <= end <= END:
        raise DataValidationError("backfill dates exceed the commissioned bounds")
    if not re.fullmatch(r"[0-9a-f]{40}", code_head):
        raise DataValidationError("a full source commit is required")
    if type(max_calls) is not int or not 0 <= max_calls <= MAX_CALLS:
        raise DataValidationError("invalid provider call budget")
    calendar_raw, calendar = _calendar(storage, start, end)
    days = sorted({row.trade_date for row in calendar if row.is_open})
    paths = {
        (dataset, day.isoformat()): path_fn(day)
        for dataset, path_fn in (
            ("stock_st", storage.stock_st_v1_path),
            ("suspend_d", storage.suspensions_v1_path),
        )
        for day in days
    }
    planned = {key for key, path in paths.items() if not path.exists()}
    if len(planned) > max_calls:
        raise DataValidationError("missing partition count exceeds the provider call budget")
    existing = {
        str(path): _hash_file(path)
        for dataset in ("stock_st", "suspensions")
        for path in sorted((storage.base_dir / "lifecycle_context_v1" / dataset).rglob("*.parquet"))
    }
    run_dir = receipt_root / uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "quantlab_missing_context_backfill_v1",
        "code_head": code_head,
        "started_at": datetime.now(UTC).isoformat(),
        "start": str(start),
        "end": str(end),
        "calendar_sha256": hashlib.sha256(calendar_raw).hexdigest(),
        "expected_sessions": len(days),
        "planned_calls": len(planned),
        "existing_manifest": existing,
        "planned": sorted(planned),
        "historical_revision_status": "unverified",
        "full_tradability_verified": False,
        "performance_eligible": False,
        "execution_authority": False,
        "original_observation_time_of_reused_data": "not_established_by_this_run",
        "sources": [
            "https://tushare.pro/document/2?doc_id=397",
            "https://tushare.pro/document/2?doc_id=214",
        ],
    }
    _write_json(run_dir / "plan.json", report)
    provider = _ObservedProvider(provider_factory, calendar, planned, run_dir, pause, progress)
    report["status"] = "complete"
    report["accepted"] = []
    by_path = {path: key for key, path in paths.items()}

    def accepted(path, rows):
        dataset, day = by_path[path]
        receipt = {
            "dataset": dataset,
            "date": day,
            "rows": rows,
            "path": str(path),
            "sha256": _hash_file(path),
        }
        _write_json(run_dir / f"accepted-{dataset}-{day}.json", receipt)
        report["accepted"].append(receipt)

    exclusive = _ExclusiveStorage(storage.base_dir, accepted)
    try:
        for day in days:
            if storage.calendar_path.read_bytes() != calendar_raw:
                raise DataValidationError("calendar changed during backfill")
            results = sync_lifecycle_context(provider, exclusive, day, day)
            for dataset, result in results.items():
                if not result.complete:
                    raise DataValidationError(
                        f"{dataset} response incomplete or truncated on {day}"
                    )
    except Exception as exc:
        report.update(status="partial_failed", error_type=type(exc).__name__)
        if isinstance(exc, (DataValidationError, RuntimeError)):
            report["error"] = str(exc)  # Locally generated messages; provider details suppressed.
    finally:
        changed = [
            path
            for path, sha in existing.items()
            if not Path(path).exists() or _hash_file(Path(path)) != sha
        ]
        changed_new = [
            item["path"]
            for item in report["accepted"]
            if not Path(item["path"]).exists() or _hash_file(Path(item["path"])) != item["sha256"]
        ]
        if (
            changed
            or changed_new
            or not storage.calendar_path.exists()
            or storage.calendar_path.read_bytes() != calendar_raw
        ):
            report.update(
                status="source_drift", changed_existing_paths=changed, changed_new_paths=changed_new
            )
        report["provider_calls"] = provider.calls
        report["missing_after"] = [key for key, path in paths.items() if not path.exists()]
        if report["missing_after"] and report["status"] == "complete":
            report["status"] = "partial_missing"
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["fingerprint"] = canonical_payload_fingerprint(report)
        _write_json(run_dir / "report.json", report)
    return run_dir / "report.json", report


def main() -> None:
    from quantlab.data.tushare_provider import TushareProvider

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized-provider-write", action="store_true", required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise RuntimeError("backfill requires a clean worktree")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != upstream:
        raise RuntimeError("backfill requires pushed code")
    path, report = backfill_missing_context(
        TushareProvider,
        ParquetStorage(root / "data/canonical"),
        root / "data/products/context_backfill",
        code_head=head,
        progress=lambda done, total: print(f"Context requests: {done}/{total}", flush=True),
    )
    print(
        json.dumps(
            {
                "path": str(path),
                "status": report["status"],
                "calls": len(report["provider_calls"]),
                "accepted": len(report["accepted"]),
                "missing": len(report["missing_after"]),
            }
        )
    )
    if report["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
