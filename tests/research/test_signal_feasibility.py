from copy import deepcopy
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research import signal_feasibility as signals
from quantlab.research import signal_portfolio_audit as audit
from quantlab.research.round2_dataset import sealed_write, verify_entries
from quantlab.research.round2_features import BASELINE_FEATURES


class PredictOnly:
    def fit(self, *args, **kwargs):
        raise AssertionError("a saved-model audit must never fit")

    def predict(self, matrix, **kwargs):
        assert list(matrix) == list(BASELINE_FEATURES)
        assert matrix.dtypes.eq("float32").all()
        return matrix.sum(axis=1).to_numpy()


def example():
    frame = pd.DataFrame(
        {
            "instrument_id": ["600000.SH", "600001.SH", "600002.SH", "600003.SH"],
            "trade_date": pd.to_datetime(["2023-01-03", "2024-12-31", "2026-09-10", "2026-09-10"]),
        }
    )
    for number, feature in enumerate(signals.FEATURE_COLUMNS, 1):
        frame[feature] = [number * 0.1, number * 0.2, number * 0.3, np.nan]
    for h in (5, 10, 20):
        frame[f"future_return_{h}d"] = [1.0, np.nan, np.nan, 99.0]
    lists = dict.fromkeys(frame.instrument_id, date(2000, 1, 1))
    models = {
        h: (PredictOnly(), pd.Series(dict.fromkeys(BASELINE_FEATURES, 0.1))) for h in (5, 10, 20)
    }
    return frame, lists, models


def test_future_labels_tail_and_period_boundaries_cannot_select_or_score():
    frame, lists, models = example()
    contract = signals.load_contract()
    selected, excluded = signals.score_cohort(frame, contract, lists, {})
    expected = signals.predict_scores(selected, models)
    assert set(expected.instrument_id) == {"600000.SH", "600001.SH", "600002.SH"}
    assert excluded.reason.tolist() == ["incomplete_features"]
    for variant in (
        frame.drop(columns=[f"future_return_{h}d" for h in (5, 10, 20)]),
        frame.assign(
            future_return_5d=-999,
            future_return_10d=999,
            future_return_20d=np.nan,
            common_features_available=False,
        ),
    ):
        cohort, rejected = signals.score_cohort(variant, contract, lists, {})
        pd.testing.assert_frame_equal(signals.predict_scores(cohort, models), expected)
        pd.testing.assert_frame_equal(excluded, rejected)
    assert expected.filter(like="transparent_combo").nunique(axis=1).eq(1).all()
    assert not expected.execution_eligible.any()


def test_code_switch_delisting_date_inclusive_and_scope():
    frame, _, models = example()
    base = frame.iloc[:1].copy()
    rows = []
    for day in ("2025-02-14", "2025-02-17"):
        for code in ("300114.SZ", "302132.SZ", "800001.BJ"):
            rows.append(base.assign(instrument_id=code, trade_date=pd.Timestamp(day)))
    frame = pd.concat(rows, ignore_index=True)
    lists = {
        "300114.SZ": date(2010, 1, 1),
        "302132.SZ": date(2025, 2, 17),
        "800001.BJ": date(2000, 1, 1),
    }
    selected, rejected = signals.score_cohort(
        frame, signals.load_contract(), lists, {"300114.SZ": date(2025, 2, 14)}
    )
    assert list(zip(selected.instrument_id, selected.trade_date.dt.date, strict=True)) == [
        ("300114.SZ", date(2025, 2, 14)),
        ("302132.SZ", date(2025, 2, 17)),
    ]
    assert len(rejected) == 4
    assert len(signals.predict_scores(selected, models)) == 2
    with pytest.raises(DataValidationError, match="identity"):
        signals.score_cohort(frame, signals.load_contract(), {}, {})


def metadata():
    return (
        {
            "group": "lightgbm_baseline",
            "horizon": 5,
            "features": list(BASELINE_FEATURES),
            "dataset_fingerprint": "stage",
            "target": "future_return_5d",
        },
        {
            "fitted_period": "discovery",
            "dtype": "float32",
            "label_transform": "none",
            "max_signal_date": "2022-12-23",
            "max_label_end_date": "2022-12-30",
            "medians": dict.fromkeys(BASELINE_FEATURES, 1.0),
        },
        {"status": "complete", "finished_at": "2026-09-11T02:00:00+08:00"},
    )


def test_saved_model_boundaries_features_medians_and_real_time():
    intent, prep, result = metadata()
    contract = signals.load_contract()
    signals.validate_model_metadata(intent, prep, result, "stage", 5, contract)
    for field, value in (
        ("max_label_end_date", "2023-01-03"),
        ("medians", dict.fromkeys(BASELINE_FEATURES, np.nan)),
        ("fitted_period", "validation"),
    ):
        bad = {**prep, field: value}
        with pytest.raises(DataValidationError):
            signals.validate_model_metadata(intent, bad, result, "stage", 5, contract)
    with pytest.raises(DataValidationError):
        signals.validate_model_metadata(
            {**intent, "features": ["future_return_5d"]}, prep, result, "stage", 5, contract
        )
    with pytest.raises(DataValidationError):
        signals.validate_model_metadata(
            intent, prep, {**result, "finished_at": "2026-09-11"}, "stage", 5, contract
        )


