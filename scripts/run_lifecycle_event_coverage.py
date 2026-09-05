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
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from quantlab.backtest.delisting_facts import load_delisting_facts
from quantlab.backtest.provenance import content_manifest
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
DEFAULT_START = date(2020, 1, 1)
DEFAULT_END = date(2024, 12, 31)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


def _git_dirty() -> bool:
    try:
        return bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True
        ).strip())
    except Exception:
        return True


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


def _context_rows(storage: ParquetStorage, dates: list[date]):
    stock_st = []
    suspensions = []
    for trade_date in dates:
        stock_st.extend(storage.load_stock_st_v1_by_date(trade_date))
        suspensions.extend(storage.load_suspensions_v1_by_date(trade_date))
    return stock_st, suspensions


def _context_paths(storage: ParquetStorage, dates: list[date]) -> list[Path]:
    paths = [storage.securities_path, storage.calendar_path]
    for trade_date in dates:
        for path in (storage.stock_st_v1_path(trade_date), storage.suspensions_v1_path(trade_date)):
            if path.exists():
                paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit systematic lifecycle event coverage.")
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--end", type=_parse_date, default=DEFAULT_END)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be <= --end")

    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    calendar_rows = storage.load_trading_calendar()
    expected_dates = sorted({
        row.trade_date for row in calendar_rows
        if row.is_open and args.start <= row.trade_date <= args.end
    })
    if not expected_dates:
        raise RuntimeError("canonical calendar has no open sessions for requested context range")
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
                key: asdict(value)
                for key, value in sync_lifecycle_context(
                    provider, storage, args.start, args.end, force=args.force
                ).items()
            }
        except Exception as exc:
            context_sync = {"status": "error", "error_class": type(exc).__name__}
    if anns_available:
        assert provider is not None
        sync_result = sync_lifecycle_announcement_index(
            provider, storage, args.start, args.end, force=args.force
        ).__dict__
        raw = _load_raw(storage, args.start, args.end)
        calendar = [(row.trade_date, row.is_open) for row in calendar_rows]
        events = normalize_lifecycle_events(raw, calendar)
        storage.save_lifecycle_events(events)
    else:
        raw = []
        events = []

    manual_facts = load_delisting_facts(PROJECT_ROOT / "config" / "delisting_facts_v2.json")
    name_changes = []
    namechange_status = "not_run"
    if provider is not None and capability["namechange"]["status"] == "available":
        try:
            for instrument_id in sorted(set(manual_facts) | {"002509.SZ"}):
                name_changes.extend(provider.get_name_changes(instrument_id, args.start, args.end))
            storage.save_name_changes_v1(name_changes)
            namechange_status = "completed"
        except Exception as exc:
            namechange_status = f"error:{type(exc).__name__}"
    stock_st, suspensions = _context_rows(storage, expected_dates)
    golden = golden_event_audit(manual_facts, events)
    event_facts = events_to_facts(events)
    instrument_ids = set(manual_facts) | {"002509.SZ"}
    securities = {item.instrument_id: item for item in storage.load_securities()}
    coverage = lifecycle_coverage_rows(
        sorted(instrument_ids),
        {key: value.delist_date for key, value in securities.items() if value.delist_date},
        events,
        stock_st,
        suspensions,
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
    st_002509 = [row for row in stock_st if row.instrument_id == "002509.SZ"]
    suspension_002509 = [row for row in suspensions if row.instrument_id == "002509.SZ"]
    diagnostic_002509["st_summary"] = {
        "first_observed": min((row.trade_date for row in st_002509), default=None),
        "last_observed": max((row.trade_date for row in st_002509), default=None),
        "types": sorted({row.status for row in st_002509 if row.status}),
        "type_names": sorted({row.type_name for row in st_002509 if row.type_name}),
        "names": sorted({row.name for row in st_002509 if row.name}),
    }
    diagnostic_002509["suspension_summary"] = {
        "confirmed_s_dates": [
            row.trade_date for row in suspension_002509 if row.suspend_type == "S"
        ],
        "resume_event_dates": [
            row.trade_date for row in suspension_002509 if row.suspend_type == "R"
        ],
    }
    diagnostic_002509["relevant_name_changes"] = [
        asdict(row) for row in storage.load_name_changes_v1()
        if row.instrument_id == "002509.SZ"
    ]

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
        "workspace_dirty": _git_dirty(),
        "code_manifest": content_manifest(
            sorted((PROJECT_ROOT / "src" / "quantlab").rglob("*.py"))
            + [Path(__file__)],
            PROJECT_ROOT,
        ),
        "declared_tushare_points": DECLARED_TUSHARE_POINTS,
        "capability": capability,
        "status": status,
        "period": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        "sync": sync_result,
        "context_sync": context_sync,
        "termination_announcement_source_readiness": status,
        "lifecycle_context_readiness": (
            "complete_for_2020_2024"
            if context_sync
            and all(
                isinstance(context_sync.get(dataset), dict)
                and context_sync[dataset].get("complete")
                for dataset in ("stock_st", "suspend_d")
            )
            else "incomplete"
        ),
        "namechange_audit": {"status": namechange_status, "row_count": len(name_changes)},
        "dataset_schema_versions": {
            "stock_st": "stock_st_daily_v1",
            "suspensions": "suspensions_daily_v1",
        },
        "data_input_manifest": content_manifest(
            _context_paths(storage, expected_dates), PROJECT_ROOT
        ),
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
