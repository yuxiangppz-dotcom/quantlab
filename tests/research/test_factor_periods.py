from __future__ import annotations

import copy
import json
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research import factor_experiment as experiment
from quantlab.research.label_period import select_period_labels, validate_factor_periods


def _sessions():
    # Synthetic calendar; omitted business day represents a declared holiday.
    return [day.date() for day in pd.bdate_range("2026-01-01", periods=31)][1:]


def _config():
    days = _sessions()
    return {
        "label_horizon_sessions": 5,
        "discovery": [days[0].isoformat(), days[9].isoformat()],
        "validation": [days[10].isoformat(), days[19].isoformat()],
        "test_observed": [days[20].isoformat(), days[29].isoformat()],
        "lightgbm": {"n_estimators": 2, "n_jobs": 1},
    }


def _frame():
    days = _sessions()
    frame = pd.DataFrame({
        "instrument_id": ["000001.SZ"] * len(days),
        "trade_date": days,
        "future_return_5d": [0.01] * len(days),
        "transparent_combo_v1": [0.5] * len(days),
    })
    for column in experiment.FEATURE_COLUMNS:
        frame[column] = list(range(len(days)))
    return frame


def test_last_five_signal_sessions_are_excluded_at_each_split():
    periods, evidence = experiment._experiment_periods(_frame(), _config(), _sessions())
    for name, start in (("discovery", 0), ("validation", 10), ("test_observed", 20)):
        assert periods[name]["trade_date"].tolist() == _sessions()[start:start + 5]
        assert periods[name]["label_end_date"].tolist() == _sessions()[start + 5:start + 10]
        counts = evidence["periods"][name]
        assert counts["signal_rows"] == 10
        assert counts["retained_rows"] == 5
    assert evidence["periods"]["discovery"]["excluded_crossing_label_end"] == 5
    assert evidence["periods"]["test_observed"]["excluded_unknown_label_end"] == 5


def test_sparse_rows_do_not_shorten_horizon_and_missing_label_is_not_replaced():
    frame = _frame().iloc[[0, 2, 4, 7]].copy()
    frame.loc[2, "future_return_5d"] = float("nan")
    result, counts = select_period_labels(
        frame, _config()["discovery"], _sessions(), horizon=5, label_column="future_return_5d",
    )
    assert result["trade_date"].tolist() == [_sessions()[0], _sessions()[4]]
    assert result["label_end_date"].tolist() == [_sessions()[5], _sessions()[9]]
    assert counts["excluded_missing_or_nonfinite_label"] == 1
    assert counts["excluded_crossing_label_end"] == 1


def test_holidays_use_declared_sessions_and_exact_end_is_included():
    days = [date(2026, 1, day) for day in (5, 6, 9, 12, 13, 14, 15)]
    frame = _frame().iloc[:2].copy()
    frame["trade_date"] = days[:2]
    selected, _ = select_period_labels(
        frame, ["2026-01-05", "2026-01-14"], days * 2,
        horizon=5, label_column="future_return_5d",
    )
    assert selected["trade_date"].tolist() == [days[0]]
    assert selected["label_end_date"].tolist() == [days[5]]


@pytest.mark.parametrize("bounds", [None, [], ["2026-01-01"], ["x", "y"], [1, 2]])
def test_invalid_period_shape_rejected(bounds):
    config = _config()
    config["discovery"] = bounds
    with pytest.raises(DataValidationError, match="period"):
        validate_factor_periods(config)


@pytest.mark.parametrize("kind", ["inverted", "overlap", "reordered"])
def test_invalid_chronology_rejected(kind):
    config = _config()
    if kind == "inverted":
        config["discovery"].reverse()
    elif kind == "overlap":
        config["validation"][0] = config["discovery"][1]
    else:
        config["discovery"], config["validation"] = config["validation"], config["discovery"]
    with pytest.raises(DataValidationError, match="period"):
        validate_factor_periods(config)


