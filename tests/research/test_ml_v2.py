from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.data import (
    calendar_index,
    date_weights,
    execution_labels,
    monthly_folds,
    transform_labels,
    validate_features,
)
from quantlab.research.ml.evaluation import scenario_metrics, signal_diagnostics
from quantlab.research.ml.io import seal_bundle, verify_bundle, write_json
from quantlab.research.ml.portfolio import buffered_target
from quantlab.research.ml.replay import replay_scores
from quantlab.research.ml.training import walk_forward
from quantlab.research.quantity_kernel import (
    ResearchFeeScenario,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.quantity_scheduler import RawCloseMark, ResearchDay


def small_config(**kwargs):
    base = MLConfig(
        horizon_sessions=2,
        train_sessions=30,
        validation_sessions=15,
        min_cross_section=4,
        models=("ridge",),
        min_data_in_leaf=5,
        max_rounds=12,
        early_stopping_rounds=3,
        min_feature_fraction=0.5,
        max_positions=2,
        entry_rank=2,
        exit_rank=4,
        max_replacements=1,
        min_hold_sessions=2,
        max_weight=0.4,
        max_industry_weight=0.8,
        min_trade_fen=0,
    )
    return replace(base, **kwargs)


def synthetic_panel():
    calendar = pd.bdate_range("2022-01-03", periods=110)
    rng = np.random.default_rng(42)
    rows = []
    for code in range(6):
        price = 10.0
        for i, day in enumerate(calendar):
            price *= 1 + rng.normal(0, 0.01)
            rows.append(
                {
                    "instrument_id": f"S{code}",
                    "trade_date": day,
                    "adj_close": price,
                    "f1": float(np.sin(i + code)),
                    "f2": float(code) if i % 5 else np.nan,
                    "eligible": True,
                    "industry": "test",
                    "feature_available_at": day.tz_localize("Asia/Shanghai")
                    + pd.Timedelta(hours=15, minutes=30),
                }
            )
    return calendar, pd.DataFrame(rows)


def test_labels_start_at_next_close_and_missing_session_does_not_slide():
    days = pd.bdate_range("2024-01-01", periods=6)
    prices = pd.DataFrame(
        {"trade_date": days, "instrument_id": "A", "adj_close": [10, 20, 30, 40, 50, 60]}
    )
    result = execution_labels(prices, days, small_config())
    assert result.iloc[0].raw_label == 1.0  # 40/20 - 1, not 30/10 - 1
    assert result.iloc[0].label_end == days[3]
    missing = execution_labels(prices.drop(index=1), days, small_config())
    assert pd.isna(missing.iloc[0].raw_label)
    assert result.iloc[-1].label_reason == "calendar_tail"
    with pytest.raises(ValueError, match="duplicate"):
        execution_labels(pd.concat([prices, prices.iloc[:1]]), days, small_config())


def test_feature_admission_never_uses_future_label_and_allows_partial_missingness():
    days, panel = synthetic_panel()
    config = small_config()
    frame = validate_features(panel, ["f1", "f2"], days, config)
    assert frame.feature_ok.all()
    panel["future_return"] = 1
    with pytest.raises(ValueError, match="label/target"):
        validate_features(panel, ["f1", "future_return"], days, config)
    bad = panel.copy()
    bad["feature_available_at"] = bad.feature_available_at + pd.Timedelta(hours=1)
    with pytest.raises(ValueError, match="after decision"):
        validate_features(bad, ["f1", "f2"], days, config)
    bad["feature_available_at"] = bad.trade_date
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_features(bad, ["f1", "f2"], days, config)
    with pytest.raises(ValueError, match="ordered"):
        calendar_index(days[::-1])


def test_purging_is_by_label_endpoint_and_prediction_does_not_require_labels():
    days, panel = synthetic_panel()
    config = small_config()
    frame = validate_features(panel.drop(columns="adj_close"), ["f1", "f2"], days, config)
    labels = execution_labels(panel, days, config)
    frame = frame.merge(labels, on=["trade_date", "instrument_id"])
    folds = monthly_folds(days, days[60], days[-1], config)
    for fold in folds:
        train, valid, test = fold.masks(frame)
        assert frame.loc[train, "label_end"].max() < fold.validation_start
        assert frame.loc[valid, "label_end"].max() <= fold.fit_asof < fold.test_start
        changed = frame.copy()
        changed.loc[test, "raw_label"] = np.nan
        assert fold.masks(changed)[2].equals(test)
    assert folds[-1].masks(frame)[2].loc[frame.label_end.isna()].any()


def test_rank_targets_and_equal_date_weights_are_well_defined():
    frame = pd.DataFrame(
        {
            "trade_date": [1, 1, 2, 2, 2],
            "instrument_id": list("ABCDE"),
            "raw_label": [-10, 100, 0, 0, 1000],
        }
    )
    target = transform_labels(frame, small_config())
    assert target.groupby(frame.trade_date).mean().abs().max() < 1e-12
    assert target.iloc[2] == target.iloc[3]
    totals = pd.Series(date_weights(frame)).groupby(frame.trade_date).sum()
    assert totals.iloc[0] == pytest.approx(totals.iloc[1])
    assert frame.raw_label.max() == 1000  # evaluation tails were not deleted


def test_test_label_perturbation_cannot_change_scores(tmp_path):
    days, panel = synthetic_panel()
    config = small_config()
    frame = validate_features(panel.drop(columns="adj_close"), ["f1", "f2"], days, config)
    frame = frame.merge(execution_labels(panel, days, config), on=["trade_date", "instrument_id"])
    start, end = days[60], days[65]  # single monthly fit
    first, fits = walk_forward(frame, ["f1", "f2"], days, start, end, config, tmp_path / "models")
    changed = frame.copy()
    changed.loc[changed.trade_date.ge(start), "raw_label"] = 1e12
    second, _ = walk_forward(changed, ["f1", "f2"], days, start, end, config)
    np.testing.assert_array_equal(first.score, second.score)
    assert fits[0]["validation_label_max"] <= fits[0]["fit_asof"]
    saved = np.load(next((tmp_path / "models").rglob("ridge.npz")), allow_pickle=False)
    assert saved["features"].tolist() == ["f1", "f2"]


@pytest.mark.parametrize("kind", ["lightgbm", "binary", "lambdarank"])
def test_optional_native_lightgbm_objectives(kind, tmp_path):
    pytest.importorskip("lightgbm")
    days, panel = synthetic_panel()
    config = small_config(models=(kind,))
    frame = validate_features(panel.drop(columns="adj_close"), ["f1", "f2"], days, config)
    frame = frame.merge(execution_labels(panel, days, config), on=["trade_date", "instrument_id"])
    scores, fits = walk_forward(frame, ["f1", "f2"], days, days[60], days[65], config, tmp_path)
    assert np.isfinite(scores.score).all()
    assert 1 <= fits[0]["best_iteration"] <= config.max_rounds


def cross(scores, industries=None):
    return pd.DataFrame(
        {
            "instrument_id": list(scores),
            "score": list(scores.values()),
            "eligible": True,
            "industry": industries or ["sector"] * len(scores),
        }
    )


def test_buffer_reduces_churn_and_hard_risk_exit_overrides_holding_age():
    config = small_config()
    rows = cross({"A": 1, "B": 2, "C": 3, "D": 4})
    decision = buffered_target(
        pd.Timestamp("2024-01-01").date(), rows, {"A": 0.4, "B": 0.4}, {"A": 20, "B": 20}, config
    )
    assert decision.planned_one_way_turnover == 0  # A/B still inside exit rank 4
    rows.loc[rows.instrument_id.eq("A"), "eligible"] = False
    decision = buffered_target(
        pd.Timestamp("2024-01-01").date(), rows, {"A": 0.4, "B": 0.4}, {"A": 0, "B": 20}, config
    )
    assert "A" not in {p.instrument_id for p in decision.target.positions}
    assert decision.risk_reduction_one_way_turnover == pytest.approx(0.2)
    assert decision.target.gross_exposure <= 0.8


def test_unknown_held_metadata_stops_and_industry_cap_leaves_cash():
    config = MLConfig()
    rows = cross({f"S{i}": i for i in range(30)})
    decision = buffered_target(pd.Timestamp("2024-01-01").date(), rows, {}, {}, config)
    assert decision.target.gross_exposure == pytest.approx(0.2)
    assert len(decision.target.positions) == 5
    rows["eligible"] = rows.eligible.astype("boolean")
    rows.loc[rows.instrument_id.eq("S0"), "eligible"] = pd.NA
    with pytest.raises(ValueError, match="eligibility unknown"):
        buffered_target(pd.Timestamp("2024-01-01").date(), rows, {"S0": 0.04}, {"S0": 10}, config)


def replay_fixture():
    days = tuple(d.date() for d in pd.bdate_range("2024-01-02", periods=8))
    fees = ResearchFeeScenario(
        "test",
        days[0],
        days[-1],
        Decimal("0.0001"),
        500,
        Decimal(0),
        Decimal("0.0005"),
        Decimal(0),
        0,
        Decimal(0),
    )
    rules = ResearchQuantityRules("test", days[0], days[-1], 100, 100, 100, 100, 1000000, True)
    market = []
    for i in range(1, 7):
        contexts = tuple(
            ResearchSession(
                code,
                days[i],
                days[i + 1],
                days[i],
                True,
                True,
                True,
                1000,
                900,
                1100,
                800,
                1200,
                10**10,
                days[i - 1],
                20,
                10**10,
                10**7,
                Decimal("0.005"),
                rules,
                fees,
            )
            for code in "ABC"
        )
        market.append(
            ResearchDay(
                days[i], (), contexts, tuple(RawCloseMark(c, days[i], 1000) for c in "ABC"), True
            )
        )
    rows, scores = [], []
    for i in range(7):
        for code, score in zip("ABC", ([3, 2, 1] if i == 0 else [1, 3, 2]), strict=True):
            rows.append(
                {"trade_date": days[i], "instrument_id": code, "eligible": True, "industry": "test"}
            )
            scores.append(
                {
                    "trade_date": days[i],
                    "instrument_id": code,
                    "score": score,
                    "fit_asof": days[0] - timedelta(days=1),
                    "model": "test",
                }
            )
    marks = tuple(RawCloseMark(c, days[0], 1000) for c in "ABC")
    config = small_config(
        max_positions=1,
        entry_rank=1,
        exit_rank=2,
        max_weight=0.8,
        max_replacements=1,
        rebalance_sessions=1,
        min_hold_sessions=0,
        max_one_way_turnover=1,
    )
    return days, market, pd.DataFrame(rows), pd.DataFrame(scores), marks, config


def run_fixture(market=None, capital=10_000_000):
    days, original, universe, scores, marks, config = replay_fixture()
    return replay_scores(
        scores,
        universe,
        days,
        market or original,
        marks,
        start=days[1],
        end=days[6],
        initial_cash_fen=capital,
        config=config,
    )


def test_replay_uses_actual_shares_minimum_fees_and_sells_before_buys():
    result = run_fixture()
    assert result.schedule.status == "completed_scenario"
    first = result.schedule.records[0]
    assert first.attempts[0].transition.commission_fen == 800
    assert first.book.cash_fen == 1_999_200
    second = result.schedule.records[1]
    assert [a.order.side for a in second.attempts] == ["sell"]
    assert result.schedule.records[2].attempts[0].order.side == "buy"
    assert second.attempts[0].transition.stamp_fen == 4000
    assert second.book.cash_fen >= 0
    small = run_fixture(capital=1_000_000)
    assert small.schedule.records[0].attempts[0].transition.commission_fen == 500
    assert first.marked_equity_fen / 10_000_000 != (
        small.schedule.records[0].marked_equity_fen / 1_000_000
    )
    metrics = scenario_metrics(result.schedule.records, 10_000_000)
    assert metrics["total_return"] == pytest.approx(
        result.schedule.records[-1].marked_equity_fen / 10_000_000 - 1
    )


def test_failed_sale_cannot_create_extra_position_or_fake_sale_cash():
    _, market, *_ = replay_fixture()
    second = market[1]
    a = replace(second.contexts[0], down_limit_fen=1000, low_fen=1000)
    market[1] = replace(second, contexts=(a, *second.contexts[1:]))
    result = run_fixture(market)
    record = result.schedule.records[1]
    assert {lot.instrument_id for lot in record.book.lots} == {"A"}
    assert record.book.cash_fen == result.schedule.records[0].book.cash_fen
    assert record.attempts[0].transition.simulated_quantity == 0
    assert result.decisions[1]["pretrade_deferred"][0]["reason"] == "no_slot_without_assumed_sale"


def test_unknown_fee_stops_before_any_day_mutation_and_retains_positions():
    _, market, *_ = replay_fixture()
    second = market[1]
    a = replace(second.contexts[0], fees=replace(second.contexts[0].fees, commission_rate=None))
    market[1] = replace(second, contexts=(a, *second.contexts[1:]))
    result = run_fixture(market)
    assert result.schedule.status == "stopped"
    assert result.schedule.stop_reason == "fee_components_unknown:A"
    assert len(result.schedule.records) == 1
    assert {lot.instrument_id for lot in result.schedule.book.lots} == {"A"}


def test_missing_nonrebalance_mark_stops_instead_of_erasing_loss():
    days, market, universe, scores, marks, config = replay_fixture()
    market[1] = replace(market[1], marks=())
    result = replay_scores(
        scores,
        universe,
        days,
        market,
        marks,
        start=days[1],
        end=days[6],
        initial_cash_fen=10_000_000,
        config=replace(config, rebalance_sessions=5),
    )
    assert result.schedule.status == "stopped"
    assert "raw_mark_unknown" in result.schedule.stop_reason


def test_input_binding_detects_changes_and_cannot_overwrite(tmp_path):
    for name in ("features.parquet", "prices.parquet", "calendar.json", "feature_names.json"):
        (tmp_path / name).write_bytes(b"fixture")
    seal_bundle(tmp_path, provenance={"synthetic": True})
    verify_bundle(tmp_path)
    (tmp_path / "features.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="fingerprint changed"):
        verify_bundle(tmp_path)
    with pytest.raises(FileExistsError):
        write_json(tmp_path / "manifest.json", {})


def test_diagnostics_keep_missing_labels_and_do_not_compound_them():
    scores = pd.DataFrame(
        {
            "model": "x",
            "trade_date": [1] * 5,
            "instrument_id": list("ABCDE"),
            "score": range(5),
            "raw_label": [0, 1, 2, 3, np.nan],
        }
    )
    daily, summary = signal_diagnostics(scores, 4)
    assert daily.iloc[0].label_coverage == 0.8
    assert summary["x"]["rank_ic"] == pytest.approx(1)
    assert summary["x"]["overlapping_label_returns_are_not_daily_pnl"] is True


def test_minimum_holding_age_and_discretionary_turnover_cap():
    rows = cross({"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5})
    config = small_config(max_one_way_turnover=0.1)
    day = pd.Timestamp("2024-01-01").date()
    young = buffered_target(day, rows, {"A": 0.4, "B": 0.4}, {"A": 1, "B": 1}, config)
    assert young.planned_one_way_turnover == 0
    mature = buffered_target(day, rows, {"A": 0.4, "B": 0.4}, {"A": 20, "B": 20}, config)
    assert mature.planned_one_way_turnover <= config.max_one_way_turnover
    assert len(mature.target.positions) <= 2


def test_execution_price_gap_is_reported_without_rewriting_fills():
    _, market, *_ = replay_fixture()
    first = market[0]
    a = replace(
        first.contexts[0],
        raw_close_fen=2000,
        low_fen=1900,
        high_fen=2100,
        down_limit_fen=1800,
        up_limit_fen=2200,
    )
    market[0] = replace(
        first,
        contexts=(a, *first.contexts[1:]),
        marks=(replace(first.marks[0], price_fen=2000), *first.marks[1:]),
    )
    result = run_fixture(market)
    assert result.schedule.records[0].book.lots
    assert result.decisions[0]["realized_exposure"]["breaches"]
    assert result.schedule.records[0].attempts[0].transition.simulated_quantity > 0


def test_runner_writes_bound_models_scores_and_does_not_overwrite(tmp_path, monkeypatch):
    import json

    from quantlab.research.ml import runner

    days, panel = synthetic_panel()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    panel.drop(columns="adj_close").to_parquet(bundle / "features.parquet", index=False)
    panel[["trade_date", "instrument_id", "adj_close"]].to_parquet(
        bundle / "prices.parquet", index=False
    )
    write_json(bundle / "calendar.json", days.strftime("%Y-%m-%d").tolist())
    write_json(bundle / "feature_names.json", ["f1", "f2"])
    seal_bundle(bundle, provenance={"synthetic": True})
    config_path = tmp_path / "config.json"
    write_json(config_path, small_config().payload())
    monkeypatch.setattr(runner, "code_identity", lambda root: {"synthetic_test": True})
    out = tmp_path / "run"
    runner.run_training(bundle, config_path, out, days[60].date(), days[65].date(), root=tmp_path)
    completed = json.loads((out / "completed.json").read_text())
    assert "scores.parquet" in completed["artifacts"]
    assert not completed["performance_eligible"]
    assert len(pd.read_parquet(out / "scores.parquet")) == 36
    with pytest.raises(FileExistsError):
        runner.run_training(bundle, config_path, out, days[60], days[65], root=tmp_path)


def test_scenario_output_has_independent_capital_ledgers(tmp_path):
    from quantlab.research.ml.runner import run_scenarios

    days, market, universe, scores, marks, config = replay_fixture()
    scores["model"] = "ridge"
    summaries = run_scenarios(
        scores,
        universe,
        days,
        market,
        marks,
        start=days[1],
        end=days[6],
        capitals_fen=[1_000_000, 10_000_000],
        config=config,
        output=tmp_path / "replays",
    )
    assert len(summaries) == 2
    assert all(r["status"] == "completed_scenario" for r in summaries)
    assert summaries[0]["total_return"] != summaries[1]["total_return"]
    assert len(list((tmp_path / "replays").rglob("attempts.parquet"))) == 2


def test_sealed_history_export_reuses_receipts_and_requires_explicit_context(tmp_path):
    from pathlib import Path

    from quantlab.research.alpha158_native import load_contract
    from quantlab.research.ml.history import export_history
    from quantlab.research.ml.io import sha256
    from quantlab.research.round2_dataset import sealed_write

    root = Path(__file__).resolve().parents[2]
    names = [f["name"] for f in load_contract(root)["features"]]
    history = tmp_path / "history"
    (history / "receipts").mkdir(parents=True)
    (history / "features").mkdir()
    (history / "raw").mkdir()
    days = pd.bdate_range("2024-01-01", periods=10)
    keys = pd.DataFrame({"instrument_id": "A", "trade_date": days})
    features = pd.concat([keys, pd.DataFrame(np.ones((10, 158)), columns=names)], axis=1)
    features.to_parquet(history / "features/usable_features.parquet", index=False)
    keys.assign(close=10.0, adj_factor=1.0).to_parquet(
        history / "raw/batch-0000.parquet", index=False
    )
    for name, rel in (
        ("features-0000", "features/usable_features.parquet"),
        ("raw-0000", "raw/batch-0000.parquet"),
    ):
        path = history / rel
        sealed_write(
            history / "receipts" / f"{name}.json",
            {
                "result": {"batch_rows": {"0": 10}},
                "artifacts": {rel: {"sha256": sha256(path), "bytes": path.stat().st_size}},
            },
        )
    sealed_write(history / "plan.json", {"batches": [["A"]]})
    sealed_write(history / "inventory.json", {"sessions": days.strftime("%Y-%m-%d").tolist()})
    context = keys.iloc[:5].assign(eligible=True, industry="test")
    context["feature_available_at"] = context.trade_date.dt.tz_localize("UTC") + pd.Timedelta(
        hours=7
    )
    context.to_parquet(tmp_path / "context.parquet", index=False)
    out = tmp_path / "bundle"
    export_history(history, tmp_path / "context.parquet", out, root=root)
    verify_bundle(out)
    assert len(pd.read_parquet(out / "features.parquet")) == 5
    assert len(pd.read_parquet(out / "prices.parquet")) == 10


def test_market_json_decoder_retains_unknowns_and_decimal_rates():
    from dataclasses import asdict

    from quantlab.research.ml.io import decode_market_day

    _, market, *_ = replay_fixture()
    day = market[0]
    payload = {
        "session": str(day.session),
        "corporate_processing_complete": None,
        "marks": [{"instrument_id": "A", "price_fen": 1000}],
        "contexts": [asdict(day.contexts[0])],
    }

    def encode(value):
        if isinstance(value, dict):
            return {k: encode(v) for k, v in value.items()}
        if isinstance(value, list):
            return [encode(v) for v in value]
        if hasattr(value, "isoformat") or isinstance(value, Decimal):
            return str(value)
        return value

    decoded = decode_market_day(encode(payload))
    assert decoded.corporate_processing_complete is None
    assert decoded.contexts[0] == day.contexts[0]


def test_quantile_membership_is_frozen_before_missing_outcomes():
    scores = pd.DataFrame(
        {
            "model": "x",
            "trade_date": [1] * 20,
            "instrument_id": [str(i) for i in range(20)],
            "score": range(20),
            "raw_label": np.arange(20, dtype=float),
        }
    )
    scores.loc[0, "raw_label"] = np.nan
    daily, _ = signal_diagnostics(scores)
    assert daily.iloc[0].q1_raw_return == 1
    assert daily.iloc[0].q1_label_coverage == 0.5


def test_admission_does_not_depend_on_execution_day_outcomes():
    _, market, *_ = replay_fixture()
    original = run_fixture()
    day = market[1]
    market[1] = replace(
        day, contexts=(replace(day.contexts[0], market_open=False), *day.contexts[1:])
    )
    blocked = run_fixture(market)
    assert original.decisions[1]["pretrade_deferred"] == blocked.decisions[1]["pretrade_deferred"]
    assert original.decisions[1]["target_weights"] == blocked.decisions[1]["target_weights"]


def test_replay_checkpoints_resume_exact_account_and_detect_changed_inputs(tmp_path, monkeypatch):
    from quantlab.research.ml import artifacts
    from quantlab.research.ml.runner import run_scenarios

    days, market, universe, scores, marks, config = replay_fixture()
    scores["model"] = "ridge"
    kwargs = dict(
        start=days[1],
        end=days[6],
        capitals_fen=[10_000_000],
        config=config,
        output=tmp_path / "replay",
    )
    original = artifacts.checkpoint_write
    counter = 0

    def interrupt(path, payload):
        nonlocal counter
        original(path, payload)
        counter += 1
        if counter == 2:
            raise RuntimeError("synthetic power loss")

    monkeypatch.setattr(artifacts, "checkpoint_write", interrupt)
    with pytest.raises(RuntimeError, match="power loss"):
        run_scenarios(scores, universe, days, market, marks, **kwargs)
    monkeypatch.setattr(artifacts, "checkpoint_write", original)
    changed = scores.copy()
    changed.loc[0, "score"] += 1
    with pytest.raises(ValueError, match="mismatch"):
        run_scenarios(changed, universe, days, market, marks, **kwargs, resume=True)
    resumed = run_scenarios(scores, universe, days, market, marks, **kwargs, resume=True)
    fresh = run_scenarios(
        scores, universe, days, market, marks, **{**kwargs, "output": tmp_path / "fresh"}
    )
    assert resumed == fresh
    pd.testing.assert_frame_equal(
        pd.read_parquet(kwargs["output"] / "ridge-10000000fen" / "positions.parquet"),
        pd.read_parquet(tmp_path / "fresh" / "ridge-10000000fen" / "positions.parquet"),
    )


def test_corporate_dividend_receivable_prevents_fake_ex_date_loss():
    from quantlab.research.ml.corporate import CorporateEvent

    days, market, universe, scores, marks, config = replay_fixture()
    event = CorporateEvent(
        "cash",
        "A",
        "cash_dividend",
        days[1],
        days[2],
        days[4],
        "synthetic",
        net_cash_per_share_fen=Decimal(100),
    )
    for index in range(1, len(market)):
        day = market[index]
        context = replace(day.contexts[0], raw_close_fen=900, low_fen=850)
        market[index] = replace(
            day,
            contexts=(context, *day.contexts[1:]),
            marks=(replace(day.marks[0], price_fen=900), *day.marks[1:]),
        )
    result = replay_scores(
        scores,
        universe,
        days,
        market,
        marks,
        start=days[1],
        end=days[6],
        initial_cash_fen=10_000_000,
        config=replace(config, rebalance_sessions=5),
        corporate_actions=(event,),
    )
    assert result.schedule.status == "completed_scenario"
    assert result.decisions[1]["receivable_fen"] == 800000
    assert (
        result.schedule.records[1].marked_equity_fen == result.schedule.records[0].marked_equity_fen
    )
    assert result.decisions[3]["receivable_fen"] == 0
    assert (
        result.schedule.records[3].book.cash_fen
        == result.schedule.records[0].book.cash_fen + 800000
    )
