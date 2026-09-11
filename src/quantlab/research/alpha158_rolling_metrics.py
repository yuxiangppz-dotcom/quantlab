"""Descriptive diagnostics; no independent-sample tests or return conversion."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_data import contained_mask
from quantlab.research.round2_diagnostics import clean_numbers


def daily_diagnostics(frame, bounds, sessions):
    if frame.duplicated(["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicate model score identity")
    frame = frame.sort_values(["trade_date", "instrument_id"])
    valid = contained_mask(frame, bounds)
    frame = frame.assign(evaluation_available=valid)
    previous, previous_day, records = None, None, []
    days = [pd.Timestamp(day) for day in sessions if bounds[0] <= day <= bounds[1]]
    grouped = {d: f for d, f in frame.groupby("trade_date", sort=True)}
    for day in days:
        part = grouped.get(day, frame.iloc[:0])
        scores = part.set_index("instrument_id").score
        eligible = part.loc[part.evaluation_available]
        ic = (
            eligible.score.rank().corr(eligible.future_return_5d.rank())
            if len(eligible) > 1
            else np.nan
        )
        rank = scores.rank(method="average", pct=True)
        top = set(scores.sort_values(ascending=False, kind="stable").head(20).index)
        stability, rank_change, turnover = np.nan, np.nan, np.nan
        common_count = 0
        if previous is not None and len(scores) and len(previous["scores"]):
            common = scores.index.intersection(previous["scores"].index)
            common_count = len(common)
            if common_count > 1:
                stability = scores.loc[common].rank().corr(previous["scores"].loc[common].rank())
                rank_change = (rank.loc[common] - previous["rank"].loc[common]).abs().mean()
            if top and previous["top"]:
                turnover = 1 - len(top & previous["top"]) / max(len(top), len(previous["top"]))
        records.append(
            {
                "trade_date": str(day.date()),
                "previous_market_session": previous_day,
                "score_rows": len(part),
                "evaluation_rows": len(eligible),
                "rank_ic": ic,
                "score_std_population": scores.std(ddof=0),
                "score_p05": scores.quantile(0.05),
                "score_p95": scores.quantile(0.95),
                "consecutive_common_codes": common_count,
                "rank_stability": stability,
                "mean_absolute_percentile_rank_change": rank_change,
                "top20_membership_change": turnover,
                "missing_label_rows": int((~np.isfinite(part.future_return_5d)).sum()),
                "crossing_label_rows": int(part.label_end_date.gt(bounds[1]).sum()),
            }
        )
        previous, previous_day = {"scores": scores, "rank": rank, "top": top}, str(day.date())
    daily = pd.DataFrame(records)
    metrics = [
        "rank_ic",
        "score_std_population",
        "rank_stability",
        "mean_absolute_percentile_rank_change",
        "top20_membership_change",
    ]
    summary = {
        "bounds": bounds,
        "score_rows": len(frame),
        "evaluation_rows": int(valid.sum()),
        "expected_days": len(days),
        "score_days": int(daily.score_rows.gt(0).sum()),
        "rank_ic_days": int(daily.rank_ic.notna().sum()),
        **{f"mean_{name}": float(daily[name].mean()) for name in metrics},
    }
    return daily, clean_numbers(summary)
