"""End-to-end research strategy for observable base completion and breakout.

"Main-force accumulation" is not observable in daily bars.  This strategy
therefore classifies only causal price/volume behaviour and reuses the frozen
S5-B state kernel.  It does not infer account identity or trading intent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.s5_base_completion import (
    S5BaseConfig,
    S5BaseObservation,
    S5BaseState,
    evaluate_s5_base,
)

STRATEGY_ID = "price_volume_base_breakout"
FEATURE_HISTORY_SESSIONS = 120


@dataclass(frozen=True)
class BaseBreakoutStrategyConfig:
    """Selection policy; detection thresholds remain owned by ``S5BaseConfig``."""

    max_names: int = 20
    weight_per_name: float = 0.04
    evaluation_horizons: tuple[int, ...] = (1, 5, 10, 20)
    state_config: S5BaseConfig = S5BaseConfig()

    def __post_init__(self) -> None:
        if type(self.max_names) is not int or self.max_names <= 0:
            raise ValueError("max_names must be a positive integer")
        if not math.isfinite(self.weight_per_name) or not 0.0 < self.weight_per_name <= 1.0:
            raise ValueError("weight_per_name must be finite and in (0, 1]")
        if self.max_names * self.weight_per_name > 1.0 + 1e-12:
            raise ValueError("max_names * weight_per_name cannot exceed 1")
        if (
            not self.evaluation_horizons
            or tuple(sorted(set(self.evaluation_horizons))) != self.evaluation_horizons
            or any(type(value) is not int or value <= 0 for value in self.evaluation_horizons)
        ):
            raise ValueError("evaluation_horizons must be unique increasing positive integers")


_FEATURES = (
    "prior_drawdown_from_120d_high",
    "recent_return_10",
    "max_drawdown_10",
    "new_low_count_10",
    "new_low_count_20",
    "max_new_low_undercut_10",
    "volatility_ratio_10_to_prior_20",
    "recovery_from_10d_low",
    "prior_low_reclaimed",
    "reclaim_sessions",
    "close_location_20",
    "close_to_ma20_ratio",
    "ma20_slope_5",
    "breakout_distance_20",
    "volume_ratio_5_to_20",
    "history_complete",
)


def build_base_breakout_signals(
    frame: pd.DataFrame,
    market_sessions: tuple[date, ...],
    *,
    signal_dates: tuple[date, ...] | None = None,
    config: BaseBreakoutStrategyConfig | None = None,
) -> pd.DataFrame:
    """Materialize causal features, classify states, and construct daily targets.

    ``frame`` may contain future-return columns, but only the four declared
    market columns are read.  Missing instrument sessions are retained as gaps
    and cannot be compressed into apparently continuous history.
    """

    cfg = config or BaseBreakoutStrategyConfig()
    required = {"instrument_id", "trade_date", "adj_close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise DataValidationError(f"base breakout input missing columns: {sorted(missing)}")
    _validate_sessions(market_sessions)
    session_index = pd.DatetimeIndex(market_sessions)
    selected_dates = tuple(signal_dates) if signal_dates is not None else market_sessions
    if len(selected_dates) != len(set(selected_dates)):
        raise ValueError("signal_dates cannot contain duplicates")
    unknown_dates = set(selected_dates) - set(market_sessions)
    if unknown_dates:
        raise ValueError("signal_dates must be contained in market_sessions")

    work = frame[["instrument_id", "trade_date", "adj_close", "volume"]].copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"])
    if work.duplicated(["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicate base breakout input keys")
    invalid_ids = work["instrument_id"].isna() | work["instrument_id"].astype(
        str
    ).str.strip().eq("")
    if invalid_ids.any():
        raise DataValidationError("instrument_id must be non-empty")
    if not work["trade_date"].isin(session_index).all():
        raise DataValidationError("input trade_date is absent from market_sessions")
    for column in ("adj_close", "volume"):
        work[column] = pd.to_numeric(work[column], errors="coerce")
        values = work[column].to_numpy(dtype=float)
        invalid = ~np.isfinite(values) | (values <= 0.0 if column == "adj_close" else values < 0.0)
        if invalid.any():
            raise DataValidationError(f"{column} contains invalid observations")

    wanted = {pd.Timestamp(value) for value in selected_dates}
    parts: list[pd.DataFrame] = []
    for instrument_id, rows in work.groupby("instrument_id", sort=True, observed=True):
        indexed = rows.set_index("trade_date").sort_index().reindex(session_index)
        features = _features_for_instrument(indexed["adj_close"], indexed["volume"])
        features["instrument_id"] = str(instrument_id)
        features["trade_date"] = features.index
        present = indexed["adj_close"].notna() & features.index.isin(wanted)
        parts.append(features.loc[present].reset_index(drop=True))

    columns = ["instrument_id", "trade_date", *_FEATURES]
    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)
    if result.empty:
        return _empty_result()

    states: list[str] = []
    reasons: list[str] = []
    for row in result.itertuples(index=False):
        observation = S5BaseObservation(
            entity_id=row.instrument_id,
            as_of=row.trade_date.date(),
            **{name: _python_value(getattr(row, name)) for name in _FEATURES},
        )
        evaluated = evaluate_s5_base(observation, cfg.state_config)
        states.append(evaluated.state.value)
        reasons.append("|".join(evaluated.reasons))
    result["state"] = states
    result["reasons"] = reasons
    result["eligible"] = result["state"].isin(
        {S5BaseState.BASE_READY.value, S5BaseState.BREAKOUT_CONFIRMED.value}
    )
    result["rank"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["selected"] = False
    result["target_weight"] = 0.0

    priority = result["state"].map(
        {S5BaseState.BASE_READY.value: 1, S5BaseState.BREAKOUT_CONFIRMED.value: 2}
    )
    result["_priority"] = priority
    for _, date_rows in result[result["eligible"]].groupby("trade_date", sort=True):
        ordered = date_rows.sort_values(
            [
                "_priority",
                "recovery_from_10d_low",
                "close_location_20",
                "volume_ratio_5_to_20",
                "instrument_id",
            ],
            ascending=[False, False, False, False, True],
            kind="stable",
        )
        result.loc[ordered.index, "rank"] = range(1, len(ordered) + 1)
        chosen = ordered.index[: cfg.max_names]
        result.loc[chosen, "selected"] = True
        result.loc[chosen, "target_weight"] = cfg.weight_per_name
    result = result.drop(columns="_priority").sort_values(
        ["trade_date", "instrument_id"], kind="stable"
    )
    result["cash_weight"] = 1.0 - result.groupby("trade_date")["target_weight"].transform("sum")
    result["strategy_id"] = STRATEGY_ID
    result["research_only"] = True
    result["performance_claim"] = False
    result["broker_order_authority"] = False
    return result.reset_index(drop=True)


def summarize_base_breakout_signals(
    signals: pd.DataFrame,
    labelled_frame: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 5, 10, 20),
) -> list[dict[str, object]]:
    """Return close-to-close signal diagnostics, never executable PnL."""

    if not horizons or any(type(value) is not int or value <= 0 for value in horizons):
        raise ValueError("horizons must contain positive integers")
    keys = ["instrument_id", "trade_date"]
    labels = labelled_frame.copy()
    labels["trade_date"] = pd.to_datetime(labels["trade_date"])
    if signals.duplicated(keys).any() or labels.duplicated(keys).any():
        raise DataValidationError("diagnostic keys must be unique")
    required = set(keys) | {f"future_return_{value}d" for value in horizons}
    missing = required - set(labels.columns)
    if missing:
        raise DataValidationError(f"diagnostic labels missing columns: {sorted(missing)}")
    merged = signals.merge(labels[list(required)], on=keys, how="left", validate="one_to_one")
    rows: list[dict[str, object]] = []
    for horizon in horizons:
        column = f"future_return_{horizon}d"
        for population, mask in (
            ("all_observed", pd.Series(True, index=merged.index)),
            ("eligible", merged["eligible"]),
            ("selected", merged["selected"]),
            ("breakout_confirmed", merged["state"].eq(S5BaseState.BREAKOUT_CONFIRMED.value)),
        ):
            values = pd.to_numeric(merged.loc[mask, column], errors="coerce").dropna()
            rows.append(
                {
                    "strategy_id": STRATEGY_ID,
                    "population": population,
                    "horizon_sessions": horizon,
                    "observation_count": int(len(values)),
                    "mean_close_return": float(values.mean()) if len(values) else None,
                    "median_close_return": float(values.median()) if len(values) else None,
                    "strict_positive_rate": float(values.gt(0).mean()) if len(values) else None,
                    "label_semantics": "signal_close_to_future_session_close",
                    "executable_pnl": False,
                    "performance_claim": False,
                }
            )
    return rows


def _features_for_instrument(close: pd.Series, volume: pd.Series) -> pd.DataFrame:
    result = pd.DataFrame(index=close.index)
    returns = close.pct_change(fill_method=None)
    prior_low = close.shift(1).rolling(60, min_periods=60).min()
    new_low = close.lt(prior_low) & close.notna() & prior_low.notna()
    undercut = (1.0 - close / prior_low).where(new_low, 0.0)
    result["prior_drawdown_from_120d_high"] = (
        close / close.rolling(120, min_periods=120).max() - 1.0
    )
    result["recent_return_10"] = close / close.shift(10) - 1.0
    result["max_drawdown_10"] = close.rolling(10, min_periods=10).apply(_max_drawdown, raw=True)
    result["new_low_count_10"] = new_low.astype(float).rolling(10, min_periods=10).sum()
    result["new_low_count_20"] = new_low.astype(float).rolling(20, min_periods=20).sum()
    result["max_new_low_undercut_10"] = undercut.rolling(10, min_periods=10).max()
    current_vol = returns.rolling(10, min_periods=10).std(ddof=1)
    prior_vol = returns.shift(10).rolling(20, min_periods=20).std(ddof=1)
    result["volatility_ratio_10_to_prior_20"] = current_vol / prior_vol.where(prior_vol.gt(0))
    result["recovery_from_10d_low"] = close / close.rolling(10, min_periods=10).min() - 1.0
    reclaimed, reclaim_sessions = _reclaim_features(close, new_low, prior_low)
    result["prior_low_reclaimed"] = reclaimed
    result["reclaim_sessions"] = reclaim_sessions
    low20 = close.rolling(20, min_periods=20).min()
    high20 = close.rolling(20, min_periods=20).max()
    spread = high20 - low20
    result["close_location_20"] = ((close - low20) / spread).where(spread.ne(0), 0.5)
    ma20 = close.rolling(20, min_periods=20).mean()
    result["close_to_ma20_ratio"] = close / ma20
    result["ma20_slope_5"] = ma20 / ma20.shift(5) - 1.0
    result["breakout_distance_20"] = close / close.shift(1).rolling(20, min_periods=20).max() - 1.0
    volume20 = volume.rolling(20, min_periods=20).mean()
    result["volume_ratio_5_to_20"] = (
        volume.rolling(5, min_periods=5).mean() / volume20.where(volume20.gt(0))
    )
    result["new_low_count_10"] = result["new_low_count_10"].round().astype("Int64")
    result["new_low_count_20"] = result["new_low_count_20"].round().astype("Int64")
    required_features = [name for name in _FEATURES[:-1] if name != "reclaim_sessions"]
    result["history_complete"] = (
        close.rolling(120, min_periods=120).count().eq(120)
        & volume.rolling(20, min_periods=20).count().eq(20)
        & prior_vol.gt(0)
        & result[required_features].notna().all(axis=1)
    )
    return result


def _reclaim_features(
    close: pd.Series, new_low: pd.Series, prior_low: pd.Series
) -> tuple[pd.Series, pd.Series]:
    prices = close.to_numpy(dtype=float)
    lows = new_low.to_numpy(dtype=bool)
    references = prior_low.to_numpy(dtype=float)
    reclaimed: list[object] = [pd.NA] * len(close)
    sessions: list[object] = [pd.NA] * len(close)
    for end in range(9, len(close)):
        if not np.isfinite(prices[end - 9 : end + 1]).all():
            continue
        candidates = np.flatnonzero(lows[max(0, end - 9) : end + 1])
        if not len(candidates):
            reclaimed[end] = True
            continue
        low_index = max(0, end - 9) + int(candidates[-1])
        reference = references[low_index]
        offsets = [
            index - low_index
            for index in range(low_index + 1, end + 1)
            if prices[index] > reference
        ]
        reclaimed[end] = bool(offsets)
        sessions[end] = offsets[0] if offsets else end - low_index
    return (
        pd.Series(reclaimed, index=close.index, dtype="boolean"),
        pd.Series(sessions, index=close.index, dtype="Int64"),
    )


def _max_drawdown(values: np.ndarray) -> float:
    peak = values[0]
    drawdown = 0.0
    for value in values[1:]:
        drawdown = max(drawdown, 1.0 - value / peak)
        peak = max(peak, value)
    return drawdown


def _python_value(value: object) -> object:
    if value is pd.NA or (not isinstance(value, (bool, np.bool_)) and pd.isna(value)):
        return None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _validate_sessions(sessions: tuple[date, ...]) -> None:
    if not sessions:
        raise ValueError("market_sessions cannot be empty")
    if any(left >= right for left, right in pairwise(sessions)):
        raise ValueError("market_sessions must be strictly increasing")


def _empty_result() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "instrument_id",
            "trade_date",
            *_FEATURES,
            "state",
            "reasons",
            "eligible",
            "rank",
            "selected",
            "target_weight",
            "cash_weight",
            "strategy_id",
            "research_only",
            "performance_claim",
            "broker_order_authority",
        ]
    )
