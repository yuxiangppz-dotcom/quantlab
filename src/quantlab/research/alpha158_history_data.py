"""Read-only historical identity discovery and bounded raw Alpha158 partitions."""

from __future__ import annotations

import io
from datetime import date

import numpy as np
import pandas as pd

from quantlab.data.context_backfill import _calendar
from quantlab.data.models import DataValidationError
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.alpha158_native import FIELDS, KEYS, RAW_FIELDS, map_inputs
from quantlab.research.dataset import _build_delist_dates, _build_list_dates
from quantlab.research.round2_dataset import InputBinding
from quantlab.research.universe import is_v1_a_share


def read_partition(root, source, binding, columns=None):
    path = root / source["path"]
    if source["status"] == "missing":
        if path.exists():
            raise DataValidationError("previously missing canonical partition appeared")
        return pd.DataFrame(columns=columns or [*KEYS, *RAW_FIELDS])
    data = pd.read_parquet(io.BytesIO(binding.read(path)), columns=columns, use_threads=False)
    if (
        data[KEYS].isna().any().any()
        or data.duplicated(KEYS).any()
        or not pd.to_datetime(data.trade_date).eq(pd.Timestamp(source["date"])).all()
        or not data.instrument_id.map(lambda x: isinstance(x, str)).all()
    ):
        raise DataValidationError(f"invalid canonical identity/date: {source['path']}")
    data["trade_date"] = pd.to_datetime(data.trade_date)
    return data


def discover_inventory(root, config):
    """Inspect identities/source hashes only; no factor or label outcomes are read."""
    binding = InputBinding(root)
    storage = ParquetStorage(root / "data/canonical")
    binding.read(storage.calendar_path)
    binding.read(storage.securities_path)
    changes_path = root / "config/security_code_changes.csv"
    binding.read(changes_path)
    dates = sorted({x.trade_date for x in storage.load_trading_calendar() if x.is_open})
    first = next(i for i, day in enumerate(dates) if str(day) >= config["start"])
    if first < config["warmup_sessions"]:
        raise DataValidationError("insufficient historical staging warm-up calendar")
    _, calendar = _calendar(
        storage, dates[first - config["warmup_sessions"]], date.fromisoformat(config["end"])
    )
    sessions = sorted({str(x.trade_date) for x in calendar if x.is_open})
    sources, identities, outside = [], set(), set()
    for day in sessions:
        stamp = date.fromisoformat(day)
        for kind, path in (
            ("daily", storage.daily_bars_path(stamp)),
            ("adj_factor", storage.adj_factor_path(stamp)),
        ):
            item = {
                "date": day,
                "kind": kind,
                "path": path.relative_to(root).as_posix(),
                "status": "present" if path.exists() else "missing",
            }
            data = read_partition(root, item, binding, KEYS)
            item["rows"] = len(data)
            if kind == "daily" and day >= config["start"]:
                for code in data.instrument_id.unique():
                    (identities if is_v1_a_share(code) else outside).add(code)
            sources.append(item)
    if not identities:
        raise DataValidationError("no historical target-period identities")
    securities = storage.load_securities()
    changes = load_security_code_changes(changes_path)
    listed = _build_list_dates(securities, changes)
    delisted = _build_delist_dates(securities, changes)
    binding.check()
    return {
        "scope": {key: config[key] for key in ("start", "end", "warmup_sessions")},
        "sessions": sessions,
        "instruments": sorted(identities),
        "outside_v1_observed_codes": sorted(outside),
        "sources": sources,
        "source_files": binding.entries,
        "list_dates": {
            code: str(listed[code]) if listed.get(code) else None for code in sorted(identities)
        },
        "delist_dates": {
            code: str(delisted[code]) if delisted.get(code) else None for code in sorted(identities)
        },
        "historical_market_coverage_complete": False,
    }


