#!/usr/bin/env python3
"""Probe and audit Systematic Lifecycle Event Data v0.

The runner is deliberately separate from the risk-policy experiment.  It
records a safe capability table first, then either performs a resumable raw
announcement sync or reports the explicit permission block without inventing
manual announcement data.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from quantlab.backtest.delisting_facts import load_delisting_facts
from quantlab.data import (
    ParquetStorage,
    TushareProvider,
    sync_lifecycle_announcement_index,
    sync_lifecycle_context,
)
from quantlab.data.lifecycle_events import (
    events_to_facts,
    golden_event_audit,
    lifecycle_coverage_rows,
    normalize_lifecycle_events,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_SCHEMA = "systematic_lifecycle_event_data_v0_1"
DECLARED_TUSHARE_POINTS = 5000
DEFAULT_START = date(2019, 1, 1)
DEFAULT_END = date(2020, 12, 31)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


def _load_raw(storage: ParquetStorage, start: date, end: date):
    rows = []
    current = start
    while current <= end:
        rows.extend(storage.load_lifecycle_announcements_by_date(current))
        current += timedelta(days=1)
    return rows


def _bars_for(
    storage: ParquetStorage, instrument_ids: set[str]
) -> dict[str, list[tuple[date, float]]]:
    result = {instrument_id: [] for instrument_id in instrument_ids}
    for path in (storage.base_dir / "daily").glob("year=*/month=*/*.parquet"):
        frame = pd.read_parquet(path, columns=["instrument_id", "trade_date", "close"])
        subset = frame[frame["instrument_id"].isin(instrument_ids)]
        for row in subset.to_dict("records"):
            result[row["instrument_id"]].append(
                (pd.Timestamp(row["trade_date"]).date(), float(row["close"]))
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit systematic lifecycle event coverage.")
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--end", type=_parse_date, default=DEFAULT_END)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be <= --end")

    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    try:
        provider = TushareProvider()
    except RuntimeError:
        provider = None
        capability = {
            endpoint: {
                "endpoint": endpoint,
                "status": "not_probed",
                "reason": "credential_not_configured",
            }
            for endpoint in ("stock_basic", "stock_st", "suspend_d", "anns_d", "namechange")
        }
        anns_available = False
        status = "blocked_by_missing_tushare_token"
    else:
        capability = provider.probe_lifecycle_capabilities(date(2020, 5, 15))
        anns_available = capability["anns_d"]["status"] == "available"
        status = (
            "ready_for_systematic_sync"
            if anns_available
            else "blocked_by_missing_anns_d_permission"
        )

    sync_result = None
    context_sync: dict[str, object] | None = None
    if provider is not None and (
        capability["stock_st"]["status"] == "available"
        and capability["suspend_d"]["status"] == "available"
    ):
        try:
            context_sync = {
                "status": "completed",
                **sync_lifecycle_context(provider, storage, args.start, args.end),
            }
        except Exception as exc:
            context_sync = {"status": "incomplete", "error_class": type(exc).__name__}
    if anns_available:
        assert provider is not None
        sync_result = sync_lifecycle_announcement_index(
            provider, storage, args.start, args.end, force=args.force
        ).__dict__
        raw = _load_raw(storage, args.start, args.end)
        calendar = [(row.trade_date, row.is_open) for row in storage.load_trading_calendar()]
        events = normalize_lifecycle_events(raw, calendar)
        storage.save_lifecycle_events(events)
    else:
        raw = []
        events = []

    manual_facts = load_delisting_facts(PROJECT_ROOT / "config" / "delisting_facts_v2.json")
    golden = golden_event_audit(manual_facts, events)
    event_facts = events_to_facts(events)
    instrument_ids = set(manual_facts) | {"002509.SZ"}
    securities = {item.instrument_id: item for item in storage.load_securities()}
    coverage = lifecycle_coverage_rows(
        sorted(instrument_ids),
        {key: value.delist_date for key, value in securities.items() if value.delist_date},
        events,
        storage.load_stock_st(),
        storage.load_suspensions(),
        _bars_for(storage, instrument_ids),
    )
    diagnostic_002509 = next(row for row in coverage if row["instrument_id"] == "002509.SZ")
    diagnostic_002509["systematic_source_status"] = status
    diagnostic_002509["candidate_count"] = sum(
        1 for item in raw if item.instrument_id == "002509.SZ"
    )
    diagnostic_002509["canonical_event_count"] = sum(
        1 for item in events if item.instrument_id == "002509.SZ"
    )

    out_dir = (
        PROJECT_ROOT / "data" / "experiments" / "lifecycle_event_data_v0_1"
        / datetime.now().strftime("%Y%m%dT%H%M%S%f")
    )
    out_dir.mkdir(parents=True, exist_ok=False)
    pd.DataFrame(golden).to_csv(out_dir / "golden_event_audit.csv", index=False)
    pd.DataFrame(coverage).to_csv(out_dir / "coverage_audit.csv", index=False)
    metadata = {
        "experiment_schema": EXPERIMENT_SCHEMA,
        "git_sha": _git_sha(),
        "declared_tushare_points": DECLARED_TUSHARE_POINTS,
        "capability": capability,
        "status": status,
        "period": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        "sync": sync_result,
        "context_sync": context_sync,
        "raw_announcement_count": len(raw),
        "canonical_event_count": len(events),
        "trusted_termination_decision_count": sum(
            1 for event in events if event.verification_status == "trusted"
        ),
        "golden": {
            "counts": pd.Series([row["status"] for row in golden]).value_counts().to_dict(),
            "rows": len(golden),
        },
        "002509_diagnostic": diagnostic_002509,
        "risk_policy_source_adapter": {
            "mode": "canonical_event_adapter_v0",
            "fact_instrument_count": len(event_facts),
            "integration_status": (
                "not_run_when_anns_d_unavailable" if not anns_available else "ready"
            ),
        },
        "limitations": [
            "ST and suspension records are context only and never liquidation triggers.",
            "Missing suspension rows are not interpreted as tradability.",
            "Daily availability is always the next open session after announcement date.",
            "Terminal settlement and delist-boundary semantics are out of scope.",
        ],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({"status": status, "output_dir": str(out_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
