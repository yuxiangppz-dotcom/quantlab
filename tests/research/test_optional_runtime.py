from __future__ import annotations

import os

import pandas as pd
import pytest

from quantlab.research.qlib_adapter import qlib_integration_status, to_qlib_static_loader

pytestmark = pytest.mark.skipif(
    os.environ.get("QUANTLAB_TEST_OPTIONAL_RESEARCH") != "1",
    reason="optional research runtimes are exercised by the dedicated CI job",
)


def test_fixed_rolling_qlib_models_synthetic_fit_and_saved_prediction(tmp_path):
    import json
    import pickle

    import numpy as np
    from qlib.workflow import R

    from quantlab.daily.service import PROJECT_ROOT
    from quantlab.research.alpha158_rolling import init_qlib
    from quantlab.research.alpha158_rolling_data import fit_scaler_inplace, transform
    from quantlab.research.alpha158_rolling_models import ArrayDataset, make_model

    config = json.loads((PROJECT_ROOT / "config/alpha158_rolling_v1.json").read_text())
    random = np.random.default_rng(417)
    original = np.asarray(random.normal(size=(800, 158)), dtype="float32", order="F")
    y = (original[:, 0] * 0.1 + original[:, 2] * 0.05).astype("float32")
    init_qlib(tmp_path / "qlib", experiment_name="synthetic_compatibility")
    for kind in ("ridge", "lightgbm"):
        x = original.copy(order="F")
        scaler = fit_scaler_inplace(x) if kind == "ridge" else None
        model = make_model(kind, config)
        with R.start(experiment_name="synthetic_compatibility", recorder_name=kind):
            if kind == "lightgbm":
                model.fit(ArrayDataset(x, y), verbose_eval=0)
                assert model.model.current_iteration() == 100
            else:
                model.fit(ArrayDataset(x, y))
                assert model.n_iter_[0] <= 200
            path = tmp_path / f"{kind}.pkl"
            path.write_bytes(pickle.dumps(model))
            R.save_objects(local_path=str(path))
            assert path.name in R.get_recorder().list_artifacts()
        dataset = ArrayDataset(transform(original[:10], scaler))
        prediction = model.predict(dataset)
        np.testing.assert_array_equal(prediction, pickle.loads(path.read_bytes()).predict(dataset))
        assert np.isfinite(prediction).all()
        assert len(prediction) == 10


