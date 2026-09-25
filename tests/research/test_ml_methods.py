"""Analytic/statistical references and real quantity-path method comparisons."""

import json
import math
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest
from test_ml_v2 import replay_fixture, small_config

from quantlab.research.ml.artifacts import complete
from quantlab.research.ml.baselines import compare_pool_baseline
from quantlab.research.ml.diagnostics import signal_decay
from quantlab.research.ml.io import sha256, write_json
from quantlab.research.ml.runner import run_scenarios
from quantlab.research.ml.selection import selection_diagnostics, trial_inventory
from quantlab.research.ml.statistics import (
    deflated_sharpe,
    expected_maximum_sharpe,
    probabilistic_sharpe,
    return_moments,
)
from quantlab.research.ml.strategies import EligibleEqualWeightPolicy, portfolio_policy
from quantlab.research.ml.study import initialize, reserve


def test_psr_analytic_normal_reference_and_symmetry():
    assert probabilistic_sharpe(0, 101, 0, 3) == 0.5
    # z = 1 by construction; independently known standard-normal CDF.
    sr = math.sqrt(1 / 99.5)
    assert probabilistic_sharpe(sr, 101, 0, 3) == pytest.approx(0.8413447460685429)
    assert probabilistic_sharpe(-sr, 101, 0, 3) == pytest.approx(0.1586552539314571)
    assert deflated_sharpe(sr, 101, 0, 3, trials=1, trial_sharpe_std=0.2) == pytest.approx(
        probabilistic_sharpe(sr, 101, 0, 3)
    )


def test_expected_maximum_matches_independent_gaussian_simulation():
    maxima = np.random.default_rng(317).normal(size=(30000, 100)).max(axis=1)
    assert expected_maximum_sharpe(100, 1) == pytest.approx(maxima.mean(), abs=0.05)
    ps = [deflated_sharpe(0.1, 252, -0.2, 4, trials=n, trial_sharpe_std=0.06) for n in (1, 5, 100)]
    assert ps[0] > ps[1] > ps[2]


@pytest.mark.parametrize("args", [(0, 2, 0, 3), (0, 100, 2, 3), (float("nan"), 100, 0, 3)])
def test_invalid_psr_inputs_rejected(args):
    with pytest.raises(ValueError):
        probabilistic_sharpe(*args)


@pytest.mark.parametrize("values", [[0.0] * 5, [0.0, 0.1], [0.0, -1.0, 0.1], [0.0, 0.1, np.nan]])
def test_degenerate_returns_do_not_produce_confidence(values):
    with pytest.raises(ValueError):
        return_moments(values)


def test_moments_have_pearson_convention_and_sample_sharpe():
    values = [-0.02, -0.01, 0.01, 0.02]
    moments = return_moments(values)
    assert moments["sharpe_per_observation"] == 0
    assert moments["skewness"] == 0
    assert moments["pearson_kurtosis"] == pytest.approx(1.36)


def test_decay_exact_endpoints_and_calendar_gaps():
    sessions = pd.bdate_range("2024-01-02", periods=65)
    prices = pd.DataFrame(
        [
            {
                "trade_date": day,
                "instrument_id": str(code),
                "adj_close": 10 * np.exp(0.0001 * code * i),
            }
            for i, day in enumerate(sessions)
            for code in range(1, 31)
        ]
    )
    scores = pd.DataFrame(
        [
            {"trade_date": day, "instrument_id": str(code), "score": code, "model": "test"}
            for i, day in enumerate(sessions[:40])
            if i != 5
            for code in range(1, 31)
        ]
    )
    prices = prices.loc[~(prices.trade_date.eq(sessions[2]) & prices.instrument_id.eq("30"))]
    daily, persistence, summary = signal_decay(
        scores, prices, sessions, small_config(), horizons=(1, 5), lags=(1, 5)
    )
    first = daily.loc[daily.trade_date.eq(sessions[0]) & daily.horizon_sessions.eq(1)].iloc[0]
    assert first.rank_ic == pytest.approx(1)
    assert first.valid_labels == 29 and first.label_coverage == pytest.approx(29 / 30)
    gap = persistence.loc[persistence.trade_date.eq(sessions[6]) & persistence.lag_sessions.eq(1)]
    assert gap.rank_autocorrelation.isna().all()  # Cannot compare day 6 with day 4.
    assert persistence.rank_autocorrelation.dropna().eq(1).all()
    assert summary["horizons"][0]["uncertainty"]["status"] == "computed"
    # Outcomes cannot alter score persistence or top membership.
    _, changed, _ = signal_decay(
        scores, prices.assign(adj_close=1), sessions, small_config(), horizons=(1, 5), lags=(1, 5)
    )
    pd.testing.assert_frame_equal(persistence, changed)


