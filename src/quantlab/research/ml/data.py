"""PIT panel validation, exact-session executable labels and purged monthly folds."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quantlab.research.ml.config import MLConfig

KEYS = ["trade_date", "instrument_id"]


def calendar_index(sessions) -> pd.DatetimeIndex:
    result = pd.DatetimeIndex(pd.to_datetime(sessions))
    if result.tz is not None or result.hasnans or not result.equals(result.normalize()):
        raise ValueError("calendar requires timezone-naive, nonmissing session dates")
    if len(result) < 3 or not result.is_unique or not result.is_monotonic_increasing:
        raise ValueError("calendar must be complete, unique and ordered")
    return result


def keyed(frame, sessions):
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    if frame[KEYS].isna().any().any() or frame.duplicated(KEYS).any():
        raise ValueError("missing or duplicate panel identity")
    if not frame.instrument_id.map(lambda x: isinstance(x, str) and bool(x.strip())).all():
        raise ValueError("instrument_id must be a nonempty string")
    if not frame.trade_date.isin(sessions).all():
        raise ValueError("panel dates outside market calendar")
    return frame.sort_values(KEYS).reset_index(drop=True)


def validate_features(frame, names, sessions, config: MLConfig, *, allow_late=False):
    if not names or len(set(names)) != len(names):
        raise ValueError("feature allowlist must be nonempty and unique")
    reserved = {*KEYS, "eligible", "industry", "feature_available_at", "score"}
    if any(
        n in reserved or any(k in n.lower() for k in ("future", "label", "target")) for n in names
    ):
        raise ValueError("feature allowlist contains label/target/metadata fields")
    required = {*KEYS, "feature_available_at", "eligible", *names}
    if required - set(frame.columns):
        raise ValueError(f"missing panel columns: {sorted(required - set(frame.columns))}")
    frame = keyed(frame, sessions)
    if not frame.eligible.map(lambda x: pd.isna(x) or type(x) in (bool, np.bool_)).all():
        raise ValueError("eligibility must be explicit boolean or unknown")
    # Decision uses the frozen Shanghai local cutoff after the completed daily bar.
    if not frame.feature_available_at.map(
        lambda x: pd.notna(x) and pd.Timestamp(x).tzinfo is not None
    ).all():
        raise ValueError("feature availability must be timezone-aware and nonmissing")
    available = pd.to_datetime(frame.feature_available_at, utc=True, errors="coerce")
    deadline = frame.trade_date.dt.tz_localize("Asia/Shanghai") + pd.Timedelta(
        hours=config.decision_hour
    )
    if available.isna().any() or (not allow_late and (available > deadline).any()):
        raise ValueError("features missing availability or arriving after decision cutoff")
    values = frame[names].astype("float32").replace([np.inf, -np.inf], np.nan)
    frame = pd.concat([frame.drop(columns=names), values], axis=1)
    frame["feature_ok"] = values.notna().mean(axis=1).ge(config.min_feature_fraction)
    frame["eligible"] = frame.eligible.astype("boolean")
    return frame


def execution_labels(prices, sessions, config: MLConfig):
    """close(t+1+H)/close(t+1)-1; missing exact endpoints stay missing.

    These are adjusted-price research labels, not promises of executable fills.
    Raw prices and dated corporate-action evidence remain the replay authority.
    """
    sessions = calendar_index(sessions)
    frame = keyed(prices, sessions)
    values = pd.to_numeric(frame.adj_close, errors="raise")
    values = values.where(np.isfinite(values) & values.gt(0))
    lookup = pd.Series(values.to_numpy(), index=pd.MultiIndex.from_frame(frame[KEYS]))
    offset = sessions.get_indexer(frame.trade_date)
    result = frame[KEYS].copy()
    for name, lag in (("label_start", 1), ("label_end", 1 + config.horizon_sessions)):
        idx = offset + lag
        result[name] = pd.to_datetime([sessions[i] if i < len(sessions) else pd.NaT for i in idx])

    def at(column):
        key = pd.MultiIndex.from_arrays([result[column], result.instrument_id])
        return lookup.reindex(key).to_numpy()

    result["raw_label"] = at("label_end") / at("label_start") - 1
    result["raw_label"] = result.raw_label.where(np.isfinite(result.raw_label))
    result["label_reason"] = np.where(
        result.label_end.isna(),
        "calendar_tail",
        np.where(np.isfinite(result.raw_label), "available", "missing_endpoint"),
    )
    return result


def transform_labels(frame, config: MLConfig, kind="lightgbm"):
    """Only call after period containment. Raw evaluation returns are never clipped."""
    groups = frame.groupby("trade_date", sort=False).raw_label
    rank = groups.rank(method="average", pct=True)
    if kind == "binary":
        return (rank > 0.8).astype("float64")
    if kind == "lambdarank":
        fraction = (groups.rank(method="average") - 1) / groups.transform("size")
        return np.minimum(9, np.floor(fraction * 10)).astype("int32")
    if config.label_transform == "rank":
        return rank - groups.transform(lambda s: s.rank(pct=True).mean())
    clipped = groups.transform(lambda s: s.clip(s.quantile(0.01), s.quantile(0.99)))
    regrouped = clipped.groupby(frame.trade_date)
    scale = regrouped.transform("std").replace(0, np.nan)
    return ((clipped - regrouped.transform("mean")) / scale).fillna(0.0)


def date_weights(frame):
    """Each session carries equal total weight, regardless of changing universe size."""
    counts = frame.groupby("trade_date").instrument_id.transform("size")
    weights = 1.0 / counts
    return (weights / weights.mean()).to_numpy()


@dataclass(frozen=True)
class Fold:
    name: str
    train_start: pd.Timestamp
    validation_start: pd.Timestamp
    fit_asof: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def masks(self, frame):
        usable = frame.feature_ok & frame.eligible.fillna(False) & np.isfinite(frame.raw_label)
        train = usable & frame.trade_date.ge(self.train_start)
        train &= frame.trade_date.lt(self.validation_start) & frame.label_end.lt(
            self.validation_start
        )
        valid = usable & frame.trade_date.ge(self.validation_start)
        valid &= frame.trade_date.le(self.fit_asof) & frame.label_end.le(self.fit_asof)
        # Prediction eligibility deliberately never references a future label.
        test = frame.trade_date.between(self.test_start, self.test_end)
        test &= frame.feature_ok & frame.eligible.fillna(False)
        return train, valid, test


def monthly_folds(sessions, start, end, config: MLConfig):
    sessions = calendar_index(sessions)
    requested = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    if not len(requested):
        raise ValueError("empty prediction interval")
    result = []
    for month in requested.to_period("M").unique():
        days = requested[requested.to_period("M") == month]
        first = sessions.get_loc(days[0])
        left = first - config.train_sessions - config.validation_sessions
        if left < 0:
            raise ValueError(f"insufficient history for {month}; no silent shorter fold")
        result.append(
            Fold(
                str(month),
                sessions[left],
                sessions[first - config.validation_sessions],
                sessions[first - 1],
                days[0],
                days[-1],
            )
        )
    return result
