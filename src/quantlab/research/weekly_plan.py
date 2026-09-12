"""Deterministic weekly pilot and complete monthly views; no fitting or promotion."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError

METRICS = ("rank_ic", "rank_stability", "top20_membership_change")


def pilot_schedule(sessions, cutoff):
    days = pd.DatetimeIndex(pd.to_datetime(sessions))
    if days.has_duplicates or not days.is_monotonic_increasing or days.hasnans:
        raise DataValidationError("invalid or duplicated market calendar")
    if any(day.hour or day.minute or day.second for day in days):
        raise DataValidationError("market calendar must contain dates")
    cutoff = pd.Timestamp(cutoff)
    if cutoff not in days:
        raise DataValidationError("frozen cutoff absent from market calendar")
    month_end = cutoff.replace(day=1) - pd.Timedelta(days=1)
    month_start = month_end.replace(day=1)
    mondays = pd.date_range(month_start, month_end, freq="W-MON")
    mondays = [day for day in mondays if day + pd.Timedelta(days=6) <= month_end][:3]
    if len(mondays) != 3:
        raise DataValidationError("three complete pilot weeks unavailable")
    result = []
    for i, monday in enumerate(mondays):
        week = days[(days >= monday) & (days <= monday + pd.Timedelta(days=6))]
        prior = days[days < monday]
        if not len(week) or not len(prior):
            raise DataValidationError("pilot week or prior cutoff unavailable")
        train_end = prior[-1]
        train_start = train_end - pd.DateOffset(years=3) + pd.Timedelta(days=1)
        if train_start < days[0]:
            raise DataValidationError("calendar does not cover rolling training window")
        result.append(
            {
                "week": i + 1,
                "week_monday": str(monday.date()),
                "train_start": str(train_start.date()),
                "train_end": str(train_end.date()),
                "prediction_start": str(week[0].date()),
                "prediction_end": str(week[-1].date()),
                "prediction_sessions": [str(day.date()) for day in week],
            }
        )
    last_position = days.get_loc(pd.Timestamp(result[-1]["prediction_end"]))
    if last_position + 5 >= len(days) or days[last_position + 5] > cutoff:
        raise DataValidationError("pilot evaluation labels have no known maturity cutoff")
    end = str(days[last_position + 5].date())
    for row in result:
        row["evaluation_label_cutoff"] = end
        row["anchor_week"] = 1
    return result


def monthly_summary(daily, expected_sessions):
    frame = daily.copy()
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    if frame.trade_date.duplicated().any() or frame.trade_date.isna().any():
        raise DataValidationError("daily diagnostics have duplicate or unknown dates")
    if sorted(frame.trade_date) != list(pd.to_datetime(expected_sessions)):
        raise DataValidationError("daily diagnostics omit or add market sessions")
    for key in ("score_rows", "evaluation_rows", "missing_label_rows", "crossing_label_rows"):
        values = frame[key]
        if values.isna().any() or (values < 0).any() or (values != values.astype(int)).any():
            raise DataValidationError("invalid daily population")
    if (frame.evaluation_rows > frame.score_rows).any():
        raise DataValidationError("labels exceed prediction population")
    for key in METRICS:
        if np.isinf(frame[key]).any():
            raise DataValidationError("nonfinite saved diagnostic")
    result = []
    for month, group in frame.groupby(frame.trade_date.dt.strftime("%Y-%m"), sort=True):
        row = {
            "month": month,
            "market_days": len(group),
            "score_days": int(group.score_rows.gt(0).sum()),
        }
        for key in ("score_rows", "evaluation_rows", "missing_label_rows", "crossing_label_rows"):
            row[key] = int(group[key].sum())
        for key in METRICS:
            values = group[key].dropna()
            row[key + "_days"] = len(values)
            row["mean_" + key] = None if values.empty else float(values.mean())
        valid_ic = group.rank_ic.dropna()
        row["positive_ic_day_fraction"] = None if valid_ic.empty else float(valid_ic.gt(0).mean())
        result.append(row)
    return result


def population_counts(frame, schedule, sessions):
    """Validate every metadata endpoint; prediction membership never depends on labels."""
    frame = frame.copy()
    if frame.duplicated(["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicated metadata identity")
    frame.trade_date = pd.to_datetime(frame.trade_date)
    frame.label_end_date = pd.to_datetime(frame.label_end_date)
    days = pd.DatetimeIndex(pd.to_datetime(sessions))
    if days.has_duplicates or not days.is_monotonic_increasing:
        raise DataValidationError("invalid metadata market calendar")
    if frame.trade_date.isna().any() or not frame.trade_date.isin(days).all():
        raise DataValidationError("metadata signal outside exact market calendar")
    if frame.complete_features.isna().any() or frame.complete_features.dtype != bool:
        raise DataValidationError("unknown feature membership")
    endpoint_map = dict(zip(days[:-5], days[5:], strict=True))
    expected = frame.trade_date.map(endpoint_map)
    if not (
        (frame.label_end_date == expected) | (frame.label_end_date.isna() & expected.isna())
    ).all():
        raise DataValidationError("metadata label endpoint differs from exact calendar")
    finite = np.isfinite(frame.future_return_5d)
    if (finite & expected.isna()).any():
        raise DataValidationError("finite label with unavailable endpoint")
    result = []
    for week in schedule:
        available = frame.complete_features
        train = available & frame.trade_date.between(week["train_start"], week["train_end"])
        mature = frame.label_end_date.le(week["train_end"])
        prediction = available & frame.trade_date.isin(pd.to_datetime(week["prediction_sessions"]))
        evaluated = prediction & finite & frame.label_end_date.le(week["evaluation_label_cutoff"])
        result.append(
            {
                "week": week["week"],
                "train_feature_rows": int(train.sum()),
                "train_rows": int((train & mature & finite).sum()),
                "train_unmatured_or_unknown_end": int((train & ~mature).sum()),
                "train_missing_label_rows": int((train & ~finite).sum()),
                "prediction_rows": int(prediction.sum()),
                "evaluation_rows": int(evaluated.sum()),
                "prediction_missing_label_rows": int((prediction & ~finite).sum()),
                "prediction_unmatured_or_unknown_end": int(
                    (prediction & ~frame.label_end_date.le(week["evaluation_label_cutoff"])).sum()
                ),
            }
        )
    return result