def ingest_month(root, folder, month, inventory, config):
    binding = InputBinding(root)
    instruments = inventory["instruments"]
    batches = {code: i // config["batch_codes"] for i, code in enumerate(instruments)}
    inputs = [s for s in inventory["sources"] if s["date"].startswith(month)]
    rows = []
    for day in sorted({s["date"] for s in inputs}):
        daily_src, factor_src = [
            next(s for s in inputs if s["date"] == day and s["kind"] == kind)
            for kind in ("daily", "adj_factor")
        ]
        daily = read_partition(
            root, daily_src, binding, [*KEYS, *[c for c in RAW_FIELDS if c != "adj_factor"]]
        )
        factor = read_partition(root, factor_src, binding, [*KEYS, "adj_factor"])
        daily = daily.loc[daily.instrument_id.isin(instruments)].copy()
        daily["trade_date"], factor["trade_date"] = (
            pd.to_datetime(daily.trade_date),
            pd.to_datetime(factor.trade_date),
        )
        rows.append(daily.merge(factor, on=KEYS, how="left", validate="one_to_one"))
    raw = pd.concat(rows, ignore_index=True)
    if any(inventory["source_files"].get(name) != entry for name, entry in binding.entries.items()):
        raise DataValidationError("raw staging source identity mismatch")
    binding.check()
    counts = {}
    for number, part in raw.groupby(raw.instrument_id.map(batches), sort=True):
        part.sort_values(KEYS).to_parquet(folder / f"batch-{number:04d}.parquet", index=False)
        counts[str(number)] = len(part)
    return {
        "month": month,
        "raw_rows": len(raw),
        "batch_rows": counts,
        "folder": folder.relative_to(root).as_posix(),
        "source_files": binding.entries,
    }


def map_history_inputs(raw, sessions, codes, inventory):
    """Unknown lifecycle remains unknown in evidence and ineligible for all features."""
    if (
        raw.duplicated(KEYS).any()
        or raw[KEYS].isna().any().any()
        or not raw.instrument_id.isin(codes).all()
        or not pd.to_datetime(raw.trade_date).isin(pd.DatetimeIndex(sessions)).all()
    ):
        raise DataValidationError("raw batch escaped fixed identity/calendar scope")
    known = [code for code in codes if inventory["list_dates"].get(code)]
    unknown = [code for code in codes if code not in known]
    frames, evidence = [], []
    if known:
        listed = {code: date.fromisoformat(inventory["list_dates"][code]) for code in known}
        delisted = {
            code: date.fromisoformat(day)
            for code in known
            if (day := inventory["delist_dates"].get(code))
        }
        mapped, observed = map_inputs(
            raw.loc[raw.instrument_id.isin(known)], sessions, known, listed, delisted
        )
        observed["lifecycle_known"] = True
        frames.append(mapped)
        evidence.append(observed)
    if unknown:
        selected = raw.loc[raw.instrument_id.isin(unknown), [*KEYS, *RAW_FIELDS]].copy()
        selected["trade_date"] = pd.to_datetime(selected.trade_date)
        if selected.duplicated(KEYS).any():
            raise DataValidationError("duplicate unknown-lifecycle identity")
        grid = pd.MultiIndex.from_product([unknown, pd.DatetimeIndex(sessions)], names=KEYS)
        observed = selected.set_index(KEYS).reindex(grid).reset_index()
        observed["daily_observed"] = grid.isin(selected.set_index(KEYS).index)
        observed["lifecycle_known"] = False
        observed["lifecycle_active"] = pd.NA
        observed["exclusion_reason"] = "unknown_lifecycle"
        observed["derived_raw_vwap"] = np.nan
        for field in ("price_inputs_valid", "volume_inputs_valid", "vwap_inputs_valid"):
            observed[field] = False
        frames.append(observed[KEYS].assign(**{field: np.float32(np.nan) for field in FIELDS}))
        evidence.append(observed)
    mapped = pd.concat(frames, ignore_index=True).sort_values(KEYS).reset_index(drop=True)
    observed = pd.concat(evidence, ignore_index=True).sort_values(KEYS).reset_index(drop=True)
    observed["lifecycle_active"] = observed.lifecycle_active.astype("boolean")
    return mapped, observed
