"""Missing-only historical stock price limits; unresolved responses stay diagnostic."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from quantlab.data.context_backfill import _calendar, _ExclusiveStorage, _hash_file, _write_json
from quantlab.data.enrichment import sync_daily_price_limits
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import DuplicateDataError, ParquetStorage

START = date(2020, 1, 2)
END = date(2026, 9, 3)
MAX_CALLS = 1618


def _safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite": str(value)}
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def backfill_price_limits(
    provider_factory: Callable,
    storage: ParquetStorage,
    history_path: Path,
    receipt_root: Path,
    *,
    code_head: str,
    start: date = START,
    end: date = END,
    max_calls: int = MAX_CALLS,
    pause: Callable = time.sleep,
    progress: Callable | None = None,
) -> tuple[Path, dict]:
    import re

    if not START <= start <= end <= END:
        raise DataValidationError("price-limit backfill exceeds commissioned dates")
    if not re.fullmatch(r"[0-9a-f]{40}", code_head):
        raise DataValidationError("a full code commit is required")
    if type(max_calls) is not int or not 0 <= max_calls <= MAX_CALLS:
        raise DataValidationError("invalid price-limit provider call budget")
    calendar_bytes, calendar = _calendar(storage, start, end)
    days = sorted({item.trade_date for item in calendar if item.is_open})
    missing = [day for day in days if not storage.daily_price_limit_exists(day)]
    if len(missing) > max_calls:
        raise DataValidationError("price-limit missing dates exceed call budget")
    sources = {
        str(storage.calendar_path): hashlib.sha256(calendar_bytes).hexdigest(),
        str(history_path): _hash_file(history_path),
        str(storage.securities_path): _hash_file(storage.securities_path),
    }
    changes = load_security_code_changes(history_path)
    originals = {
        str(path): _hash_file(path)
        for path in storage.base_dir.glob("daily_price_limit/year=*/month=*/*.parquet")
    }
    run = receipt_root / uuid4().hex
    run.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "quantlab_historical_limit_backfill_v1",
        "code_head": code_head,
        "started_at": datetime.now(UTC).isoformat(),
        "start": str(start),
        "end": str(end),
        "source_manifest": sources,
        "original_limits": originals,
        "planned_dates": missing,
        "historical_revision_status": "unverified",
        "full_tradability_verified": False,
        "performance_eligible": False,
        "execution_authority": False,
        "source": "https://tushare.pro/document/2?doc_id=183",
        "units": "price limits in CNY; raw reference facts, no inferred unlimited session",
    }
    _write_json(run / "plan.json", report)
    report.update(status="complete", accepted=[], rejected=[], provider_calls=0)

    def accepted(path, row_count):
        entry = {"path": str(path), "rows": row_count, "sha256": _hash_file(path)}
        _write_json(run / f"accepted-{path.stem}.json", entry)
        report["accepted"].append(entry)

    exclusive = _ExclusiveStorage(storage.base_dir, accepted)
    provider = None
    try:
        for day in missing:
            if any(
                not Path(path).exists() or _hash_file(Path(path)) != sha
                for path, sha in sources.items()
            ):
                raise DataValidationError("fixed source changed during limit backfill")
            if storage.daily_price_limit_exists(day):
                raise DataValidationError("price-limit partition appeared after the plan was bound")
            daily_path = storage.daily_bars_path(day)
            daily_sha = _hash_file(daily_path)
            if provider is None:
                try:
                    provider = provider_factory()
                except Exception:
                    raise RuntimeError("price-limit provider initialization failed") from None
            pause(0.4)
            receipt = {
                "date": str(day),
                "requested_at": datetime.now(UTC).isoformat(),
                "daily_path": str(daily_path),
                "daily_sha256": daily_sha,
            }
            report["provider_calls"] += 1
            try:
                rows = provider.get_daily_price_limits_by_date(day)
            except KeyboardInterrupt:
                receipt.update(status="interrupted_request_outcome_unknown")
                raise
            except Exception as exc:
                receipt.update(status="provider_failed", error_type=type(exc).__name__)
                raise RuntimeError(f"stk_limit provider request failed on {day}") from None
            else:
                records = _safe([asdict(item) for item in rows])
                receipt.update(
                    status="received_unvalidated",
                    rows=len(rows),
                    normalized_records_sha256=canonical_payload_fingerprint({"rows": records}),
                )
            finally:
                receipt["observed_at"] = datetime.now(UTC).isoformat()
                _write_json(run / f"request-{day}.json", receipt)
            report.setdefault("daily_inputs", {})[str(daily_path)] = daily_sha
            # Bind this exact response to the strict, history-aware existing sync path.
            cached = SimpleNamespace(
                get_daily_price_limits_by_date=lambda _, response=rows: response
            )
            try:
                if _hash_file(daily_path) != daily_sha:
                    raise RuntimeError("daily partition changed during provider call")
                result = sync_daily_price_limits(
                    cached, exclusive, day, security_code_changes=changes
                )
                if result.status != "synced":
                    raise RuntimeError("price-limit partition was concurrently published")
            except (DataValidationError, DuplicateDataError) as exc:
                entry = {
                    "date": str(day),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "request": receipt,
                    "normalized_records": records,
                    "canonical_accepted": False,
                    "tradability": "unknown",
                }
                path = run / f"rejected-{day}.json"
                _write_json(path, entry)
                report["rejected"].append(
                    {
                        "date": str(day),
                        "path": str(path),
                        "sha256": _hash_file(path),
                        "error": str(exc),
                    }
                )
                if "unverified historical A-share identifiers" in str(exc):
                    raise RuntimeError(
                        "unverified historical identity requires scope review"
                    ) from exc
            if progress and report["provider_calls"] % 50 == 0:
                progress(report["provider_calls"], len(missing), len(report["rejected"]))
    except KeyboardInterrupt:
        report.update(status="interrupted", error_type="KeyboardInterrupt")
    except Exception as exc:
        report.update(status="partial_failed", error_type=type(exc).__name__)
        if isinstance(exc, (DataValidationError, RuntimeError)):
            report["error"] = str(exc)
    finally:
        bound = {**sources, **report.get("daily_inputs", {}), **originals}
        bound.update(
            {item["path"]: item["sha256"] for item in report["accepted"] + report["rejected"]}
        )
        changed = [
            path
            for path, sha in bound.items()
            if not Path(path).exists() or _hash_file(Path(path)) != sha
        ]
        report["changed_sources"] = changed
        report["missing_after"] = [
            str(day) for day in days if not storage.daily_price_limit_exists(day)
        ]
        if changed:
            report["status"] = "source_drift"
        elif report["status"] == "complete" and report["missing_after"]:
            report["status"] = "complete_with_unresolved_dates"
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["fingerprint"] = canonical_payload_fingerprint(report)
        _write_json(run / "report.json", report)
    return run / "report.json", report


def main() -> None:
    from quantlab.data.tushare_provider import TushareProvider

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized-provider-write", action="store_true", required=True)
    parser.add_argument("--max-provider-calls", type=int, default=MAX_CALLS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise RuntimeError("price-limit backfill requires a clean worktree")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != upstream:
        raise RuntimeError("price-limit backfill requires pushed code")
    path, report = backfill_price_limits(
        TushareProvider,
        ParquetStorage(root / "data/canonical"),
        root / "config/security_code_changes.csv",
        root / "data/products/limit_backfill",
        code_head=head,
        max_calls=args.max_provider_calls,
        progress=lambda done, total, rejected: print(
            f"Limit requests: {done}/{total}; unresolved: {rejected}", flush=True
        ),
    )
    print(
        json.dumps(
            {
                "path": str(path),
                "status": report["status"],
                "calls": report["provider_calls"],
                "accepted": len(report["accepted"]),
                "rejected": len(report["rejected"]),
                "missing": len(report["missing_after"]),
            }
        )
    )
    if report["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
