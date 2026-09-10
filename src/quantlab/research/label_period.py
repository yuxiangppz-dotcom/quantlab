"""Exact global-session label containment for chronological research splits."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError

PERIOD_NAMES = ("discovery", "validation", "test_observed")
LABEL_PERIOD_POLICY = "signal_and_exact_label_end_within_same_period_v1"


def period_bounds(bounds: object) -> tuple[date, date]:
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        raise DataValidationError("period requires exactly two ISO dates")
    try:
        start, end = (date.fromisoformat(value) for value in bounds)
    except (TypeError, ValueError) as exc:
        raise DataValidationError("period bounds must be ISO dates") from exc
    if start > end:
        raise DataValidationError("period start must not follow its end")
    return start, end


def validate_factor_periods(config: dict) -> None:
    # The existing factor features and target are explicitly the 5-session task.
    # Reject a misleading configurable horizon instead of changing label formulas.
    horizon = config.get("label_horizon_sessions")
    if type(horizon) is not int or horizon != 5:
        raise DataValidationError("factor experiment requires the fixed 5-session label")
    previous_end = None
    for name in PERIOD_NAMES:
        start, end = period_bounds(config.get(name))
        if previous_end is not None and start <= previous_end:
            raise DataValidationError("research periods must be ordered and non-overlapping")
        previous_end = end


def select_period_labels(
    frame: pd.DataFrame,
    bounds: list[str] | tuple[str, str],
    open_dates: list[date],
    *,
    horizon: int,
    label_column: str,
) -> tuple[pd.DataFrame, dict]:
    """Exclude unknown, crossing and missing labels; never infer sessions from rows."""
    start, end = period_bounds(bounds)
    if type(horizon) is not int or horizon <= 0:
        raise DataValidationError("label horizon must be a positive integer")
    if any(type(day) is not date for day in open_dates):
        raise DataValidationError("label calendar requires dates")
    sessions = sorted(set(open_dates))
    if "trade_date" not in frame or label_column not in frame:
        raise DataValidationError("period selection requires signal dates and label values")
    try:
        signal_dates = pd.to_datetime(frame["trade_date"], errors="raise").dt.date
    except (TypeError, ValueError) as exc:
        raise DataValidationError("period selection has invalid signal dates") from exc
    if signal_dates.isna().any() or not set(signal_dates).issubset(sessions):
        raise DataValidationError("signal date is absent from the open-session calendar")
    in_period = (signal_dates >= start) & (signal_dates <= end)
    selected = frame.loc[in_period].copy()
    selected["trade_date"] = signal_dates.loc[in_period]
    endpoints = dict(zip(sessions[:-horizon], sessions[horizon:], strict=True))
    selected["label_end_date"] = selected["trade_date"].map(endpoints)
    unknown = selected["label_end_date"].isna()
    crossing = ~unknown & (selected["label_end_date"] > end)
    try:
        labels = pd.to_numeric(selected[label_column], errors="raise")
    except (TypeError, ValueError) as exc:
        raise DataValidationError("research labels must be numeric or missing") from exc
    finite = pd.Series(np.isfinite(labels.to_numpy(dtype=float)), index=selected.index)
    missing = ~unknown & ~crossing & ~finite
    keep = ~unknown & ~crossing & finite
    counts = {
        "bounds": [start.isoformat(), end.isoformat()],
        "signal_rows": len(selected),
        "retained_rows": int(keep.sum()),
        "excluded_unknown_label_end": int(unknown.sum()),
        "excluded_crossing_label_end": int(crossing.sum()),
        "excluded_missing_or_nonfinite_label": int(missing.sum()),
    }
    return selected.loc[keep].copy(), counts
