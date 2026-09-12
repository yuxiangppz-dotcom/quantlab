"""Calendar and population planning only; no feature matrices, model calls or returns."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError


def schedule(sessions, cutoff):
    days = pd.DatetimeIndex(pd.to_datetime(sessions))
    if days.has_duplicates or days.hasnans or not days.is_monotonic_increasing:
        raise DataValidationError("invalid extended calendar")
    if not days.equals(days.normalize()) or pd.Timestamp(cutoff) not in days:
        raise DataValidationError("extended calendar requires exact dates and cutoff")
    end = pd.Timestamp(cutoff).replace(day=1) - pd.Timedelta(days=1)
    start = (end + pd.Timedelta(days=1)) - pd.DateOffset(months=12)
    signal = days[(days >= start) & (days <= end)]
    if signal.empty or start < days[0]:
        raise DataValidationError("extended signal calendar unavailable")
    last = days.get_loc(signal[-1]) + 5
    if last >= len(days) or days[last] > pd.Timestamp(cutoff):
        raise DataValidationError("extended evaluation labels not mature")
    groups = {}
    for day in signal:
        iso = day.isocalendar()
        groups.setdefault(f"{iso.year}w{iso.week:02d}", []).append(day)
    result, monthly = [], {}
    for number, (key, dates) in enumerate(groups.items(), 1):
        train_end = days[days < dates[0]][-1]
        train_start = train_end - pd.DateOffset(years=3) + pd.Timedelta(days=1)
        if train_start < days[0]:
            raise DataValidationError("extended training calendar incomplete")
        month = dates[0].strftime("%Y-%m")
        monthly.setdefault(month, key)
        monday = dates[0] - pd.Timedelta(days=dates[0].dayofweek)
        full_week = days[(days >= monday) & (days < monday + pd.Timedelta(days=7))]
        result.append(
            {
                "week": number,
                "week_id": key,
                "month": month,
                "monthly_anchor": monthly[month],
                "train_start": str(train_start.date()),
                "train_end": str(train_end.date()),
                "prediction_start": str(dates[0].date()),
                "prediction_end": str(dates[-1].date()),
                "prediction_sessions": [str(day.date()) for day in dates],
                "evaluation_label_cutoff": str(days[last].date()),
                "partial_calendar_week": len(full_week) != len(dates),
            }
        )
    return result


def daily_population(frame, sessions):
    keys = ["instrument_id", "trade_date"]
    if frame[keys].isna().any().any() or frame.duplicated(keys).any():
        raise DataValidationError("invalid extended metadata identity")
    days = pd.DatetimeIndex(pd.to_datetime(sessions))
    date = pd.to_datetime(frame.trade_date)
    endpoint = pd.to_datetime(frame.label_end_date)
    expected = date.map(dict(zip(days[:-5], days[5:], strict=True)))
    if (
        not date.isin(days).all()
        or not ((endpoint == expected) | (endpoint.isna() & expected.isna())).all()
    ):
        raise DataValidationError("extended metadata differs from global five-session endpoints")
    if frame.complete_features.dtype != bool or frame.complete_features.isna().any():
        raise DataValidationError("unknown extended feature membership")
    finite = np.isfinite(frame.future_return_5d)
    if (finite & expected.isna()).any():
        raise DataValidationError("finite extended label without known endpoint")
    return (
        pd.DataFrame(
            {
                "date": date,
                "feature": frame.complete_features.astype("int64"),
                "finite": (frame.complete_features & finite).astype("int64"),
            }
        )
        .groupby("date")
        .sum()
    )


def attach_populations(rows, daily, sessions):
    days = pd.DatetimeIndex(pd.to_datetime(sessions))
    output = []
    for row in rows:
        begin, end = pd.Timestamp(row["train_start"]), pd.Timestamp(row["train_end"])
        mature_end = days[days.get_loc(end) - 5]
        training = daily.loc[(daily.index >= begin) & (daily.index <= end)]
        mature = training.loc[training.index <= mature_end]
        prediction = daily.reindex(pd.to_datetime(row["prediction_sessions"]), fill_value=0)
        counts = {
            "train_feature_rows": int(training.feature.sum()),
            "train_rows": int(mature.finite.sum()),
            "train_unmatured_or_unknown_end": int(
                training.loc[training.index > mature_end].feature.sum()
            ),
            "train_missing_label_rows": int((training.feature - training.finite).sum()),
            "prediction_rows": int(prediction.feature.sum()),
            "evaluation_rows": int(prediction.finite.sum()),
            "prediction_missing_label_rows": int((prediction.feature - prediction.finite).sum()),
        }
        if not counts["train_rows"] or not counts["prediction_rows"]:
            raise DataValidationError("empty extended training/prediction population")
        output.append({**row, **counts})
    return output


def fit_inventory(rows, config, pilot_plan, pilot_report, hashes, preprocessing):
    old = pilot_plan["config"]
    for key in ("features", "models"):
        if config[key] != old[key]:
            raise DataValidationError("extended feature/model contract differs from reusable pilot")
    if config["runtime"] != pilot_plan["sources"]["runtime"]:
        raise DataValidationError("extended runtime differs from reusable pilot")
    expected = {row["train_end"]: row for row in old["weeks"]}
    slots, reused = [], []
    results = {row["slot"]: row for row in pilot_report["attempts"]}
    for row in rows:
        for kind in ("ridge", "lightgbm"):
            value = {
                "slot": f"{row['week_id']}_{kind}",
                "week_id": row["week_id"],
                "model": kind,
                "train_start": row["train_start"],
                "train_end": row["train_end"],
                "train_rows": row["train_rows"],
                "reuse_slot": None,
            }
            if row["train_end"] in expected:
                original = expected[row["train_end"]]
                slot = f"week{original['week']}_{kind}"
                result, prep = results[slot], preprocessing[slot]
                if (
                    result["status"] != "completed"
                    or any(
                        row[key] != original[key]
                        for key in ("train_start", "train_end", "train_rows")
                    )
                    or hashes[row["train_end"]]
                    != old["membership_sha256"][f"week{original['week']}"]["train"]
                    or prep["train_membership_sha256"] != hashes[row["train_end"]]
                    or prep["features"] != config["features"]
                    or prep["fitted_on"] != "mature_training_only"
                    or prep["matrix_dtype"] != "float32"
                    or prep["label_dtype"] != "float32"
                    or prep["label_transform"] != "none"
                    or any(
                        prep[key] != row[key] for key in ("train_start", "train_end", "train_rows")
                    )
                    or (kind == "lightgbm" and prep["scaler"] is not None)
                    or (
                        kind == "ridge"
                        and (
                            prep["scaler"]["rows"] != row["train_rows"]
                            or prep["scaler"]["ddof"] != 0
                        )
                    )
                ):
                    raise DataValidationError("extended exact pilot reuse mismatch")
                value.update(
                    reuse_slot=slot,
                    model_sha256=result["summary"]["saved_model_sha256"],
                    source_head=pilot_plan["code_head"],
                )
                reused.append(slot)
            slots.append(value)
    if len(reused) != 6 or len(set(reused)) != 6:
        raise DataValidationError("extended plan must reuse exactly the six consumed pilot models")
    return slots
