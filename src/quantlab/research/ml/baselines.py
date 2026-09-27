"""Compare independent accounts on the exact same calendar and capital."""

import json
from pathlib import Path

import pandas as pd

from quantlab.research.ml.artifacts import verify_completed


def compare_factor_baseline(replay, factor_replay, *, factor="momentum_20d"):
    """Model vs a same-holdings transparent factor under identical rules.

    Unlike the full-pool baseline, this comparison changes only the score
    source: positions, buffers, limits, fees, frequency and accounting are the
    model configuration's own, so the contrast isolates selection rather than
    capacity or idle cash.
    """
    verify_completed(replay)
    verify_completed(factor_replay)
    model_intent = json.loads((replay / "intent.json").read_text())["inputs"]
    factor_intent = json.loads((factor_replay / "intent.json").read_text())["inputs"]
    if factor_intent["strategy_mode"] != "simple_factor":
        raise ValueError("expected an independently replayed simple-factor baseline")
    for key in (
        "universe",
        "calendar",
        "initial_marks",
        "market",
        "corporate",
        "start",
        "end",
        "capitals_fen",
        "config",
    ):
        if model_intent[key] != factor_intent[key]:
            raise ValueError(f"factor baseline input differs:{key}")
    if factor_intent["extra"]["code"] != model_intent["extra"]["code"]:
        raise ValueError("factor baseline code/runtime differs")
    models = json.loads((replay / "summary.json").read_text())
    factors = {
        r["capital_fen"]: r
        for r in json.loads((factor_replay / "summary.json").read_text())
    }
    rows = {}
    for model in models:
        capital = model["capital_fen"]
        name = f"{model['model']}-{capital}fen"
        factor = factors.get(capital)
        if (
            not factor
            or factor["stop_reason"]
            or model["stop_reason"]
            or factor["valid_through"] != model_intent["end"]
            or model["valid_through"] != model_intent["end"]
        ):
            rows[name] = {"status": "incomplete_account_not_comparable"}
            continue
        left = pd.read_parquet(replay / name / "ledger.parquet").sort_values("session")
        right = pd.read_parquet(
            factor_replay / f"momentum_20d-{capital}fen/ledger.parquet"
        ).sort_values("session")
        if left.empty or left.session.tolist() != right.session.tolist():
            rows[name] = {"status": "calendar_mismatch_not_comparable"}
            continue
        from quantlab.research.ml.reporting import block_mean_interval, comparison_metrics

        metrics = comparison_metrics(left.daily_return, right.daily_return)
        block = max(
            model_intent["config"]["horizon_sessions"] + 1,
            model_intent["config"]["rebalance_sessions"],
        )
        rows[name] = {
            "status": "compared",
            **metrics,
            "factor_mean_gross_exposure": float(right.gross_exposure.mean()),
            "factor_mean_one_way_turnover": float(right.one_way_turnover.mean()),
            "factor_fees_fen": int(right.fees_fen.sum()),
            "daily_return_difference_interval": block_mean_interval(
                left.daily_return.to_numpy() - right.daily_return.to_numpy(), block
            ),
            "selection_alpha_identified": False,
            "interpretation": (
                "identical rules and capacity; residual differences still "
                "include path-dependent fills and turnover timing"
            ),
        }
    return {
        "scenarios": rows,
        "factor": "momentum_20d",
        "same_holdings_same_rules": True,
    }


