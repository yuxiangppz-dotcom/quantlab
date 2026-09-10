from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research import round2_diagnostics as diagnostics
from quantlab.research.models import ResearchDailyPrice
from quantlab.research.price import filter_point_in_time
from quantlab.research.round2_dataset import (
    InputBinding,
    join_session,
    partition_frame,
    sealed_read,
    sealed_write,
    verify_entries,
)
from quantlab.research.round2_features import AUGMENTED_FEATURES


def raw_frame():
    frame = pd.DataFrame(
        {
            "instrument_id": ["300114.SZ", "302132.SZ", "800001.BJ"],
            "trade_date": pd.Timestamp("2025-02-14"),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "amount": 100.0,
        }
    )
    factors = frame[["instrument_id", "trade_date"]].assign(adj_factor=2.0)
    basics = factors.drop(columns="adj_factor").assign(
        total_mv=100.0, circ_mv=50.0, turnover_rate=1.0
    )
    lists = {
        "300114.SZ": date(2010, 1, 1),
        "302132.SZ": date(2025, 2, 17),
        "800001.BJ": date(2020, 1, 1),
    }
    delists = {"300114.SZ": date(2025, 2, 16)}
    return frame, factors, basics, lists, delists


@pytest.mark.parametrize("day", [date(2025, 2, 14), date(2025, 2, 17)])
def test_vector_pit_matches_frozen_filter_and_keeps_exclusions(day):
    daily, factors, basics, lists, delists = raw_frame()
    for frame in (daily, factors, basics):
        frame["trade_date"] = pd.Timestamp(day)
    joined = join_session(daily, factors, basics, lists, delists)
    prices = [
        ResearchDailyPrice(row.instrument_id, day, row.close, 2, row.close * 2)
        for row in daily.itertuples()
    ]
    expected = {
        row.instrument_id
        for row in filter_point_in_time(prices, lists, delists)
        if not row.instrument_id.endswith("BJ")
    }
    assert set(joined.loc[joined.research_exclusion.eq("retained"), "instrument_id"]) == expected
    assert len(joined) == len(daily)
    assert joined.adj_close.eq(20).all()


def test_basic_missing_preserves_terminal_identity_and_unknown_code_errors():
    daily, factors, basics, lists, delists = raw_frame()
    joined = join_session(daily, factors, basics.iloc[1:], lists, delists)
    assert joined.iloc[0].instrument_id == "300114.SZ"
    assert joined.iloc[0].research_exclusion == "retained"
    assert np.isnan(joined.iloc[0].circ_mv)
    with pytest.raises(DataValidationError, match="unknown"):
        join_session(daily, factors, basics, {}, delists)
    with pytest.raises(DataValidationError, match="adj_factor"):
        join_session(daily, factors.iloc[1:], basics, lists, delists)


def test_binding_detects_modified_source_and_partition_date(tmp_path):
    path = tmp_path / "source.parquet"
    daily, *_ = raw_frame()
    daily.to_parquet(path)
    binding = InputBinding(tmp_path)
    partition_frame(binding, path, date(2025, 2, 14))
    binding.check()
    with pytest.raises(DataValidationError, match="date"):
        partition_frame(binding, path, date(2025, 2, 17))
    daily.iloc[:1].to_parquet(path)
    with pytest.raises(DataValidationError, match="changed"):
        binding.check()
    with pytest.raises(DataValidationError, match="changed"):
        binding.read(path)


def test_sealed_artifacts_are_exclusive_and_detect_tampering(tmp_path):
    path = tmp_path / "one.json"
    original = sealed_write(path, {"unknown": None, "status": "failed"})
    assert sealed_read(path) == original
    with pytest.raises(FileExistsError):
        sealed_write(path, {"unknown": 0})
    path.write_text(path.read_text().replace("failed", "passed"))
    with pytest.raises(DataValidationError, match="fingerprint"):
        sealed_read(path)


def synthetic_cohorts():
    config = diagnostics.load_config()
    dates = [date(2021, 1, 1) + timedelta(days=i) for i in range(120)]
    for i, period in enumerate(("discovery", "validation", "test_observed")):
        config[period] = [str(dates[i * 40]), str(dates[i * 40 + 39])]
    frame = pd.DataFrame(
        [
            {
                "instrument_id": f"{i:06d}.SZ",
                "trade_date": day,
                **{name: i + j / 100 for name in AUGMENTED_FEATURES},
                "transparent_combo_v1": float(i),
                "common_features_available": True,
                **{f"future_return_{h}d": i / 1000 for h in (5, 10, 20)},
            }
            for j, day in enumerate(dates)
            for i in range(35)
        ]
    )
    return config, dates, frame


