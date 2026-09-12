"""Matched descriptive policy diagnostics over the complete fixed replay timeline."""

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_metrics import daily_diagnostics

KEYS = ["instrument_id", "trade_date"]
METRICS = (
    "rank_ic",
    "score_std_population",
    "rank_stability",
    "mean_absolute_percentile_rank_change",
    "top20_membership_change",
)


def summarize(frame):
    result = {
        "market_days": len(frame),
        "score_rows": int(frame.score_rows.sum()),
        "evaluation_rows": int(frame.evaluation_rows.sum()),
        "rank_ic_days": int(frame.rank_ic.notna().sum()),
    }
    for key in METRICS:
        values = frame[key].dropna()
        result["mean_" + key] = None if values.empty else float(values.mean())
    return result


def policy_diagnostics(frames, config):
    weeks = config["weeks"]
    sessions = [day for week in weeks for day in week["prediction_sessions"]]
    if sessions != sorted(set(sessions)):
        raise DataValidationError("extended timeline duplicated or unordered")
    daily_frames, weekly, monthly, paired = [], [], [], []
    for kind in ("ridge", "lightgbm"):
        parts = {"weekly": [], "monthly": []}
        for week in weeks:
            one_week = {}
            for policy, week_id in (
                ("weekly", week["week_id"]),
                ("monthly", week["monthly_anchor"]),
            ):
                frame = frames[f"{week_id}_{kind}"]
                part = (
                    frame.loc[frame.trade_date.isin(pd.to_datetime(week["prediction_sessions"]))]
                    .sort_values(KEYS)
                    .reset_index(drop=True)
                )
                if (
                    len(part) != week["prediction_rows"]
                    or part.duplicated(KEYS).any()
                    or not np.isfinite(part.score).all()
                ):
                    raise DataValidationError("extended policy member population changed")
                one_week[policy] = part
                parts[policy].append(part)
            cols = [
                *KEYS,
                "complete_features",
                "label_end_date",
                "future_return_5d",
                "label_reason",
            ]
            if not one_week["weekly"][cols].equals(one_week["monthly"][cols]):
                raise DataValidationError("extended policies use different members or labels")
            if week["week_id"] == week["monthly_anchor"] and not np.array_equal(
                one_week["weekly"].score, one_week["monthly"].score
            ):
                raise DataValidationError(
                    "shared monthly first week must reuse exactly the same artifact"
                )
        policies = {}
        for policy, items in parts.items():
            frame = pd.concat(items, ignore_index=True)
            if len(frame) != config["policy_members_per_model"]:
                raise DataValidationError("extended full policy membership changed")
            daily, _ = daily_diagnostics(
                frame, [sessions[0], weeks[-1]["evaluation_label_cutoff"]], sessions
            )
            if int(daily.evaluation_rows.sum()) != config["policy_evaluation_rows_per_model"]:
                raise DataValidationError("extended policy evaluation population changed")
            daily["model"], daily["policy"] = kind, policy
            mapping = {
                day: week["week_id"] for week in weeks for day in week["prediction_sessions"]
            }
            shared = {
                day
                for week in weeks
                if week["week_id"] == week["monthly_anchor"]
                for day in week["prediction_sessions"]
            }
            daily["week_id"] = daily.trade_date.map(mapping)
            daily["shared_first_week"] = daily.trade_date.isin(shared)
            if (
                len(shared) != config["shared_signal_days_per_model"]
                or len(daily) - len(shared) != config["comparison_signal_days_per_model"]
            ):
                raise DataValidationError("extended fixed frequency-comparison days changed")
            policies[policy] = daily.set_index("trade_date", drop=False)
            daily_frames.append(daily)
            for month, group in daily.groupby(daily.trade_date.str[:7], sort=True):
                monthly.append(
                    {"model": kind, "policy": policy, "month": month, **summarize(group)}
                )
        for week in weeks:
            for policy, daily in policies.items():
                group = daily.loc[week["prediction_sessions"]]
                model_week = week["week_id"] if policy == "weekly" else week["monthly_anchor"]
                if int(group.evaluation_rows.sum()) != week["evaluation_rows"]:
                    raise DataValidationError("extended weekly evaluation population changed")
                weekly.append(
                    {
                        "model": kind,
                        "policy": policy,
                        "week_id": week["week_id"],
                        "model_slot": f"{model_week}_{kind}",
                        "shared_first_week": week["week_id"] == week["monthly_anchor"],
                        **summarize(group),
                    }
                )
            if week["week_id"] != week["monthly_anchor"]:
                a, b = [
                    policies[policy].loc[week["prediction_sessions"]]
                    for policy in ("weekly", "monthly")
                ]
                valid = a.rank_ic.notna() & b.rank_ic.notna()
                difference = a.rank_ic[valid] - b.rank_ic[valid]
                paired.append(
                    {
                        "model": kind,
                        "week_id": week["week_id"],
                        "paired_days": int(valid.sum()),
                        "mean_weekly_minus_monthly_rank_ic": None
                        if difference.empty
                        else float(difference.mean()),
                        "identical_members_and_labels": True,
                    }
                )
    return pd.concat(daily_frames, ignore_index=True), weekly, monthly, paired