def momentum_20d_scores(bundle, sessions):
    """Transparent same-holdings factor baseline scores: 20-session momentum.

    The score is adjusted_close(t) / adjusted_close(t-20) - 1 computed from the
    sealed bundle prices; the specification is frozen code with no fitting, so
    ``fit_asof`` conservatively records the prior session (prices themselves
    are lineage-gated to the decision cutoff by the bundle).
    """
    prices = pd.read_parquet(
        Path(bundle) / "prices.parquet",
        columns=["trade_date", "instrument_id", "adj_close"],
    )
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices = prices.sort_values(["instrument_id", "trade_date"], kind="mergesort")
    momentum = prices.groupby("instrument_id", sort=False)["adj_close"].transform(
        lambda s: s / s.shift(20) - 1
    )
    index = pd.DatetimeIndex(sessions)
    position = index.get_indexer(prices["trade_date"])
    if (position < 0).any():
        raise ValueError("bundle price session outside the sealed calendar")
    fit_asof = [index[max(i - 1, 0)] for i in position]
    scores = prices[["trade_date", "instrument_id"]].copy()
    scores["score"] = momentum.to_numpy()
    scores["model"] = "momentum_20d"
    scores["fit_asof"] = fit_asof
    return scores


def compare_pool_baseline(replay, baseline):
    from quantlab.research.ml.reporting import block_mean_interval, comparison_metrics

    verify_completed(replay)
    verify_completed(baseline)
    model_intent = json.loads((replay / "intent.json").read_text())["inputs"]
    pool_intent = json.loads((baseline / "intent.json").read_text())["inputs"]
    if pool_intent["strategy_mode"] != "eligible_equal_weight":
        raise ValueError("expected independently replayed eligible equal-weight policy")
    for key in (
        "universe",
        "calendar",
        "initial_marks",
        "market",
        "corporate",
        "start",
        "end",
        "capitals_fen",
    ):
        if model_intent[key] != pool_intent[key]:
            raise ValueError(f"baseline accounting input differs:{key}")
    allowed = {"max_positions", "entry_rank", "exit_rank"}
    if {k: v for k, v in model_intent["config"].items() if k not in allowed} != {
        k: v for k, v in pool_intent["config"].items() if k not in allowed
    }:
        raise ValueError("baseline execution/turnover/risk configuration differs")
    if pool_intent["extra"]["code"] != model_intent["extra"]["code"]:
        raise ValueError("baseline code/runtime differs")
    models = json.loads((replay / "summary.json").read_text())
    pools = {r["capital_fen"]: r for r in json.loads((baseline / "summary.json").read_text())}
    rows = {}
    for model in models:
        capital = model["capital_fen"]
        name = f"{model['model']}-{capital}fen"
        pool = pools.get(capital)
        if (
            not pool
            or pool["stop_reason"]
            or model["stop_reason"]
            or pool["valid_through"] != model_intent["end"]
            or model["valid_through"] != model_intent["end"]
        ):
            rows[name] = {"status": "incomplete_account_not_comparable"}
            continue
        left = pd.read_parquet(replay / name / "ledger.parquet").sort_values("session")
        right = pd.read_parquet(baseline / f"eligible_equal_weight-{capital}fen/ledger.parquet")
        right = right.sort_values("session")
        if left.empty or left.session.tolist() != right.session.tolist():
            rows[name] = {"status": "calendar_mismatch_not_comparable"}
            continue
        metrics = comparison_metrics(left.daily_return, right.daily_return)
        block = max(
            model_intent["config"]["horizon_sessions"] + 1,
            model_intent["config"]["rebalance_sessions"],
        )
        rows[name] = {
            "status": "compared",
            **metrics,
            "baseline_mean_gross_exposure": float(right.gross_exposure.mean()),
            "baseline_mean_one_way_turnover": float(right.one_way_turnover.mean()),
            "baseline_fees_fen": int(right.fees_fen.sum()),
            "baseline_risk_breach_days": int(right.risk_breaches.gt(0).sum()),
            "daily_return_difference_interval": block_mean_interval(
                left.daily_return.to_numpy() - right.daily_return.to_numpy(), block
            ),
            "selection_alpha_identified": False,
            "interpretation": "includes concentration, exposures, cash and execution differences",
        }
    return {
        "scenarios": rows,
        "policy": "eligible_equal_weight",
        "position_count_differs_from_concentrated_model": True,
        "risk_constraints_may_break_equal_weights": True,
        "lot_minimum_trade_and_fee_constraints_may_leave_cash": True,
    }
