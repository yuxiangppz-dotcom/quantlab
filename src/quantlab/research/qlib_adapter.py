"""Optional Qlib DataFrame adapter; Canonical data remains owned by QuantLab."""

from __future__ import annotations

import importlib.util

import pandas as pd


def qlib_integration_status() -> dict[str, object]:
    available = importlib.util.find_spec("qlib") is not None
    return {
        "qlib_available": available,
        "adapter": "StaticDataLoader(DataFrame)",
        "canonical_source": "QuantLab ParquetStorage",
        "qlib_sample_data_downloaded": False,
        "qlib_backtest_used": False,
        "alpha158_subset": "KMID,KLEN exact mapping only",
        "alpha158_full_implementation": False,
        "status": "available" if available else "optional_dependency_not_installed",
    }


def to_qlib_static_loader(frame: pd.DataFrame, feature_columns: list[str]):
    """Build Qlib's StaticDataLoader from a validated local feature frame.

    Labels are intentionally excluded by an explicit column allow-list. The
    adapter raises a clear optional-dependency error rather than substituting a
    lookalike object and claiming Qlib integration.
    """
    missing = {"instrument_id", "trade_date", *feature_columns} - set(frame.columns)
    if missing:
        raise ValueError(f"Qlib adapter input missing columns: {sorted(missing)}")
    if importlib.util.find_spec("qlib") is None:
        raise RuntimeError("Qlib is not installed; run `uv sync --extra qlib` to enable it")
    from qlib.data.dataset.loader import StaticDataLoader

    payload = frame[["instrument_id", "trade_date", *feature_columns]].copy()
    payload["trade_date"] = pd.to_datetime(payload["trade_date"])
    payload = payload.set_index(["trade_date", "instrument_id"]).sort_index()
    payload.index = payload.index.set_names(["datetime", "instrument"])
    payload.columns = pd.MultiIndex.from_product([["feature"], feature_columns])
    return StaticDataLoader(payload)