def test_diagnostics_do_not_consume_holdout_outcomes_after_evaluation_end():
    from test_ml_v2 import synthetic_panel

    days, panel = synthetic_panel()
    cutoff = days[75]
    scores = panel.loc[
        panel.trade_date.between(days[65], cutoff), ["trade_date", "instrument_id"]
    ].assign(model="test", score=1.0)
    prices = panel[["trade_date", "instrument_id", "adj_close"]]
    before = signal_decay(scores, prices, days, small_config(), evaluation_end=cutoff)
    changed = prices.copy()
    changed.loc[changed.trade_date.gt(cutoff), "adj_close"] *= 10
    after = signal_decay(scores, changed, days, small_config(), evaluation_end=cutoff)
    pd.testing.assert_frame_equal(before[0], after[0])
    tail = before[0].loc[before[0].trade_date.eq(cutoff)]
    assert tail.mature_count.eq(0).all() and tail.valid_labels.eq(0).all()


def test_fold_evaluation_tail_cannot_read_beyond_registered_period(tmp_path):
    from test_ml_v2 import synthetic_panel

    from quantlab.research.ml.data import monthly_folds
    from quantlab.research.ml.panel import fold_panel

    days, panel = synthetic_panel()
    config = small_config()
    panel.to_parquet(tmp_path / "features.parquet", index=False)
    panel[["trade_date", "instrument_id", "adj_close"]].to_parquet(
        tmp_path / "prices.parquet", index=False
    )
    fold = monthly_folds(days, days[65], days[75], config)[-1]
    result = fold_panel(tmp_path, fold, ["f1", "f2"], days, config, label_cutoff=days[75])
    assert result.loc[result.label_end.gt(days[75]), "raw_label"].isna().all()
    assert result.loc[result.label_end.le(days[75]), "raw_label"].notna().any()


def baseline_fixture():
    days, market, universe, scores, marks, config = replay_fixture()
    universe = universe.assign(can_open=True, must_exit=False, soft_exit=False)
    scores["model"] = "ridge"
    config = replace(config, models=("ridge",))
    config = replace(config, min_trade_fen=300000)
    pool_config = replace(config, max_positions=3, entry_rank=3, exit_rank=4)
    pool_scores = universe[["trade_date", "instrument_id"]].assign(
        score=np.nan, model="eligible_equal_weight"
    )
    return days, market, universe, scores, marks, config, pool_config, pool_scores


def test_equalweight_risk_and_liquidity_do_not_depend_on_alpha():
    days, _, universe, _, _, _, config, _ = baseline_fixture()
    cross = universe.loc[universe.trade_date.eq(days[0])].copy()
    cross["score"] = [np.nan, float("inf"), -1000]
    policy = EligibleEqualWeightPolicy()
    first = policy.decide(days[0], cross, {}, {}, config, scheduled=True)
    assert [p.target_weight for p in first.target.positions] == pytest.approx([0.8 / 3] * 3)
    cross.loc[cross.instrument_id.eq("A"), ["must_exit", "eligible", "can_open"]] = [
        True,
        False,
        False,
    ]
    cross.loc[cross.instrument_id.eq("B"), "can_open"] = False
    out = policy.decide(
        days[0], cross, {"A": 0.2, "B": 0.2}, {"A": 0, "B": 0}, config, scheduled=False
    )
    assert [(p.instrument_id, p.target_weight) for p in out.target.positions] == [("B", 0.2)]
    assert out.risk_reduction_one_way_turnover == pytest.approx(0.1)
    with pytest.raises(ValueError, match="unknown portfolio"):
        portfolio_policy("typo")


