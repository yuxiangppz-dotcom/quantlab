"""Source-bound staging for the fixed second round; canonical inputs are read-only."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.data.context_backfill import _calendar, _write_json
from quantlab.data.index_context import load_index_context
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.dataset import _build_delist_dates, _build_list_dates
from quantlab.research.factor_registry import add_transparent_combination
from quantlab.research.input_audit import _sha
from quantlab.research.returns import calculate_forward_returns, calculate_returns
from quantlab.research.round2_features import (
    AUGMENTED_FEATURES,
    COMBINATION,
    build_round2_features,
)
from quantlab.research.universe import is_v1_a_share

KEYS = ["instrument_id", "trade_date"]
RAW_COLUMNS = [*KEYS, "open", "high", "low", "close", "amount"]
BASIC_COLUMNS = [*KEYS, "turnover_rate", "total_mv", "circ_mv"]


def sealed_write(path: Path, payload: dict) -> dict:
    result = {**payload, "fingerprint": canonical_payload_fingerprint(payload)}
    _write_json(path, result)
    return result


def sealed_read(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    content = {key: value for key, value in result.items() if key != "fingerprint"}
    if result.get("fingerprint") != canonical_payload_fingerprint(content):
        raise DataValidationError(f"artifact fingerprint mismatch: {path.name}")
    return result


class InputBinding:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.entries: dict[str, dict] = {}

    def read(self, path: Path) -> bytes:
        path = path.resolve()
        name = path.relative_to(self.root).as_posix()
        raw = path.read_bytes()
        entry = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        if name in self.entries and self.entries[name] != entry:
            raise DataValidationError(f"source changed while reading: {name}")
        self.entries[name] = entry
        return raw

    def check(self) -> None:
        verify_entries(self.root, self.entries)


def verify_entries(root: Path, entries: dict) -> None:
    for name, entry in entries.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or _sha(path) != entry["sha256"]:
            raise DataValidationError(f"bound file changed: {name}")


def code_binding(root: Path, binding: InputBinding) -> str:
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
    if status.strip():
        raise DataValidationError("real research requires a clean committed workspace")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    for name in names:
        if name:
            binding.read(root / name)
    return head


def partition_frame(binding: InputBinding, path: Path, day: date) -> pd.DataFrame:
    frame = pd.read_parquet(io.BytesIO(binding.read(path)), use_threads=False)
    if frame[KEYS].isna().any().any() or frame.duplicated(KEYS).any():
        raise DataValidationError(f"invalid partition keys: {path.name}")
    parsed = pd.to_datetime(frame.trade_date)
    if not parsed.eq(pd.Timestamp(day)).all():
        raise DataValidationError(f"wrong partition date: {path}")
    frame["trade_date"] = parsed
    return frame


def join_session(daily, factors, basics, list_dates, delist_dates):
    """Vectorized frozen price/PIT math, left basics join, all input identities retained."""
    if daily.instrument_id.isna().any() or not set(daily.instrument_id).issubset(list_dates):
        raise DataValidationError("unknown daily instrument identity")
    joined = daily[RAW_COLUMNS].merge(
        factors[[*KEYS, "adj_factor"]], on=KEYS, how="left", validate="one_to_one"
    )
    if joined.adj_factor.isna().any():
        raise DataValidationError("missing adj_factor for a daily observation")
    joined = joined.merge(
        basics[BASIC_COLUMNS], on=KEYS, how="left", validate="one_to_one", indicator="basic_join"
    )
    joined["adj_close"] = joined.close * joined.adj_factor
    # Bad prices stay in staged raw evidence but cannot bridge return endpoints or MACD.
    valid = (
        np.isfinite(joined.adj_close)
        & joined.adj_close.gt(0)
        & joined.close.gt(0)
        & joined.adj_factor.gt(0)
    )
    joined["adj_close"] = joined.adj_close.where(valid)
    listed = pd.to_datetime(joined.instrument_id.map(list_dates))
    delisted = pd.to_datetime(joined.instrument_id.map(delist_dates))
    active = joined.trade_date.ge(listed) & (delisted.isna() | joined.trade_date.le(delisted))
    scope = joined.instrument_id.map(is_v1_a_share)
    joined["research_exclusion"] = np.select(
        [~active, ~scope], ["inactive_code_or_lifecycle", "outside_v1_scope"], default="retained"
    )
    return joined


def prepare_dataset(root: Path, out: Path, config: dict, *, progress=print) -> dict:
    """Write a new exclusive staging directory; no fitting, source mutation or providers."""
    out.mkdir(parents=True, exist_ok=False)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    storage = ParquetStorage(root / "data/canonical")
    binding.read(storage.calendar_path)
    _, calendar = _calendar(
        storage, date.fromisoformat(config["read_start"]), date.fromisoformat(config["data_end"])
    )
    sessions = sorted({item.trade_date for item in calendar if item.is_open})
    binding.read(storage.securities_path)
    securities = storage.load_securities()
    if len({item.instrument_id for item in securities}) != len(securities):
        raise DataValidationError("duplicate security master")
    changes_path = root / "config/security_code_changes.csv"
    binding.read(changes_path)
    changes = load_security_code_changes(changes_path)
    lists, delists = (
        _build_list_dates(securities, changes),
        _build_delist_dates(securities, changes),
    )
    snapshot = root / config["index_snapshot"]
    binding.read(snapshot)
    bundle = load_index_context(snapshot, expected_fingerprint=config["index_fingerprint"])
    ids = sorted(lists)
    buckets = {code: i // 200 for i, code in enumerate(ids)}
    raw_dir, feature_dir = out / "raw", out / "features"
    raw_dir.mkdir()
    feature_dir.mkdir()
    counts, all_paths = [], {}
    for year in sorted({day.year for day in sessions}):
        parts = []
        for day in (day for day in sessions if day.year == year):
            daily = partition_frame(binding, storage.daily_bars_path(day), day)
            factors = partition_frame(binding, storage.adj_factor_path(day), day)
            basics = partition_frame(binding, storage.daily_basic_path(day), day)
            joined = join_session(daily, factors, basics, lists, delists)
            parts.append(joined)
            counts.append(
                {
                    "trade_date": str(day),
                    "source_rows": len(daily),
                    "retained_rows": int(joined.research_exclusion.eq("retained").sum()),
                    "exclusions": joined.research_exclusion.value_counts().to_dict(),
                    "missing_basic_source_rows": int(joined.basic_join.eq("left_only").sum()),
                }
            )
        year_frame = pd.concat(parts, ignore_index=True)
        del parts
        for bucket, part in year_frame.groupby(year_frame.instrument_id.map(buckets)):
            path = raw_dir / f"{year}-{bucket:02d}.parquet"
            part.to_parquet(path, index=False)
            all_paths[path.relative_to(out).as_posix()] = {"sha256": _sha(path), "rows": len(part)}
        del year_frame
        progress(f"staged source year {year}", flush=True)
    binding.check()
    for bucket in sorted(set(buckets.values())):
        paths = sorted(raw_dir.glob(f"*-{bucket:02d}.parquet"))
        if not paths:
            continue
        frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
        frame = frame.loc[frame.research_exclusion.eq("retained")].copy()
        if frame.empty:
            continue
        price = calculate_returns(frame[[*KEYS, "adj_close"]], sessions, (1, 5, 20))
        price = calculate_forward_returns(price, sessions, (5, 10, 20))
        price["trade_date"] = pd.to_datetime(price.trade_date)
        frame = frame.merge(price.drop(columns="adj_close"), on=KEYS, validate="one_to_one")
        features = build_round2_features(frame, sessions, bundle, include_combination=False)
        features = features.loc[
            features.trade_date.ge(config["data_start"]),
            [
                *KEYS,
                *AUGMENTED_FEATURES,
                "cap_inputs_valid",
                "common_features_available",
                "future_return_5d",
                "future_return_10d",
                "future_return_20d",
            ],
        ]
        path = feature_dir / f"{bucket:02d}.parquet"
        features.to_parquet(path, index=False)
        all_paths[path.relative_to(out).as_posix()] = {"sha256": _sha(path), "rows": len(features)}
        del frame, price, features
        progress(f"built full-history feature bucket {bucket}", flush=True)
    # Normalize all PIT eligible rows before applying any future-label mask.
    frame = pd.concat(
        [pd.read_parquet(path) for path in sorted(feature_dir.glob("*.parquet"))], ignore_index=True
    )
    frame["instrument_id"] = frame.instrument_id.astype("category")
    frame = add_transparent_combination(frame, list(COMBINATION))
    frame = frame.drop(columns=[f"{name}_combo_contribution" for name in COMBINATION])
    final_path = out / "dataset.parquet"
    frame.to_parquet(final_path, index=False)
    all_paths["dataset.parquet"] = {"sha256": _sha(final_path), "rows": len(frame)}
    missing = {name: int((~np.isfinite(frame[name])).sum()) for name in AUGMENTED_FEATURES}
    invalid_caps = frame.loc[~frame.cap_inputs_valid, KEYS].copy()
    invalid_caps["trade_date"] = invalid_caps.trade_date.dt.strftime("%Y-%m-%d")
    binding.check()
    payload = {
        "schema_version": 1,
        "config": config,
        "code_head": head,
        "inputs": binding.entries,
        "artifacts": all_paths,
        "open_dates": [str(day) for day in sessions],
        "source_coverage": counts,
        "feature_rows": len(frame),
        "common_feature_rows": int(frame.common_features_available.sum()),
        "feature_missing": missing,
        "invalid_cap_rows": invalid_caps.to_dict("records"),
        "performance_eligible": False,
        "execution_authority": False,
    }
    return sealed_write(out / "manifest.json", payload)