def test_lightgbm_native_runtime_can_fit_and_predict() -> None:
    from lightgbm import LGBMRegressor

    features = pd.DataFrame(
        {
            "feature_a": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
            "feature_b": [5.0, 4.0, 3.0, 2.0, 1.0, 0.0],
        }
    )
    label = pd.Series([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    model = LGBMRegressor(
        n_estimators=8,
        learning_rate=0.1,
        num_leaves=4,
        max_depth=2,
        min_child_samples=1,
        verbosity=-1,
        deterministic=True,
        n_jobs=1,
        random_state=20260910,
    )
    model.fit(features, label)
    predictions = model.predict(features)

    assert len(predictions) == len(features)
    assert all(pd.notna(predictions))


def test_qlib_static_loader_accepts_quantlab_dataframe_without_market_download() -> None:
    frame = pd.DataFrame(
        {
            "instrument_id": ["000001.SZ", "600000.SH"],
            "trade_date": ["2026-09-09", "2026-09-09"],
            "feature_a": [1.0, -1.0],
            "future_return_5d": [999.0, -999.0],
        }
    )
    status = qlib_integration_status()
    assert status["qlib_available"] is True
    assert status["qlib_sample_data_downloaded"] is False
    assert status["qlib_backtest_used"] is False

    loader = to_qlib_static_loader(frame, ["feature_a"])
    loaded = loader.load()

    assert len(loaded) == 2
    assert list(loaded.columns.get_level_values(0).unique()) == ["feature"]
    assert "future_return_5d" not in loaded.columns.get_level_values(-1)


def test_native_alpha158_transport_golden_split_missing_and_causal_fixtures(tmp_path):
    from quantlab.research.alpha158_audit import synthetic_fixture_checks
    from quantlab.research.alpha158_native import load_contract

    result = synthetic_fixture_checks(tmp_path / "synthetic_only", load_contract())
    assert result["native_features_checked"] == 158
    assert result["all_provider_parity"]
    assert result["split_adjustment_invariant"]
    assert result["future_and_label_invariant"] and result["prefix_invariant"]
    assert result["missing_history_masked"] and result["zero_volume_preserved_as_unknown"]


def test_historical_native_batch_handles_unknown_lifecycle_and_causal_samples(
    tmp_path, monkeypatch
):
    import numpy as np
    import pandas as pd

    from quantlab.research import alpha158_staging as staging
    from quantlab.research.alpha158_native import load_contract

    out = tmp_path / "stage"
    raw_folder = out / "raw"
    raw_folder.mkdir(parents=True)
    sessions = pd.bdate_range("2020-01-01", periods=170)
    close = 10 + np.arange(170) / 32 + np.sin(np.arange(170) / 3) / 8
    volume = 10000 + np.arange(170) * 64 + (np.arange(170) % 7) * 256
    one = pd.DataFrame(
        {
            "instrument_id": "000001.SZ",
            "trade_date": sessions,
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "adj_factor": 1.0,
        }
    )
    raw = pd.concat([one, one.assign(instrument_id="600000.SH")], ignore_index=True)
    raw.to_parquet(raw_folder / "batch-0000.parquet", index=False)
    inventory = {
        "sessions": list(sessions.strftime("%Y-%m-%d")),
        "list_dates": {"000001.SZ": "2000-01-01", "600000.SH": None},
        "delist_dates": {"000001.SZ": None, "600000.SH": None},
    }
    config = {"start": "2020-01-01", "end": "2020-12-31"}
    receipt = {"result": {"folder": "stage/raw", "batch_rows": {"0": len(raw)}}}
    monkeypatch.setattr(staging, "frozen_overlap", lambda *args: None)
    folder = out / "features"
    folder.mkdir()
    result = staging.compute_batch(
        tmp_path,
        out,
        folder,
        0,
        ["000001.SZ", "600000.SH"],
        inventory,
        config,
        load_contract(),
        [receipt],
    )
    assert result["target_grid_rows"] == 340
    assert result["target_unknown_lifecycle_rows"] == 170
    assert result["target_active_rows"] == 170
    assert result["target_all158_usable_rows"] == 110
    assert result["sample"]["prefix_invariant"]
    assert result["sample"]["future_and_label_invariant"]
    assert result["sample"]["provider_parity"]
    assert len(result["features"]) == 158
    usable = pd.read_parquet(folder / "usable_features.parquet")
    names = [x["name"] for x in result["features"]]
    assert usable.loc[usable.instrument_id.eq("600000.SH"), names].isna().all().all()


@pytest.mark.parametrize("kind", ["ridge", "lightgbm"])
def test_weekly_worker_synthetic_fit_intent_and_full_saved_replay(tmp_path, monkeypatch, kind):
    """Exercise the new worker end-to-end with artificial features and outcomes only."""
    import hashlib
    import json

    import numpy as np

    from quantlab.daily.service import PROJECT_ROOT
    from quantlab.research import weekly_pilot as worker
    from quantlab.research import weekly_pilot_data as data
    from quantlab.research.alpha158_rolling_protocol import FitLedger, runtime_manifest
    from quantlab.research.alpha158_store import atomic_seal
    from quantlab.research.weekly_pilot_protocol import HEAVY, OUTPUT, inherited_locks

    config = json.loads((PROJECT_ROOT / "config/alpha158_weekly_pilot_v1.json").read_text())
    random = np.random.default_rng(9281)
    values = random.normal(size=(860, 158)).astype("float32")
    days = [day for week in config["weeks"] for day in week["prediction_sessions"]]
    records = [
        {"instrument_id": f"S{i}", "trade_date": pd.Timestamp("2026-07-17")} for i in range(800)
    ]
    records += [
        {"instrument_id": f"S{i}", "trade_date": pd.Timestamp(day)}
        for day in days
        for i in range(4)
    ]
    meta = pd.DataFrame(records)
    meta["label_end_date"] = meta.trade_date + pd.offsets.BDay(5)
    meta["future_return_5d"] = (values[:, 0] * 0.1 + values[:, 2] * 0.05).astype("float64")
    meta["complete_features"] = True
    meta["label_reason"] = "available"
    meta.loc[801, "future_return_5d"] = np.nan
    meta.loc[801, "label_reason"] = "synthetic_missing_endpoint"
    for week in config["weeks"]:
        week["train_rows"] = int(data.train_mask(meta, week).sum())
        week["prediction_rows"] = 20
    config["metadata_root"] = "synthetic_metadata"
    config["history_root"] = "synthetic_history"
    config["membership_sha256"]["week1"]["train"] = data.membership(meta.iloc[:800])
    digest = hashlib.sha256()
    data.update_membership(digest, meta.iloc[800:])
    config["all_weeks_prediction_sha256"] = digest.hexdigest()
    metadata_dir = tmp_path / config["metadata_root"]
    metadata_dir.mkdir()
    atomic_seal(metadata_dir / "metadata.json", {"synthetic": True})
    out = tmp_path / OUTPUT
    out.mkdir(parents=True)
    plan = atomic_seal(
        out / "plan.json",
        {
            "config": config,
            "code_files": {},
            "code_head": "synthetic-only",
            "sources": {"inputs": {}, "runtime": runtime_manifest()},
        },
    )
    monkeypatch.setattr(data, "batches", lambda *args: iter([(meta.copy(), values.copy())]))
    monkeypatch.setattr(worker, "batches", lambda *args: iter([(meta.copy(), values.copy())]))
    slot = f"week1_{kind}"
    ledger = FitLedger(out, plan["fingerprint"], config["slots"])
    ledger.start(slot)
    with inherited_locks([tmp_path / HEAVY, out]) as descriptors:
        worker.fit_worker(tmp_path, slot, descriptors)
    result = worker.validated_worker(tmp_path, plan, slot)
    assert result["prediction_rows"] == 60 and result["saved_model_max_prediction_difference"] == 0
    assert result["train_rows"] == 800 and result["history_already_observed"] is True
    saved = pd.read_parquet(out / "fits" / slot / "predictions.parquet")
    assert len(saved) == 60 and saved.future_return_5d.isna().sum() == 1
    assert (
        result["boosted_rounds"] == 100 if kind == "lightgbm" else result["ridge_n_iter"][0] <= 200
    )
    (out / "fits" / slot / "model.pkl").write_bytes(b"tampered")
    with pytest.raises(Exception, match="bytes changed"):
        worker.validated_worker(tmp_path, plan, slot)