def test_contained_labels_and_common_cohort_counts():
    config, dates, frame = synthetic_cohorts()
    frame.loc[0, "common_features_available"] = False
    frame.loc[1, "future_return_20d"] = np.nan
    cohorts, coverage = diagnostics.contained_cohorts(frame, config, dates, 20)
    assert len(cohorts["discovery"]) == 20 * 35 - 2
    assert coverage["discovery"]["excluded_crossing_label_end"] == 20 * 35
    assert coverage["discovery"]["excluded_missing_or_nonfinite_label"] == 1
    assert coverage["discovery"]["excluded_incomplete_features"] == 1
    assert max(cohorts["discovery"].label_end_date) == dates[39]
    assert coverage["test_observed"]["excluded_unknown_label_end"] == 20 * 35


def test_medians_fit_on_training_only_and_no_zero_fallback():
    training = pd.DataFrame({"one": [1.0, 3.0, np.nan]})
    medians = diagnostics.fit_medians(training, ["one"])
    future = pd.DataFrame({"one": [np.nan, 10000.0, np.inf]})
    assert diagnostics.model_matrix(future, ["one"], medians).one.tolist() == [2.0, 10000.0, 2.0]
    with pytest.raises(DataValidationError, match="no finite"):
        diagnostics.fit_medians(pd.DataFrame({"one": [np.nan]}), ["one"])


class FakeModel:
    def __init__(self, params):
        assert params["n_estimators"] == 200
        assert params["n_jobs"] == 2
        self.booster_ = self

    def fit(self, x, y):
        self.feature_importances_ = np.ones(len(x.columns), dtype=int)
        assert np.isfinite(y).all()

    def predict(self, x):
        return x.iloc[:, 0].to_numpy()

    def save_model(self, path):
        Path(path).write_text("SYNTHETIC UNIT TEST MODEL; NO PERFORMANCE EVIDENCE")


def test_fit_failure_retained_no_second_fit_and_budget_guard(tmp_path):
    config, dates, frame = synthetic_cohorts()
    cohorts, _ = diagnostics.contained_cohorts(frame, config, dates, 5)

    def broken(params):
        raise RuntimeError("synthetic failure")

    prediction, failure = diagnostics.fit_candidate(
        tmp_path, "lightgbm_baseline", 5, cohorts, config, "synthetic", factory=broken
    )
    assert prediction is None and failure["status"] == "failed"
    receipt = sealed_read(tmp_path / "lightgbm_baseline_5d/result.json")
    assert receipt["error"] == "synthetic failure"
    with pytest.raises(FileExistsError):
        diagnostics.fit_candidate(
            tmp_path, "lightgbm_baseline", 5, cohorts, config, "synthetic", factory=FakeModel
        )
    with pytest.raises(DataValidationError, match="budget"):
        diagnostics.fit_candidate(tmp_path, "lightgbm_baseline", 1, cohorts, config, "synthetic")


def test_prediction_lineage_and_train_boundary(tmp_path):
    config, dates, frame = synthetic_cohorts()
    cohorts, _ = diagnostics.contained_cohorts(frame, config, dates, 20)
    predictions, error = diagnostics.fit_candidate(
        tmp_path, "lightgbm_augmented", 20, cohorts, config, "synthetic", factory=FakeModel
    )
    assert error is None
    folder = tmp_path / "lightgbm_augmented_20d"
    prep = sealed_read(folder / "preprocessing.json")
    assert prep["max_label_end_date"] == str(dates[39])
    receipt = sealed_read(folder / "result.json")
    verify_entries(folder, receipt["artifacts"])
    assert len(predictions["validation"]) == len(cohorts["validation"])
    (folder / "model.txt").write_text("tampered")
    with pytest.raises(DataValidationError, match="changed"):
        verify_entries(folder, receipt["artifacts"])


def test_entire_round_discloses_all_comparisons_and_cannot_multiply_fits(tmp_path, monkeypatch):
    config, dates, frame = synthetic_cohorts()
    stage = tmp_path / "stage"
    stage.mkdir()
    frame.to_parquet(stage / "dataset.parquet")
    sealed_write(
        stage / "manifest.json",
        {
            "config": config,
            "inputs": {},
            "artifacts": {},
            "code_head": "synthetic",
            "open_dates": list(map(str, dates)),
            "feature_rows": len(frame),
            "common_feature_rows": len(frame),
            "feature_missing": {},
            "invalid_cap_rows": [],
        },
    )
    monkeypatch.setattr(diagnostics, "load_config", lambda root: config)
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda name: "synthetic-test")
    report = diagnostics.run_diagnostics(
        tmp_path, tmp_path, factory=FakeModel, progress=lambda *a, **kw: None
    )
    assert len(report["comparisons"]) == 21 and report["fit_attempts"] == 6
    assert all(item["status"] == "complete" for item in report["comparisons"])
    assert report["net_return_assessed"] is False and report["drawdown_assessed"] is False
    assert (
        diagnostics.load_report(tmp_path, full_verify=True)["fingerprint"] == report["fingerprint"]
    )
    with pytest.raises(FileExistsError):
        diagnostics.run_diagnostics(tmp_path, tmp_path, factory=FakeModel)


def test_config_budget_mutation_rejected(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "research_round2_v1.json").write_text('{"model_fit_budget": 999}')
    with pytest.raises(DataValidationError, match="config changed"):
        diagnostics.load_config(tmp_path)
