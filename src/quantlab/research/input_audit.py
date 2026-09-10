"""Bounded local input quality and lineage audit; no labels, models or provider calls."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT, SHANGHAI
from quantlab.data.storage import ParquetStorage

# These are publication eligibility boundaries, not current constituent lists.
INDEX_AVAILABLE_FROM = {
    "000300.SH": date(2005, 4, 8),
    "000905.SH": date(2007, 1, 15),
    "000852.SH": date(2014, 10, 17),
    "000001.SH": date(1991, 7, 15),
    "000688.SH": date(2020, 7, 23),
}
OHLC = ["open", "high", "low", "close"]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_research_inputs(
    storage: ParquetStorage,
    start: date,
    end: date,
    *,
    warmup_sessions: int = 120,
    code_changes_path: Path = PROJECT_ROOT / "config/security_code_changes.csv",
    code_head: str | None = None,
    progress=None,
) -> dict:
    """Stream daily grains and preserve every inspected source fingerprint.

    A physically present ST/suspension file does not establish the completeness
    of the provider response. Current security attributes do not establish PIT
    board membership. This audit cannot make a trading or performance claim.
    """
    if (
        type(start) is not date
        or type(end) is not date
        or not start <= end <= datetime.now(SHANGHAI).date()
    ):
        raise ValueError("audit dates must be ordered past/present dates")
    if type(warmup_sessions) is not int or not 0 <= warmup_sessions <= 252:
        raise ValueError("warmup_sessions must be an integer from 0 to 252")
    findings, manifest = [], []
    totals = defaultdict(lambda: {"partitions": 0, "rows": 0})
    paths = {}

    def finding(code, dataset, severity, **details):
        findings.append({"code": code, "dataset": dataset, "severity": severity, **details})

    def read(path, dataset, *, day=None, required=(), keys=()):
        label = (
            path.relative_to(storage.base_dir).as_posix()
            if path.is_relative_to(storage.base_dir)
            else "repo/config/security_code_changes.csv"
        )
        if not path.exists():
            finding("missing_file", dataset, "high", path=label, date=str(day) if day else None)
            return None
        raw = path.read_bytes()
        entry = {"path": label, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        paths[label] = path
        manifest.append(entry)
        try:
            frame = (
                pd.read_csv(io.BytesIO(raw))
                if path.suffix == ".csv"
                else pd.read_parquet(io.BytesIO(raw), use_threads=False)
            )
        except Exception as exc:
            finding(
                "unreadable_table", dataset, "critical", path=label, error_type=type(exc).__name__
            )
            return None
        entry["rows"] = len(frame)
        entry["columns"] = list(frame.columns)
        totals[dataset]["partitions"] += 1
        totals[dataset]["rows"] += len(frame)
        if frame.empty and dataset not in {"stock_st", "suspensions", "code_changes"}:
            finding("empty_required_table", dataset, "high", path=label)
        missing = sorted(set(required) - set(frame.columns))
        if missing:
            finding("missing_columns", dataset, "critical", path=label, columns=missing)
            return None
        if keys:
            nulls = frame[list(keys)].isna().any(axis=1)
            duplicates = frame.duplicated(list(keys), keep=False)
            empty_ids = (
                frame["instrument_id"].astype(str).str.strip().eq("")
                if "instrument_id" in keys
                else pd.Series(False, index=frame.index)
            )
            if (nulls | duplicates | empty_ids).any():
                finding(
                    "invalid_or_duplicate_keys",
                    dataset,
                    "critical",
                    path=label,
                    null_rows=int(nulls.sum()),
                    duplicate_rows=int(duplicates.sum()),
                    empty_id_rows=int(empty_ids.sum()),
                )
        if day is not None:
            observed = pd.to_datetime(frame["trade_date"], errors="coerce")
            wrong = (
                observed.isna() | observed.dt.date.ne(day) | observed.ne(observed.dt.normalize())
            )
            if wrong.any():
                finding(
                    "wrong_partition_date", dataset, "critical", path=label, rows=int(wrong.sum())
                )
        return frame

    calendar = read(
        storage.calendar_path,
        "calendar",
        required=("exchange", "trade_date", "is_open"),
        keys=("exchange", "trade_date"),
    )
    securities = read(
        storage.securities_path,
        "securities",
        required=("instrument_id", "list_date", "delist_date", "board"),
        keys=("instrument_id",),
    )
    changes = read(
        code_changes_path,
        "code_changes",
        required=("old_instrument_id", "new_instrument_id", "effective_date", "original_list_date"),
    )
    sessions = []
    target_sessions = []
    if calendar is not None:
        parsed = pd.to_datetime(calendar["trade_date"], errors="coerce")
        valid_flags = calendar["is_open"].map(
            lambda value: (
                type(value) in (bool, np.bool_)
                or type(value) in (int, np.int64)
                and value in (0, 1)
            )
        )
        if parsed.isna().any() or not valid_flags.all():
            finding("invalid_calendar_values", "calendar", "critical")
        usable = calendar.assign(trade_date=parsed.dt.date)
        usable = usable[parsed.notna() & valid_flags & usable["exchange"].isin(["SSE", "SZSE"])]
        opens = sorted(set(usable.loc[usable["is_open"].eq(True), "trade_date"]))
        target_sessions = [day for day in opens if start <= day <= end]
        if target_sessions:
            first = opens.index(target_sessions[0])
            if first < warmup_sessions:
                finding(
                    "insufficient_calendar_warmup",
                    "calendar",
                    "high",
                    available=first,
                    required=warmup_sessions,
                )
            sessions = opens[max(0, first - warmup_sessions) : opens.index(target_sessions[-1]) + 1]
            interval_start = min(start, sessions[0])
        else:
            interval_start = start
            finding("no_observed_open_sessions", "calendar", "high")
        observed = {(row.exchange, row.trade_date): row.is_open for row in usable.itertuples()}
        missing_days, conflicts = [], []
        for offset in range((end - interval_start).days + 1):
            day = interval_start + timedelta(days=offset)
            if any((exchange, day) not in observed for exchange in ("SSE", "SZSE")):
                missing_days.append(day.isoformat())
            elif observed[("SSE", day)] != observed[("SZSE", day)]:
                conflicts.append(day.isoformat())
        if missing_days:
            finding("unverified_calendar_days", "calendar", "critical", dates=missing_days)
        if conflicts:
            finding("exchange_calendar_conflict", "calendar", "critical", dates=conflicts)
    known_ids = set(securities["instrument_id"]) if securities is not None else set()
    if changes is not None:
        known_ids.update(changes["old_instrument_id"].dropna())
        known_ids.update(changes["new_instrument_id"].dropna())
    missing_indices = defaultdict(list)
    missing_context = defaultdict(list)
    core = {
        "daily": (storage.daily_bars_path, OHLC + ["volume", "amount"]),
        "adj_factor": (storage.adj_factor_path, ["adj_factor"]),
        "daily_basic": (storage.daily_basic_path, ["turnover_rate", "total_mv", "circ_mv"]),
        "index_daily": (storage.index_daily_path, OHLC),
    }
    context = {
        "stock_st": storage.stock_st_v1_path,
        "suspensions": storage.suspensions_v1_path,
        "daily_price_limit": storage.daily_price_limit_path,
    }
    for session_number, day in enumerate(sessions, start=1):
        tables = {}
        for dataset, (path_fn, numeric_columns) in core.items():
            frame = read(
                path_fn(day),
                dataset,
                day=day,
                required=["instrument_id", "trade_date", *numeric_columns],
                keys=("instrument_id", "trade_date"),
            )
            if frame is None:
                continue
            tables[dataset] = frame
            values = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
            invalid = ~np.isfinite(values).all(axis=1)
            if dataset in ("daily", "index_daily"):
                invalid |= (values[OHLC] <= 0).any(axis=1)
                invalid |= values["high"] < values[["open", "close", "low"]].max(axis=1)
                invalid |= values["low"] > values[["open", "close", "high"]].min(axis=1)
                if dataset == "daily":
                    invalid |= (values[["volume", "amount"]] < 0).any(axis=1)
            elif dataset == "adj_factor":
                invalid |= values["adj_factor"] <= 0
            else:
                invalid |= (values < 0).any(axis=1)
                invalid |= (values[["circ_mv", "total_mv"]] <= 0).any(axis=1)
                invalid |= values["circ_mv"] > values["total_mv"] + 1
            if invalid.any():
                finding(
                    "invalid_numeric_rows",
                    dataset,
                    "high",
                    date=day.isoformat(),
                    rows=int(invalid.sum()),
                    instruments=sorted(set(frame.loc[invalid, "instrument_id"].astype(str))),
                )
        if "daily" in tables:
            ids = set(tables["daily"]["instrument_id"])
            unknown = ids - known_ids
            if unknown:
                finding(
                    "unmapped_security_ids",
                    "daily",
                    "high",
                    date=day.isoformat(),
                    instruments=sorted(map(str, unknown)),
                )
            for dataset in ("adj_factor", "daily_basic"):
                if dataset in tables:
                    missing = ids - set(tables[dataset]["instrument_id"])
                    if missing:
                        finding(
                            "unmatched_daily_keys",
                            dataset,
                            "high",
                            date=day.isoformat(),
                            instruments=sorted(map(str, missing)),
                        )
        if day >= start:
            index_ids = (
                set(tables["index_daily"]["instrument_id"]) if "index_daily" in tables else set()
            )
            for index, available_from in INDEX_AVAILABLE_FROM.items():
                if day >= available_from and index not in index_ids:
                    missing_indices[index].append(day.isoformat())
            for dataset, path_fn in context.items():
                if not path_fn(day).exists():
                    missing_context[dataset].append(day.isoformat())
                    continue
                keys = (
                    ("instrument_id", "trade_date")
                    if dataset == "daily_price_limit"
                    else ("instrument_id", "trade_date", "source_record_id")
                )
                read(path_fn(day), dataset, day=day, required=keys, keys=keys)
        if progress is not None and session_number % 250 == 0:
            progress(session_number, len(sessions))
    for index, days in missing_indices.items():
        finding(
            "missing_published_index_sessions",
            "index_daily",
            "high",
            instrument_id=index,
            dates=days,
        )
    for dataset, days in missing_context.items():
        finding("missing_context_partitions", dataset, "high", dates=days)
    # Detect source changes during the audit without taking ownership of data files.
    for entry in manifest:
        path = paths[entry["path"]]
        final_sha = _sha(path) if path.exists() else None
        if final_sha != entry["sha256"]:
            finding(
                "source_changed_during_audit",
                "lineage",
                "critical",
                path=entry["path"],
                final_sha256=final_sha,
            )
    result = {
        "schema": "quantlab_research_input_audit_v1",
        "code_head": code_head,
        "generated_at": datetime.now(SHANGHAI).isoformat(),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "warmup_sessions": warmup_sessions,
        "read_start": sessions[0].isoformat() if sessions else None,
        "target_session_count": len(target_sessions),
        "inspected_session_count": len(sessions),
        "status": "needs_input_work" if findings else "locally_consistent_with_limitations",
        "totals": dict(totals),
        "findings": findings,
        "source_manifest": manifest,
        "index_available_from": {
            key: value.isoformat() for key, value in INDEX_AVAILABLE_FROM.items()
        },
        "performance_eligible": False,
        "execution_authority": False,
        "limitations": [
            "source rows and joins are audited; publication and revision histories are unverified",
            "physical ST/suspension/limit files do not establish complete historical tradability",
            "current security board fields are not historical board membership evidence",
            "corporate-action postings, dividend tax and complete trading costs are unverified",
            "no labels, signal efficacy, net returns, models or fresh out-of-sample claim",
        ],
    }
    result["content_fingerprint"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 10))
    args = parser.parse_args()
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True
    ).strip():
        raise ValueError("real input audit requires clean committed code")
    code_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=PROJECT_ROOT, text=True
    ).strip()
    if code_head != upstream:
        raise ValueError("real input audit requires the current code to be pushed")
    result = audit_research_inputs(
        ParquetStorage(PROJECT_ROOT / "data/canonical"),
        args.start,
        args.end,
        code_head=code_head,
        progress=lambda done, total: print(
            f"inspected sessions: {done}/{total}", file=sys.stderr, flush=True
        ),
    )
    root = PROJECT_ROOT / "data/products/research_input_audit" / result["content_fingerprint"]
    root.mkdir(parents=True, exist_ok=True)
    path = root / "audit.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"path": str(path), "status": result["status"], "findings": len(result["findings"])}
        )
    )


if __name__ == "__main__":
    main()
