"""Resumable, content-verified daily ingestion. A receipt commits the whole session."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from quantlab.daily.update import _load_or_fetch_core
from quantlab.data.models import DataValidationError
from quantlab.data.sync import validate_index_daily_bars
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.io import sha256, write_json


def session_files(storage, day):
    return {
        "daily": storage.daily_bars_path(day),
        "adj_factor": storage.adj_factor_path(day),
        "daily_basic": storage.daily_basic_path(day),
        "index": storage.index_daily_path(day),
        "limits": storage.daily_price_limit_path(day),
        "st": storage.stock_st_v1_path(day),
        "suspensions": storage.suspensions_v1_path(day),
    }


def verify_session(storage, receipts, day):
    path = Path(receipts) / "sessions" / f"{day}.json"
    receipt = json.loads(path.read_text())
    if receipt.get("session") != str(day) or receipt.get("schema") != "quantlab_ingestion_v1":
        raise ValueError("invalid ingestion receipt")
    files = session_files(storage, day)
    if set(receipt["files"]) != set(files):
        raise ValueError("incomplete ingestion receipt")
    for name, file in files.items():
        if not file.is_file() or sha256(file) != receipt["files"][name]:
            raise ValueError(f"canonical partition changed:{day}:{name}")
    if receipt.get("context_validation") != "scoped_below_provider_cap_v1":
        from quantlab.data.sync import _validate_context_rows

        _validate_context_rows(storage.load_stock_st_v1_by_date(day), day, "stock_st", 1000)
        _validate_context_rows(storage.load_suspensions_v1_by_date(day), day, "suspend_d", 5000)
    metadata = Path(receipts) / "metadata" / f"{receipt['metadata_sha256']}.json"
    if sha256(metadata) != receipt["metadata_sha256"]:
        raise ValueError("metadata snapshot changed")
    for path, expected in receipt.get("raw_responses", {}).items():
        if sha256(path) != expected:
            raise ValueError("raw provider response changed")
    return receipt


def _commit_pending(storage, receipts, day):
    pending = receipts / "pending" / str(day)
    receipt = json.loads((pending / "receipt.json").read_text())
    for name, path in session_files(storage, day).items():
        source = pending / f"{name}.parquet"
        if sha256(source) != receipt["files"][name]:
            raise ValueError("prepared ingestion payload changed")
        if path.exists():
            if sha256(path) != receipt["files"][name]:
                raise ValueError("canonical conflicts with prepared ingestion")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.pending")
            try:
                shutil.copyfile(source, temporary)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    marker = receipts / "sessions" / f"{day}.json"
    if not marker.exists():
        write_json(marker, receipt)
    verify_session(storage, receipts, day)
    shutil.rmtree(pending)


def calendar_days(calendar, start, end):
    mapping = {(x.exchange, x.trade_date): x.is_open for x in calendar}
    if len(mapping) != len(calendar):
        raise DataValidationError("duplicate exchange calendar identities")
    days, day = [], start
    while day <= end:
        states = [mapping.get((exchange, day)) for exchange in ("SSE", "SZSE")]
        if None in states or states[0] != states[1]:
            raise DataValidationError(f"missing/disagreeing exchange calendar:{day}")
        if states[0]:
            days.append(day)
        day += timedelta(days=1)
    return days


def synchronize(provider, storage, receipts, start, end, *, indices, adopt_existing=False):
    """Explicit caller commission only. Never call this from plan/status/features.

    Missing or invalid payloads cannot advance the complete-session watermark.
    Previously committed partitions are verified and never refreshed in place.
    Prepared transactions recover automatically. Legacy unreceipted files require
    explicit adoption; their first observation is never backdated.
    """
    receipts = Path(receipts)
    if start > end or end > datetime.now(ZoneInfo("Asia/Shanghai")).date() or not indices:
        raise ValueError("invalid ingestion interval/indices")
    with exclusive_job(receipts):
        calendar = provider.get_trading_calendar(start, end + timedelta(days=35))
        days = calendar_days(calendar, start, end)
        securities = provider.get_securities()
        if not securities or len({s.instrument_id for s in securities}) != len(securities):
            raise DataValidationError("empty/ambiguous security master")
        # Keep each metadata observation immutable while maintaining the legacy store.
        snapshot = {
            "observed_at": datetime.now(UTC).isoformat(),
            "raw_responses": dict(getattr(getattr(provider, "_pro", None), "records", [])),
            "securities": [asdict(s) for s in securities],
            "calendar": [asdict(c) for c in calendar],
        }
        snapshot = json.loads(json.dumps(snapshot, default=str))
        temp = receipts / f"metadata-{uuid4().hex}.pending"
        write_json(temp, snapshot)
        digest = sha256(temp)
        write_json(receipts / "metadata" / f"{digest}.json", snapshot)
        temp.unlink()
        storage.upsert_securities(securities)
        storage.upsert_trading_calendar(calendar)
        completed = []
        for day in days:
            marker = receipts / "sessions" / f"{day}.json"
            if marker.exists():
                verify_session(storage, receipts, day)
                if not set(indices).issubset(
                    {r.instrument_id for r in storage.load_index_daily_by_date(day)}
                ):
                    raise DataValidationError(
                        "sealed session lacks requested benchmark; use a new data namespace"
                    )
                completed.append(str(day))
                continue
            pending = receipts / "pending" / str(day)
            if pending.exists():
                _commit_pending(storage, receipts, day)
                completed.append(str(day))
                continue
            raw_start = len(getattr(getattr(provider, "_pro", None), "records", []))
            files = session_files(storage, day)
            reused = [name for name, path in files.items() if path.exists()]
            if reused and not adopt_existing:
                raise ValueError(f"unreceipted partitions:{day}; review then use --adopt-existing")
            bars, factors, basics = _load_or_fetch_core(provider, storage, day)
            index = (
                storage.load_index_daily_by_date(day)
                if files["index"].exists()
                else [row for code in indices for row in provider.get_index_daily(code, day, day)]
            )
            validate_index_daily_bars(index, day)
            if {r.instrument_id for r in index} != set(indices):
                raise DataValidationError("incomplete benchmark coverage")
            limits = (
                storage.load_daily_price_limits_by_date(day)
                if files["limits"].exists()
                else provider.get_daily_price_limits_by_date(day)
            )
            st = (
                storage.load_stock_st_v1_by_date(day)
                if files["st"].exists()
                else provider.get_stock_st_by_date(day)
            )
            suspensions = (
                storage.load_suspensions_v1_by_date(day)
                if files["suspensions"].exists()
                else provider.get_suspensions_by_date(day)
            )
            from quantlab.data.sync import _validate_context_rows

            # Adoption is not a way around provider completeness checks either.
            _validate_context_rows(st, day, "stock_st", 1000)
            _validate_context_rows(suspensions, day, "suspend_d", 5000)
            # Storage validates limits/context; stage them away from Canonical first.
            from tempfile import TemporaryDirectory

            from quantlab.data.storage import ParquetStorage

            with TemporaryDirectory(dir=receipts) as scratch:
                stage = ParquetStorage(scratch)
                saves = (
                    ("daily", "save_daily_bars_by_date", bars),
                    ("adj_factor", "save_adj_factors_by_date", factors),
                    ("daily_basic", "save_daily_basic_by_date", basics),
                    ("index", "save_index_daily_by_date", index),
                    ("limits", "save_daily_price_limits_by_date", limits),
                    ("st", "save_stock_st_v1_by_date", st),
                    ("suspensions", "save_suspensions_v1_by_date", suspensions),
                )
                for _, method, rows in saves:
                    getattr(stage, method)(rows, day)
                if not {r.instrument_id for r in bars}.issubset({r.instrument_id for r in limits}):
                    raise DataValidationError("missing daily price-limit evidence")
                ready = Path(scratch) / "ready"
                ready.mkdir()
                staged_files = session_files(stage, day)
                for name in files:
                    shutil.copyfile(
                        files[name] if name in reused else staged_files[name],
                        ready / f"{name}.parquet",
                    )
                records = getattr(getattr(provider, "_pro", None), "records", [])[raw_start:]
                receipt = {
                    "schema": "quantlab_ingestion_v1",
                    "session": str(day),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "metadata_sha256": digest,
                    "files": {name: sha256(ready / f"{name}.parquet") for name in files},
                    "raw_responses": dict(records),
                    "adopted_existing": reused,
                    "historical_publication_certified": False,
                    "context_validation": "scoped_below_provider_cap_v1",
                }
                write_json(ready / "receipt.json", receipt)
                pending.parent.mkdir(parents=True, exist_ok=True)
                ready.rename(pending)
            _commit_pending(storage, receipts, day)
            completed.append(str(day))
        return {"status": "complete", "sessions": completed, "through": str(end)}
