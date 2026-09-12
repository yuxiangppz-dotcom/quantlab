"""Synthetic regression: saved scores can depend on prediction batch geometry."""

import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research import extended_frequency_worker as worker
from quantlab.research.extended_frequency_protocol import OLD
from quantlab.research.input_audit import _sha
from quantlab.research.weekly_pilot_data import membership


class BatchDataset:
    def __init__(self, x, index=None):
        self.x, self.index = x, index


class BatchSensitiveModel:
    """Deterministic numerical stand-in; no optional ML runtime or fit method."""

    def __init__(self):
        self.batch_sizes = []

    def predict(self, dataset):
        self.batch_sizes.append(len(dataset.x))
        return pd.Series(
            dataset.x[:, 0].astype("float64") + len(dataset.x) / 1000, index=dataset.index
        )


@pytest.mark.parametrize("reuse", [False, True])
def test_union_replay_preserves_computation_groups_and_existing_scores(
    tmp_path, monkeypatch, reuse
):
    monkeypatch.setitem(
        sys.modules,
        "quantlab.research.alpha158_rolling_models",
        SimpleNamespace(ArrayDataset=BatchDataset),
    )
    dates = pd.date_range("2025-01-01", periods=180)
    meta = pd.DataFrame(
        [{"instrument_id": code, "trade_date": d} for code in ("A", "B", "C") for d in dates]
    )
    meta["future_return_5d"] = 0.0
    meta.loc[0, "future_return_5d"] = np.nan
    meta["label_end_date"] = meta.trade_date + pd.Timedelta(days=5)
    meta["complete_features"], meta["label_reason"] = True, "synthetic"
    values = np.arange(540 * 2, dtype="float32").reshape(540, 2)
    old_mask = meta.trade_date.isin(dates[:130]).to_numpy()
    existing = old_mask if reuse else np.zeros(540, dtype=bool)
    prior = meta.loc[old_mask].copy()
    prior["score"] = values[old_mask, 0].astype("float64") + 0.390
    original = tmp_path / OLD / "fits" / "original"
    original.mkdir(parents=True)
    prior.to_parquet(original / "predictions.parquet", index=False)
    raw = b"synthetic model bytes; unpickling is mocked"
    (original / "model.pkl").write_bytes(raw)
    folder = tmp_path / "derived"
    folder.mkdir()
    if not reuse:
        (folder / "model.pkl").write_bytes(raw)
    restored, model = BatchSensitiveModel(), BatchSensitiveModel()
    monkeypatch.setattr(worker.pickle, "load", lambda stream: restored)
    monkeypatch.setattr(worker, "batches", lambda *args: iter([(meta.copy(), values.copy())]))
    config = {"history_root": "unused", "metadata_root": "unused", "features": ["a", "b"]}
    spec = {
        "reuse_slot": "original" if reuse else None,
        "model_sha256": hashlib.sha256(raw).hexdigest(),
        "original_summary": {"prediction_file_sha256": _sha(original / "predictions.parquet")},
        "prediction_sessions": dates.strftime("%Y-%m-%d").tolist(),
        "prediction_rows": 540,
        "prediction_sha256": membership(meta),
    }
    old_hashes = {p.name: _sha(p) for p in original.iterdir()}
    result = worker.predict_union(tmp_path, config, spec, folder, model, None, {})
    frame = pd.read_parquet(folder / "predictions.parquet")
    expected = values[:, 0].astype("float64") + (~existing).sum() / 1000
    if reuse:
        expected[existing] = prior.score.to_numpy()
    np.testing.assert_array_equal(frame.score, expected)
    assert frame.future_return_5d.isna().sum() == 1
    assert result["replay_verification_rows"] == result["prediction_rows"] == 540
    assert result["reused_prediction_rows"] == int(existing.sum())
    assert result["newly_scored_rows"] == int((~existing).sum())
    assert model.batch_sizes == [150 if reuse else 540]
    assert restored.batch_sizes == ([390, 150] if reuse else [540])
    assert old_hashes == {p.name: _sha(p) for p in original.iterdir()}
    if reuse:
        damaged = prior.copy()
        damaged.loc[damaged.index[0], "score"] += 0.25
        damaged.to_parquet(original / "predictions.parquet", index=False)
        spec["original_summary"]["prediction_file_sha256"] = _sha(original / "predictions.parquet")
        other = tmp_path / "corrupt"
        other.mkdir()
        with pytest.raises(DataValidationError, match="exact union replay"):
            worker.predict_union(tmp_path, config, spec, other, model, None, {})
