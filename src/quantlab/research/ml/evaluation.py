"""Signal and scenario metrics keep overlapping labels separate from daily NAV."""

from __future__ import annotations

import numpy as np
import pandas as pd


def signal_diagnostics(scores, min_cross_section=20):
    rows = []
    previous = {}
    for (model, day), group in scores.groupby(["model", "trade_date"], sort=True):
        valid = group[np.isfinite(group.score) & np.isfinite(group.raw_label)].copy()
        rank_ic = ic = None
        if len(valid) >= min_cross_section and valid.score.nunique() > 1:
            if valid.raw_label.nunique() > 1:
                rank_ic = float(valid.score.rank().corr(valid.raw_label.rank()))
                ic = float(valid.score.corr(valid.raw_label))
        valid["quantile"] = (
            np.minimum(9, ((valid.score.rank() - 1) / len(valid) * 10).astype(int))
            if len(valid)
            else pd.Series(dtype="int64")
        )
        buckets = valid.groupby("quantile").raw_label.mean()
        row = {
            "model": model,
            "trade_date": day,
            "predictions": len(group),
            "mature_labels": len(valid),
            "rank_ic": rank_ic,
            "ic": ic,
            "label_coverage": len(valid) / len(group),
        }
        row.update({f"q{i + 1}_raw_return": buckets.get(i, np.nan) for i in range(10)})
        row["top_minus_bottom_label_spread"] = buckets.get(9, np.nan) - buckets.get(0, np.nan)
        ranked = group.sort_values(["score", "instrument_id"], ascending=[False, True])
        top = ranked.head(20)
        members = set(top.instrument_id)
        prior = previous.get(model)
        row["top20_membership_change_not_turnover"] = (
            1 - len(members & prior[0]) / max(len(members), len(prior[0])) if prior else np.nan
        )
        row["score_rank_autocorrelation"] = np.nan
        today = group.set_index("instrument_id").score
        if prior:
            common = today.index.intersection(prior[1].index)
            if len(common) >= min_cross_section:
                a, b = today.loc[common], prior[1].loc[common]
                if a.nunique() > 1 and b.nunique() > 1:
                    row["score_rank_autocorrelation"] = a.rank().corr(b.rank())
        previous[model] = members, today
        # Select head by scores first, then report label coverage; never replace a
        # missing-label top name by a lower-ranked stock with a known outcome.
        row["top20_label_coverage"] = float(np.isfinite(top.raw_label).mean())
        row["top20_mean_raw_label"] = top.raw_label.mean()
        row["universe_mean_raw_label"] = valid.raw_label.mean()
        rows.append(row)
    daily = pd.DataFrame(rows)
    summaries = {}
    for model, group in daily.groupby("model"):
        ic = group.rank_ic.dropna()
        std = float(ic.std(ddof=1)) if len(ic) > 1 else np.nan
        summaries[model] = {
            "days": len(group),
            "rank_ic_days": len(ic),
            "rank_ic": float(ic.mean()) if len(ic) else None,
            "rank_icir_unannualized": float(ic.mean() / std) if std > 0 else None,
            "mean_label_coverage": float(group.label_coverage.mean()),
            "overlapping_label_returns_are_not_daily_pnl": True,
            "iid_significance_claim": False,
        }
    return daily, summaries


def scenario_metrics(records, initial_cash_fen):
    """Use actual chronological scenario ledger; do not annualize a stopped suffix."""
    if not records:
        return {"completed_sessions": 0, "total_return": None}
    equity = np.array([initial_cash_fen, *[r.marked_equity_fen for r in records]], dtype=float)
    returns = equity[1:] / equity[:-1] - 1
    std = returns.std(ddof=1) if len(returns) > 1 else 0
    traded = [sum(a.transition.simulated_notional_fen for a in r.attempts) for r in records]
    return {
        "completed_sessions": len(records),
        "total_return": float(equity[-1] / equity[0] - 1),
        "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)),
        "sharpe_zero_rf": float(returns.mean() / std * np.sqrt(252)) if std > 0 else None,
        "mean_one_way_turnover": float(np.mean(0.5 * np.array(traded) / equity[:-1])),
        "modeled_fees_fen": sum(r.modeled_fees_fen for r in records),
        "turnover_denominator": "previous_completed_close_equity",
        "performance_eligible": False,
    }
