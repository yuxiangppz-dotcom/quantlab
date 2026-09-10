"""Predeclared causal technical/index features for the second diagnostic round."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.factor_registry import (
    FACTOR_REGISTRY,
    add_transparent_combination,
    build_factor_columns,
)

BASELINE_FEATURES = tuple(item.factor_id for item in FACTOR_REGISTRY)
INDEX_FEATURES = tuple(
    f"{name}_{feature}"
    for name in ("sse", "star50")
    for feature in ("close_to_ma60", "close_to_ma120", "return_20d")
)
ADDED_FEATURES = ("wick_balance", "macd_hist_normalized", *INDEX_FEATURES)
AUGMENTED_FEATURES = (*BASELINE_FEATURES, *ADDED_FEATURES)
COMBINATION = ("reversal_20d", "low_amplitude", "small_size", "intraday_strength")


def _session_index(open_dates: list[date]) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(open_dates)
    if (
        index.hasnans
        or index.tz is not None
        or index.has_duplicates
        or not index.is_monotonic_increasing
    ):
        raise DataValidationError("feature calendar requires unique ordered naive dates")
    if not index.equals(index.normalize()):
        raise DataValidationError("feature calendar must contain whole sessions")
    return index


def consecutive_macd(frame: pd.DataFrame, open_dates: list[date]) -> pd.Series:
    """EMA12/26, DEA9; gaps reset state, first value after 60 consecutive sessions."""
    sessions = _session_index(open_dates)
    if frame.duplicated(["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicate technical feature keys")
    dates = pd.to_datetime(frame["trade_date"])
    positions = sessions.get_indexer(dates)
    if (positions < 0).any():
        raise DataValidationError("technical feature date is absent from calendar")
    work = frame[["instrument_id", "adj_close"]].copy()
    work["_session"] = positions
    work["_row"] = np.arange(len(frame))
    work = work.sort_values(["instrument_id", "_session"])
    result = np.full(len(frame), np.nan)
    for _, group in work.groupby("instrument_id", sort=False, observed=True):
        price = group["adj_close"].astype(float)
        valid = np.isfinite(price) & price.gt(0)
        consecutive = group["_session"].diff().eq(1) & valid & valid.shift(fill_value=False)
        segment_ids = (~consecutive).cumsum()
        for _, segment in group.loc[valid].groupby(segment_ids.loc[valid], sort=False):
            if len(segment) < 60:
                continue
            price = segment["adj_close"].astype(float)
            dif = price.ewm(span=12, adjust=False).mean() - price.ewm(span=26, adjust=False).mean()
            histogram = 2 * (dif - dif.ewm(span=9, adjust=False).mean()) / price
            histogram.iloc[:59] = np.nan
            result[segment["_row"].to_numpy()] = histogram.to_numpy()
    return pd.Series(result, index=frame.index, name="macd_hist_normalized")


def index_feature_frame(bundle: dict, open_dates: list[date]) -> pd.DataFrame:
    """Use a fingerprint-validated index bundle; publication is a lower bound only."""
    sessions = _session_index(open_dates)
    if bundle["request"]["semantics"]["execution_authority"] is not False:
        raise DataValidationError("index context cannot grant execution authority")
    mapping = {"000001.SH": "sse", "000688.SH": "star50"}
    result = pd.DataFrame(index=sessions)
    found = set()
    for item in bundle["series"]:
        code = item["metadata"]["ts_code"]
        if code not in mapping or code in found:
            raise DataValidationError("index feature series identity mismatch")
        found.add(code)
        rows = pd.DataFrame(item["rows"])
        dates = pd.to_datetime(rows["trade_date"])
        if dates.duplicated().any():
            raise DataValidationError("duplicate index feature dates")
        close = pd.Series(rows["close"].to_numpy(dtype=float), index=dates).reindex(sessions)
        close = close.where(np.isfinite(close) & close.gt(0))
        published = pd.Series(rows["index_published_on_date"].to_numpy(), index=dates).reindex(
            sessions
        )
        allowed = published.eq(True)
        # Extra date guard even when called directly by a synthetic/unit client.
        boundary = pd.Timestamp("2020-07-23" if code == "000688.SH" else "1991-07-15")
        allowed &= sessions >= boundary
        name = mapping[code]
        for window in (60, 120):
            result[f"{name}_close_to_ma{window}"] = (
                close / close.rolling(window, min_periods=window).mean() - 1
            ).where(allowed)
        # Missing intermediate sessions make the window incomplete; no compression.
        result[f"{name}_return_20d"] = (close / close.shift(20) - 1).where(
            allowed & close.rolling(21, min_periods=21).count().eq(21)
        )
    if found != set(mapping):
        raise DataValidationError("both predeclared index context series are required")
    result.index.name = "trade_date"
    return result.reset_index()


def build_round2_features(
    frame: pd.DataFrame,
    open_dates: list[date],
    index_bundle: dict,
    *,
    include_combination: bool = True,
) -> pd.DataFrame:
    """Retain source identities and values; mask only invalid dependent features."""
    if not frame.index.is_unique:
        raise DataValidationError("feature input row index must be unique")
    result = build_factor_columns(frame)
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    cap_valid = (
        np.isfinite(result["total_mv"])
        & np.isfinite(result["circ_mv"])
        & result["total_mv"].gt(0)
        & result["circ_mv"].gt(0)
        & result["circ_mv"].le(result["total_mv"])
    )
    result["cap_inputs_valid"] = cap_valid
    result.loc[~cap_valid, ["small_size", "float_ratio"]] = np.nan
    result.loc[
        ~np.isfinite(result["turnover_rate"]) | result["turnover_rate"].lt(0), "turnover_activity"
    ] = np.nan
    valid_ohlc = (
        np.isfinite(result[["open", "high", "low", "close"]]).all(axis=1)
        & result[["open", "high", "low", "close"]].gt(0).all(axis=1)
        & result["low"].le(result[["open", "close"]].min(axis=1))
        & result["high"].ge(result[["open", "close"]].max(axis=1))
    )
    result["wick_balance"] = (
        (
            (result[["open", "close"]].min(axis=1) - result["low"])
            - (result["high"] - result[["open", "close"]].max(axis=1))
        )
        .div(result["open"])
        .where(valid_ohlc)
    )
    result["macd_hist_normalized"] = consecutive_macd(result, open_dates)
    index_features = index_feature_frame(index_bundle, open_dates).set_index("trade_date")
    for name in INDEX_FEATURES:
        result[name] = result["trade_date"].map(index_features[name])
    if include_combination:
        result = add_transparent_combination(result, list(COMBINATION))
    result["common_features_available"] = np.isfinite(result[list(AUGMENTED_FEATURES)]).all(axis=1)
    return result