def replay_pair(tmp_path):
    days, market, universe, scores, marks, config, pool_config, pool_scores = baseline_fixture()
    kwargs = dict(
        start=days[1],
        end=days[6],
        capitals_fen=[1000000, 10000000],
        binding_extra={"code": {"synthetic": True}},
    )
    for name, values, cfg, mode in (
        ("replay", scores, config, "backtest"),
        ("baseline", pool_scores, pool_config, "eligible_equal_weight"),
    ):
        run_scenarios(
            values,
            universe,
            days,
            market,
            marks,
            config=cfg,
            output=tmp_path / name,
            strategy_mode=mode,
            **kwargs,
        )
    return tmp_path / "replay", tmp_path / "baseline"


def test_baseline_runs_real_lots_cash_fees_and_same_period_comparison(tmp_path):
    replay, baseline = replay_pair(tmp_path)
    result = compare_pool_baseline(replay, baseline)
    assert all(r["status"] == "compared" for r in result["scenarios"].values())
    a = pd.read_parquet(baseline / "eligible_equal_weight-10000000fen/attempts.parquet")
    assert set(a.instrument_id) == {"A", "B", "C"}
    assert a.commission_fen.sum() > 0 and all(a.simulated % 100 == 0)
    assert result["scenarios"]["ridge-1000000fen"]["baseline_mean_gross_exposure"] == 0
    # A zero-investment result is disclosed; cannot masquerade as a full index holding.
    assert not result["scenarios"]["ridge-1000000fen"]["selection_alpha_identified"]


def test_baseline_rejects_different_cost_evidence(tmp_path):
    replay, baseline = replay_pair(tmp_path)
    path = baseline / "intent.json"
    payload = json.loads(path.read_text())
    payload["inputs"]["market"] = "different_fees"
    path.write_text(json.dumps(payload))
    (baseline / "completed.json").unlink()
    complete(baseline)
    with pytest.raises(ValueError, match="market"):
        compare_pool_baseline(replay, baseline)


def test_study_counts_retries_once_models_separately_and_failed_trials(tmp_path):
    initialize(tmp_path / "study", date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 1))
    spec = {"config": {"models": ["ridge", "lightgbm"]}, "output": "a"}
    for output in ("a", "a", "b"):
        reserve(tmp_path / "study", "2023-01-01", "2023-12-31", {**spec, "output": output})
    counts, _ = trial_inventory(tmp_path / "study")
    assert counts["registered_invocations"] == 3 and counts["registered_candidate_count"] == 2
    reserve(tmp_path / "study", "2023-01-01", "2023-12-31", {**spec, "new_seed": 2})
    assert trial_inventory(tmp_path / "study")[0]["registered_candidate_count"] == 4


def synthetic_selection(tmp_path, models=("ridge", "lightgbm")):
    study, training, replay = [tmp_path / p for p in ("study", "training", "replay")]
    initialize(study, date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 1))
    specification = {
        "config": {"models": list(models)},
        "manifest": {},
        "code": {},
        "start": "2023-01-01",
        "end": "2023-12-31",
        "output": str(training),
    }
    reserve(study, specification["start"], specification["end"], specification)
    write_json(
        training / "intent.json",
        {
            "config": specification["config"],
            "inputs": {},
            "code": {},
            "start": specification["start"],
            "end": specification["end"],
        },
    )
    complete(training)
    statuses = []
    sessions = pd.bdate_range("2023-01-02", "2023-12-29").strftime("%Y-%m-%d").tolist()
    rng = np.random.default_rng(777)
    for i, model in enumerate(models):
        folder = replay / f"{model}-100000fen"
        folder.mkdir(parents=True)
        pd.DataFrame(
            {"session": sessions, "daily_return": rng.normal(0.0002 * i, 0.01, len(sessions))}
        ).to_parquet(folder / "ledger.parquet", index=False)
        statuses.append(
            {
                "model": model,
                "capital_fen": 100000,
                "stop_reason": None,
                "valid_through": "2023-12-29",
            }
        )
    write_json(
        replay / "intent.json",
        {
            "inputs": {
                "strategy_mode": "backtest",
                "end": "2023-12-29",
                "extra": {"training_completion_sha256": sha256(training / "completed.json")},
            }
        },
    )
    write_json(replay / "summary.json", statuses)
    complete(replay)
    return study, training, replay, specification