@pytest.mark.parametrize("horizon", [0, -5, True, 5.0, "5", 20, None])
def test_configuration_cannot_claim_a_different_label_horizon(horizon):
    config = _config()
    config["label_horizon_sessions"] = horizon
    with pytest.raises(DataValidationError, match="fixed 5-session"):
        validate_factor_periods(config)


def test_signal_missing_from_calendar_fails_closed():
    with pytest.raises(DataValidationError, match="absent"):
        experiment._experiment_periods(_frame(), _config(), _sessions()[1:])


def test_model_fit_and_preprocessing_never_see_excluded_rows(monkeypatch):
    captured = {}

    class Model:
        def __init__(self, **kwargs):
            pass

        def fit(self, features, labels):
            captured["train"] = features.copy()
            captured["labels"] = labels.copy()

        def predict(self, features):
            captured.setdefault("predicted", []).append(features.copy())
            return [0.0] * len(features)

    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(LGBMRegressor=Model))
    frame = _frame()
    feature = experiment.FEATURE_COLUMNS[0]
    frame.loc[5:9, feature] = 1000000
    frame.loc[10:14, feature] = float("nan")
    metrics, predictions, _ = experiment._train_lightgbm(frame, _config(), _sessions())
    assert captured["train"].index.tolist() == [0, 1, 2, 3, 4]
    assert captured["labels"].index.tolist() == [0, 1, 2, 3, 4]
    assert metrics["train_medians"][feature] == 2.0
    assert captured["predicted"][0][feature].tolist() == [2.0] * 5
    assert predictions.index.tolist() == list(range(10))
    assert len(predictions) == 10
    assert "label_end_date" in predictions


def test_empty_training_is_explicit_without_model_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "lightgbm", None)
    frame = _frame().iloc[5:]
    metrics, predictions, model = experiment._train_lightgbm(frame, _config(), _sessions())
    assert metrics["status"] == "no_contained_training_labels"
    assert model is None and predictions.empty


def test_empty_evaluation_periods_do_not_invent_metrics(monkeypatch):
    class Model:
        def __init__(self, **kwargs):
            pass

        def fit(self, features, labels):
            pass

        def predict(self, features):
            pytest.fail("empty validation/test must not call predict")

    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(LGBMRegressor=Model))
    metrics, predictions, _ = experiment._train_lightgbm(
        _frame().iloc[:10], _config(), _sessions(),
    )
    assert predictions.empty
    assert metrics["validation"]["valid_days"] == 0
    assert pd.isna(metrics["validation"]["mean_rank_ic"])


def test_runner_records_shared_selection_for_all_factor_metrics(tmp_path, monkeypatch):
    config = copy.deepcopy(_config())
    config.update({
        "candidate_budget": len(experiment.FACTOR_REGISTRY),
        "experiment_id": "synthetic_period_test",
        "data_start": "2026-01-01",
        "promotion_threshold": {"validation_mean_rank_ic": 0.02, "validation_positive_ratio": 0.52},
    })
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    frame = _frame()
    monkeypatch.setattr(
        experiment, "build_experiment_frame", lambda *args: (frame, _sessions()[-1]),
    )
    monkeypatch.setitem(sys.modules, "lightgbm", None)
    monkeypatch.setattr(experiment, "qlib_integration_status", lambda: {"qlib_available": False})
    real_metrics = experiment._factor_metrics
    observed = []

    def metrics(selected, column):
        observed.append(selected.index.tolist())
        return real_metrics(selected, column)

    monkeypatch.setattr(experiment, "_factor_metrics", metrics)
    storage = SimpleNamespace(load_trading_calendar=lambda: [
        SimpleNamespace(trade_date=day, is_open=True) for day in _sessions()
    ])
    output = experiment.run_factor_experiment(config_path, storage=storage, output_root=tmp_path)
    assert len(observed) == 33
    assert observed == [list(range(start, start + 5)) for start in (0, 10, 20)] * 11
    summary = json.loads((output / "summary.json").read_text())
    assert summary["label_selection"]["periods"]["discovery"]["retained_rows"] == 5
    assert summary["lightgbm"]["label_selection"] == summary["label_selection"]
    assert summary["test_observed"] is True
    assert summary["performance_claim"] is False
