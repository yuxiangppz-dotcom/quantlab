from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_data import (
    contained_mask,
    fit_scaler_inplace,
    labels_from_raw,
    prediction_mask,
    transform,
)
from quantlab.research.alpha158_rolling_metrics import daily_diagnostics
from quantlab.research.alpha158_rolling_protocol import FitLedger
from quantlab.research.alpha158_store import exclusive_job


def fixture_labels():
    sessions = pd.bdate_range("2022-12-19", periods=20)
    raw = pd.DataFrame(
        {
            "instrument_id": "000001.SZ",
            "trade_date": sessions,
            "close": np.arange(20) + 10.0,
            "adj_factor": 2.0,
        }
    )
    inv = {
        "sessions": sessions.strftime("%Y-%m-%d").tolist(),
        "list_dates": {"000001.SZ": "2020-01-01"},
        "delist_dates": {"000001.SZ": None},
    }
    return raw, inv


def test_exact_endpoint_and_purged_boundary_do_not_fallback_to_rows():
    raw, inv = fixture_labels()
    raw = raw.drop(index=5)
    labels = labels_from_raw(raw, raw, inv).assign(complete_features=True)
    assert np.isnan(labels.iloc[0].future_return_5d)
    assert labels.iloc[1].future_return_5d == pytest.approx(16 / 11 - 1)
    assert str(labels.iloc[0].label_end_date.date()) == inv["sessions"][5]
    mask = contained_mask(labels, ["2022-12-19", "2022-12-30"])
    assert labels.loc[mask].label_end_date.max() <= pd.Timestamp("2022-12-30")
    assert not mask.iloc[-1]
    pred = prediction_mask(labels, ["2022-12-19", "2023-12-31"])
    assert pred.all()
    labels["future_return_5d"] = np.nan
    labels["label_end_date"] = pd.NaT
    assert pred.equals(prediction_mask(labels, ["2022-12-19", "2023-12-31"]))


def test_future_perturbation_cannot_change_contained_training_or_prediction_membership():
    raw, inv = fixture_labels()
    a = labels_from_raw(raw, raw, inv).assign(complete_features=True)
    raw.loc[raw.trade_date.gt("2022-12-30"), "close"] *= 1000
    b = labels_from_raw(raw, raw, inv).assign(complete_features=True)
    mask = contained_mask(a, ["2022-12-19", "2022-12-30"])
    pd.testing.assert_series_equal(a.loc[mask].future_return_5d, b.loc[mask].future_return_5d)
    assert prediction_mask(a, ["2022-12-19", "2023-12-31"]).equals(
        prediction_mask(b, ["2022-12-19", "2023-12-31"])
    )


@pytest.mark.parametrize("change", ["unknown_lifecycle", "inactive_endpoint", "zero_factor"])
def test_unknown_or_invalid_endpoint_stays_missing(change):
    raw, inv = fixture_labels()
    if change == "unknown_lifecycle":
        inv["list_dates"]["000001.SZ"] = None
    elif change == "inactive_endpoint":
        inv["delist_dates"]["000001.SZ"] = inv["sessions"][4]
    else:
        raw.loc[5, "adj_factor"] = 0
    assert np.isnan(labels_from_raw(raw, raw, inv).iloc[0].future_return_5d)


def test_scaler_only_sees_training_constant_and_saved_transform():
    original = np.array([[1, 2], [3, 2], [5, 2]], dtype="float32", order="F")
    x = original.copy()
    scaler = fit_scaler_inplace(x)
    assert scaler["mean"] == [3, 2]
    assert scaler["scale"][1] == 1
    np.testing.assert_array_equal(x, transform(original, json.loads(json.dumps(scaler))))
    np.testing.assert_allclose(x.mean(axis=0), 0, atol=1e-7)
    future = transform(np.array([[100000.0, -30000.0]], dtype="float32"), scaler)
    assert future[0, 0] > 1000
    assert scaler["mean"] == [3, 2]


def test_six_cumulative_intents_failure_interruption_resume_and_tampering(tmp_path):
    slots = [f"fold{i}_{kind}" for i in (1, 2, 3) for kind in ("ridge", "lightgbm")]
    ledger = FitLedger(tmp_path, "fixed", slots)
    folder = ledger.start(slots[0])
    assert ledger.read(slots[0])["status"] == "interrupted"
    restarted = FitLedger(tmp_path, "fixed", slots)
    with pytest.raises(DataValidationError, match="cannot be retried"):
        restarted.start(slots[0])
    (folder / "model").write_bytes(b"synthetic_model_fixture")
    restarted.finish(slots[0], "completed")
    for slot in slots[1:]:
        restarted.start(slot)
        restarted.finish(slot, "failed")
    assert len(list((tmp_path / "fits").glob("*/started.json"))) == 6
    with pytest.raises(DataValidationError):
        restarted.start("fold4_ridge")
    (folder / "model").write_bytes(b"tampered")
    with pytest.raises(DataValidationError, match="bound file changed"):
        restarted.read(slots[0])
    with pytest.raises(DataValidationError, match="identity changed"):
        FitLedger(tmp_path, "different", slots).read(slots[0])


def test_exclusive_worker_prevents_duplicate_heavy_job(tmp_path):
    with exclusive_job(tmp_path), pytest.raises(DataValidationError, match="another"):
        with exclusive_job(tmp_path):
            pass


def test_metrics_include_unknown_labels_and_do_not_bridge_empty_sessions():
    frame = pd.DataFrame(
        {
            "instrument_id": ["a", "b", "a", "b"],
            "trade_date": pd.to_datetime(["2023-01-02"] * 2 + ["2023-01-04"] * 2),
            "label_end_date": pd.to_datetime(["2023-01-09"] * 4),
            "complete_features": True,
            "score": [1.0, 2.0, 2.0, 1.0],
            "future_return_5d": [np.nan, 0.2, 0.3, 0.1],
        }
    )
    daily, summary = daily_diagnostics(
        frame, ["2023-01-01", "2023-01-31"], ["2023-01-02", "2023-01-03", "2023-01-04"]
    )
    assert summary["score_rows"] == 4
    assert summary["evaluation_rows"] == 3
    assert summary["score_days"] == 2
    assert daily.iloc[1].score_rows == 0
    assert daily.rank_stability.isna().all()
    assert daily.iloc[2].rank_ic == pytest.approx(1.0)


def test_coordinator_limits_arrow_before_any_metadata_or_fit(tmp_path, monkeypatch):
    import pyarrow as pa

    from quantlab.research import alpha158_rolling as rolling

    class StopBeforeWork(Exception):
        pass

    def inspect_before_plan(*args):
        assert pa.cpu_count() == 2
        assert pa.io_thread_count() == 2
        raise StopBeforeWork

    previous = pa.cpu_count(), pa.io_thread_count()
    try:
        pa.set_cpu_count(8)
        pa.set_io_thread_count(8)
        monkeypatch.setattr(rolling, "prepare_plan", inspect_before_plan)
        with pytest.raises(StopBeforeWork):
            rolling.run(tmp_path)
        assert not list(tmp_path.rglob("started.json"))
    finally:
        pa.set_cpu_count(previous[0])
        pa.set_io_thread_count(previous[1])
