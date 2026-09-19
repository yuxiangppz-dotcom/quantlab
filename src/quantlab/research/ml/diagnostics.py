"""Exact-calendar multi-horizon signal diagnostics; research returns, not fills."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from quantlab.research.ml.data import calendar_index, execution_labels


def signal_decay(
    scores,
    prices,
    calendar,
    config,
    *,
    horizons=(1, 5, 10, 20),
    lags=(1, 5, 10, 20),
    evaluation_end=None,
):
    sessions = calendar_index(calendar)
    cutoff = pd.Timestamp(evaluation_end) if evaluation_end is not None else sessions[-1]
    if cutoff not in sessions:
        raise ValueError("evaluation cutoff must be a calendar session")
    if any(type(n) is not int or n < 1 for n in (*horizons, *lags)):
        raise ValueError("positive integer horizons/lags required")
    if len(set(horizons)) != len(horizons) or len(set(lags)) != len(lags):
        raise ValueError("unique horizons/lags required")
    s = scores[["trade_date", "instrument_id", "model", "score"]].copy()
    s["trade_date"] = pd.to_datetime(s.trade_date)
    if s.empty or s.duplicated(["trade_date", "instrument_id", "model"]).any():
        raise ValueError("nonempty unique score identities required")
    if not s.trade_date.isin(sessions).all():
        raise ValueError("scores outside calendar")
    if s.trade_date.gt(cutoff).any():
        raise ValueError("scores cross evaluation boundary")
    # Seed absent price rows with unknown values so missing endpoints stay in coverage.
    p = prices.copy()
    p["trade_date"] = pd.to_datetime(p.trade_date)
    p = p.loc[p.trade_date.le(cutoff)]
    keys = ["trade_date", "instrument_id"]
    missing = s[keys].drop_duplicates().merge(p[keys], on=keys, how="left", indicator=True)
    p = pd.concat([p, missing.loc[missing._merge.eq("left_only"), keys]], ignore_index=True)
    rows = []
    for horizon in horizons:
        label_config = replace(
            config,
            horizon_sessions=horizon,
            train_sessions=max(config.train_sessions, horizon + 2),
            validation_sessions=max(config.validation_sessions, horizon + 2),
        )
        labels = execution_labels(p, sessions, label_config)
        combined = s.merge(labels, on=keys, how="left", validate="many_to_one")
        for (model, day), g in combined.groupby(["model", "trade_date"], sort=True):
            finite_scores = np.isfinite(g.score)
            mature = g.label_end.notna() & g.label_end.le(cutoff)
            valid = finite_scores & mature & np.isfinite(g.raw_label)
            known = g.loc[valid]
            ic = None
            if (
                len(known) >= config.min_cross_section
                and known.score.nunique() > 1
                and known.raw_label.nunique() > 1
            ):
                ic = float(known.score.rank().corr(known.raw_label.rank()))
            rows.append(
                {
                    "model": model,
                    "trade_date": day,
                    "horizon_sessions": horizon,
                    "rank_ic": ic,
                    "score_count": int(finite_scores.sum()),
                    "mature_count": int((finite_scores & mature).sum()),
                    "valid_labels": int(valid.sum()),
                    "label_coverage": float(valid.sum() / finite_scores.sum())
                    if finite_scores.any()
                    else None,
                }
            )
    persistence = []
    indexed = {
        (model, day): group.set_index("instrument_id").score
        for (model, day), group in s.groupby(["model", "trade_date"])
    }
    for (model, day), today in indexed.items():
        pos = sessions.get_loc(day)
        today = today[np.isfinite(today)]
        for lag in lags:
            prior = indexed.get((model, sessions[pos - lag])) if pos >= lag else None
            corr = change = None
            count = 0
            if prior is not None:
                prior = prior[np.isfinite(prior)]
                common = today.index.intersection(prior.index)
                count = len(common)
                if (
                    count >= config.min_cross_section
                    and today[common].nunique() > 1
                    and prior[common].nunique() > 1
                ):
                    corr = float(today[common].rank().corr(prior[common].rank()))
                # Stable code tie break, independent of future label availability.
                a = set(
                    today.sort_index().sort_values(ascending=False, kind="stable").head(20).index
                )
                b = set(
                    prior.sort_index().sort_values(ascending=False, kind="stable").head(20).index
                )
                if a and b:
                    change = 1 - len(a & b) / max(len(a), len(b))
            persistence.append(
                {
                    "model": model,
                    "trade_date": day,
                    "lag_sessions": lag,
                    "common_names": count,
                    "rank_autocorrelation": corr,
                    "top20_membership_change_not_turnover": change,
                }
            )
    daily, autocorrelation = pd.DataFrame(rows), pd.DataFrame(persistence)
    summary = []
    from quantlab.research.ml.reporting import block_mean_interval

    for (model, horizon), group in daily.groupby(["model", "horizon_sessions"]):
        # Calendar reindex keeps missing entire score days from compressing block time.
        series = group.set_index("trade_date").rank_ic.reindex(
            sessions[(sessions >= group.trade_date.min()) & (sessions <= group.trade_date.max())]
        )
        summary.append(
            {
                "model": model,
                "horizon_sessions": int(horizon),
                "uncertainty": block_mean_interval(series, int(horizon) + 1),
                "mean_label_coverage": float(group.label_coverage.mean()),
            }
        )
    return (
        daily,
        autocorrelation,
        {
            "horizons": summary,
            "label_basis": "adjusted_close_t_plus_1_to_t_plus_1_plus_H",
            "evaluation_end": str(cutoff.date()),
            "membership_change_is_not_account_turnover": True,
            "outcomes_do_not_filter_signal_membership": True,
            "multiple_testing_adjusted": False,
            "performance_certified": False,
        },
    )
