from __future__ import annotations

import os

import pandas as pd
import pytest

from quantlab.research.qlib_adapter import qlib_integration_status, to_qlib_static_loader

pytestmark = pytest.mark.skipif(
    os.environ.get("QUANTLAB_TEST_OPTIONAL_RESEARCH") != "1",
    reason="optional research runtimes are exercised by the dedicated CI job",
)


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
