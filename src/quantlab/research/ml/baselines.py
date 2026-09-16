"""Compare independent accounts on the exact same calendar and capital."""

import json

import pandas as pd

from quantlab.research.ml.artifacts import verify_completed


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
