"""Frozen weekly membership, matrix assembly and matched replay diagnostics."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_data import batches
from quantlab.research.alpha158_rolling_metrics import daily_diagnostics

KEYS = ["instrument_id", "trade_date"]


def train_mask(meta, week):
    return (
        meta.complete_features
        & meta.trade_date.between(week["train_start"], week["train_end"])
        & meta.label_end_date.le(week["train_end"])
        & np.isfinite(meta.future_return_5d)
    )


def prediction_mask(meta, dates):
    # Labels, their endpoint, and observed outcomes deliberately do not participate.
    return meta.complete_features & meta.trade_date.isin(pd.to_datetime(dates))


def update_membership(digest, meta):
    """Stable ordered identity bytes, independent of pandas/parquet serialization."""
    for code, stamp in meta[KEYS].itertuples(index=False, name=None):
        if not isinstance(code, str) or "\n" in code or "\t" in code:
            raise DataValidationError("invalid membership instrument identity")
        digest.update(f"{code}\t{stamp.date().isoformat()}\n".encode())


def membership(meta):
    digest = hashlib.sha256()
    update_membership(digest, meta)
    return digest.hexdigest()


def matrix_for_week(history, metadata_root, metadata, features, week, expected_hash):
    expected = week["train_rows"]
    x = np.empty((expected, len(features)), dtype="float32", order="F")
    y = np.empty(expected, dtype="float32")
    offset, digest = 0, hashlib.sha256()
    for meta, values in batches(history, metadata_root, metadata, features):
        mask = train_mask(meta, week).to_numpy()
        selected = meta.loc[mask]
        size = len(selected)
        if offset + size > expected:
            raise DataValidationError("weekly training population exceeds frozen count")
        update_membership(digest, selected)
        x[offset : offset + size] = values[mask]
        y[offset : offset + size] = selected.future_return_5d
        offset += size
    if offset != expected or digest.hexdigest() != expected_hash or not np.isfinite(y).all():
        raise DataValidationError("weekly training identities/count/labels changed")
    return x, y


def policy_diagnostics(frames, weeks, sessions):
    """Stitch each policy first so week-boundary model changes remain in stability metrics."""
    rows, daily_frames, paired = [], [], []
    for kind in ("ridge", "lightgbm"):
        anchor = frames[f"week1_{kind}"]
        parts = {"anchor": [], "weekly": []}
        for week in weeks:
            number = week["week"]
            dates = pd.to_datetime(week["prediction_sessions"])
            left = (
                anchor.loc[anchor.trade_date.isin(dates)].sort_values(KEYS).reset_index(drop=True)
            )
            right = frames[f"week{number}_{kind}"]
            right = right.loc[right.trade_date.isin(dates)].sort_values(KEYS).reset_index(drop=True)
            cols = [
                *KEYS,
                "complete_features",
                "label_end_date",
                "future_return_5d",
                "label_reason",
            ]
            if not left[cols].equals(right[cols]) or len(left) != week["prediction_rows"]:
                raise DataValidationError("weekly/anchor prediction members or labels differ")
            if number == 1 and not np.array_equal(left.score, right.score):
                raise DataValidationError("first week policies must share the same artifact")
            parts["anchor"].append(left)
            parts["weekly"].append(right)
        bounds = [weeks[0]["prediction_start"], weeks[-1]["evaluation_label_cutoff"]]
        signal_sessions = [
            day for day in sessions if bounds[0] <= day <= weeks[-1]["prediction_end"]
        ]
        expected_sessions = [day for week in weeks for day in week["prediction_sessions"]]
        if signal_sessions != expected_sessions:
            raise DataValidationError("policy timeline omits a market session")
        policies = {}
        for policy, frames_for_policy in parts.items():
            full = pd.concat(frames_for_policy, ignore_index=True)
            daily, _ = daily_diagnostics(full, bounds, signal_sessions)
            policies[policy] = daily
        for week in weeks:
            number = week["week"]
            pair = []
            for policy, full_daily in policies.items():
                daily = full_daily.loc[
                    full_daily.trade_date.isin(week["prediction_sessions"])
                ].copy()
                if int(daily.evaluation_rows.sum()) != week["evaluation_rows"]:
                    raise DataValidationError("weekly evaluation label population changed")
                daily["model"], daily["policy"], daily["week"] = kind, policy, number
                daily_frames.append(daily)
                summary = {
                    "score_rows": int(daily.score_rows.sum()),
                    "evaluation_rows": int(daily.evaluation_rows.sum()),
                    "expected_days": len(daily),
                    "score_days": int(daily.score_rows.gt(0).sum()),
                    "rank_ic_days": int(daily.rank_ic.notna().sum()),
                }
                for metric in (
                    "rank_ic",
                    "score_std_population",
                    "rank_stability",
                    "mean_absolute_percentile_rank_change",
                    "top20_membership_change",
                ):
                    valid = daily[metric].dropna()
                    summary["mean_" + metric] = None if valid.empty else float(valid.mean())
                rows.append(
                    {
                        "model": kind,
                        "policy": policy,
                        "week": number,
                        "model_slot": f"week{1 if policy == 'anchor' else number}_{kind}",
                        "prediction_start": week["prediction_start"],
                        "prediction_end": week["prediction_end"],
                        **summary,
                    }
                )
                pair.append(daily)
            if number > 1:
                valid = pair[0].rank_ic.notna() & pair[1].rank_ic.notna()
                delta = pair[1].rank_ic[valid] - pair[0].rank_ic[valid]
                paired.append(
                    {
                        "model": kind,
                        "week": number,
                        "paired_days": int(valid.sum()),
                        "mean_weekly_minus_anchor_rank_ic": None
                        if delta.empty
                        else float(delta.mean()),
                        "identical_members_and_labels": True,
                    }
                )
    return rows, pd.concat(daily_frames, ignore_index=True), paired