def test_targets_do_not_replace_missing_future_prices_or_reorder_ties():
    frame, lists, models = example()
    frame.trade_date = pd.Timestamp("2026-09-10")
    selected, _ = signals.score_cohort(frame, signals.load_contract(), lists, {})
    scored = signals.predict_scores(selected, models)
    scored["lightgbm_baseline_5d"] = 1.0
    contract = deepcopy(signals.load_contract())
    contract["target_contract"]["max_names"] = 2
    expected = audit.score_targets(scored, "lightgbm_baseline_5d", contract)
    assert [p.instrument_id for p in expected.positions] == ["600000.SH", "600001.SH"]
    perturbed = scored.sample(frac=1, random_state=2).assign(
        future_return_5d=np.nan, next_price=np.nan, execution_eligible=False
    )
    assert audit.score_targets(perturbed, "lightgbm_baseline_5d", contract) == expected
    assert expected.cash_weight == pytest.approx(0.2)


def test_raw_context_absence_events_and_daily_bar_never_certify_fills():
    ids = ["600000.SH", "600001.SH"]
    daily = pd.DataFrame(
        {
            "instrument_id": ids,
            "open": [10.0, 10.0],
            "close": [11.0, 0.0],
            "high": [12.0, 12.0],
            "low": [9.0, 9.0],
        }
    )
    limits = pd.DataFrame(
        {"instrument_id": ids, "up_limit": [12.0, np.nan], "down_limit": [8.0, np.nan]}
    )
    empty = pd.DataFrame(columns=["instrument_id", "suspend_type"])
    unknown = audit.raw_context(ids, daily, limits, empty, empty)
    assert unknown.valid_ohlc.tolist() == [True, False]
    assert unknown.st_raw_record_count.tolist() == [0, 0]
    assert unknown.market_accessibility.eq("unknown").all()
    events = pd.DataFrame({"instrument_id": [ids[0], ids[0]], "suspend_type": ["S", "R"]})
    observed = audit.raw_context(ids, daily, limits, None, events)
    assert observed.suspension_S_record_count.tolist() == [1, 0]
    assert observed.suspension_R_record_count.tolist() == [1, 0]
    assert observed.st_raw_record_count.isna().all()
    assert not observed.execution_eligible.any()
    assert audit.research_authority()["complete_trading_cost_fen"] is None


def test_missing_rule_intervals_remain_missing():
    rows = audit.rule_coverage([date(2023, 4, 7), date(2023, 4, 10), date(2025, 1, 2)])
    assert rows[0]["covered_sessions"] == 2
    assert rows[2]["covered_sessions"] == 1
    assert all(row["historical_security_board_certified"] is False for row in rows)


def test_contract_and_artifact_tampering_and_authority_escalation(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "research_signal_feasibility_v1.json").write_text('{"model_fit_budget": 6}')
    with pytest.raises(DataValidationError, match="contract"):
        signals.load_contract(tmp_path)
    report = {"contract": signals.load_contract(), "new_fit_attempts": 0, "artifacts": {}}
    for flag in (
        "performance_eligible",
        "execution_authority",
        "fresh_forward_evidence",
        "net_return_assessed",
        "drawdown_assessed",
        "strategy_promoted",
    ):
        report[flag] = False
    sealed_write(tmp_path / "report.json", {**report, "execution_authority": True})
    with pytest.raises(DataValidationError, match="authority"):
        signals.load_feasibility_report(tmp_path)
    data = tmp_path / "model.txt"
    data.write_text("saved model")
    entries = {"model.txt": {"sha256": signals._sha(data)}}
    verify_entries(tmp_path, entries)
    data.write_text("modified model")
    with pytest.raises(DataValidationError, match="changed"):
        verify_entries(tmp_path, entries)


def test_actual_audit_writes_tail_unknowns_and_uses_fee_and_target_interfaces(tmp_path):
    from quantlab.daily.service import PROJECT_ROOT
    from quantlab.data.storage import ParquetStorage
    from quantlab.research.round2_dataset import InputBinding

    storage = ParquetStorage(tmp_path / "data/canonical")
    storage.calendar_path.parent.mkdir(parents=True)
    pd.DataFrame(
        [
            {"exchange": exchange, "trade_date": stamp, "is_open": True}
            for exchange in ("SSE", "SZSE")
            for stamp in pd.date_range("2026-09-09", "2026-09-11")
        ]
    ).to_parquet(storage.calendar_path, index=False)
    contract = signals.load_contract()
    cost = tmp_path / contract["cost_profile"]
    cost.parent.mkdir()
    cost.write_bytes((PROJECT_ROOT / contract["cost_profile"]).read_bytes())
    daily = storage.daily_bars_path(date(2026, 9, 10))
    daily.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "instrument_id": ["600000.SH"],
            "trade_date": [pd.Timestamp("2026-09-10")],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.0],
        }
    ).to_parquet(daily)
    frame, lists, models = example()
    frame = frame.iloc[:2].assign(trade_date=pd.to_datetime(["2026-09-09", "2026-09-10"]))
    frame.instrument_id = "600000.SH"
    selected, _ = signals.score_cohort(frame, contract, lists, {})
    scores = signals.predict_scores(selected, models)
    out = tmp_path / "audit"
    out.mkdir()
    binding = InputBinding(tmp_path)
    result = audit.audit_portfolio_inputs(
        tmp_path, out, scores, contract, binding, progress=lambda *a, **k: None
    )
    binding.check()
    assert result["row_coverage"]["entry_valid_ohlc_missing_or_invalid"] == 1
    assert all(row["missing_exit_raw_price"] == 2 for row in result["target_summary"])
    exported = pd.read_parquet(out / "context/2026-09.parquet")
    assert exported.entry_beyond_data_cutoff.tolist() == [False, True]
    assert exported.entry_st_raw_record_count.isna().all()
    assert result["complete_trading_cost_fen"] is None
    assert all(item["complete_trading_cost_fen"] is None for item in result["fee_examples"])
    assert {item["stamp_duty_rate"] for item in result["fee_examples"]} == {"0", "0.001", "0.0005"}
    assert not pd.read_parquet(out / "hypothetical_targets.parquet").execution_eligible.any()