def test_selection_computes_only_complete_trial_set_and_preserves_failed_attempt(tmp_path):
    study, training, replay, specification = synthetic_selection(tmp_path)
    result = selection_diagnostics(study, training, replay)
    assert result["inventory"]["registered_candidate_count"] == 2
    assert all(r["dsr_iid_raw_trial_proxy"] is not None for r in result["scenarios"].values())
    assert not result["performance_certified"]
    reserve(
        study,
        specification["start"],
        specification["end"],
        {**specification, "config": {**specification["config"], "seed": 17}},
    )
    second = selection_diagnostics(study, training, replay)
    assert second["inventory"]["registered_candidate_count"] == 4
    assert all(r["dsr_iid_raw_trial_proxy"] is None for r in second["scenarios"].values())
    assert len(list((study / "evaluations").glob("*.json"))) == 2  # Retry didn't create new trials.


def test_selection_one_model_is_psr_only(tmp_path):
    study, training, replay, _ = synthetic_selection(tmp_path, ("ridge",))
    row = selection_diagnostics(study, training, replay)["scenarios"]["ridge-100000fen"]
    assert row["psr_iid_zero_reference"] is not None and row["dsr_iid_raw_trial_proxy"] is None


def test_trial_tuple_normalization_and_legacy_hash_are_both_auditable(tmp_path):
    from quantlab.research.ml.artifacts import fingerprint

    study = tmp_path / "study"
    initialize(study, date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 1))
    spec = {"config": {"models": ("ridge", "lightgbm")}}
    reserve(study, "2023-01-01", "2023-12-31", spec)
    write_json(
        study / "trials/legacy.json",
        {
            "specification": spec,
            "specification_sha256": fingerprint(spec),
        },
    )
    assert trial_inventory(study)[0]["registered_candidate_count"] == 2
    path = study / "trials/legacy.json"
    payload = json.loads(path.read_text())
    payload["specification"]["config"]["models"] = ["binary"]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="hash mismatch"):
        trial_inventory(study)


def test_report_failure_does_not_leave_partially_published_output(tmp_path, monkeypatch):
    from quantlab.research.ml import reporting

    def interrupted(replay, benchmark, output, **kwargs):
        write_json(output / "partial.json", {"interrupted": True})
        raise ValueError("missing evidence")

    monkeypatch.setattr(reporting, "_build_report", interrupted)
    output = tmp_path / "report"
    with pytest.raises(ValueError, match="missing evidence"):
        reporting.build_report(None, None, output)
    assert not output.exists()


def test_report_connects_real_baseline_and_exposes_price_index_limit(tmp_path):
    from quantlab.research.ml.reporting import build_report

    replay, baseline = replay_pair(tmp_path)
    ledger = pd.read_parquet(replay / "ridge-10000000fen/ledger.parquet")
    benchmark = tmp_path / "benchmark.parquet"
    ledger[["session"]].assign(benchmark_return=0.0).to_parquet(benchmark, index=False)
    write_json(tmp_path / "benchmark_metadata.json", {"return_basis": "price_index"})
    report = build_report(replay, benchmark, tmp_path / "report", baseline_replay=baseline)
    assert report["same_pool_baseline"]["scenarios"]["ridge-10000000fen"]["status"] == "compared"
    assert report["benchmark_metadata"]["return_basis"] == "price_index"
    assert not report["dividend_comparability_verified"]
