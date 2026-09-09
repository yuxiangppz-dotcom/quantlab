from __future__ import annotations

import importlib.util

import pandas as pd
import pytest

from quantlab.research.factor_registry import (
    FACTOR_REGISTRY,
    add_transparent_combination,
    build_factor_columns,
)
from quantlab.research.qlib_adapter import qlib_integration_status, to_qlib_static_loader


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "instrument_id": ["000001.SZ", "600000.SH", "000002.SZ"],
            "trade_date": ["2026-09-09"] * 3,
            "return_1d": [0.01, -0.02, 0.0],
            "return_5d": [0.03, -0.04, 0.01],
            "return_20d": [0.1, -0.2, 0.0],
            "open": [10.0, 20.0, 30.0],
            "high": [11.0, 22.0, 31.0],
            "low": [9.0, 19.0, 29.0],
            "close": [10.5, 19.0, 30.5],
            "amount": [1000.0, 2000.0, 3000.0],
            "turnover_rate": [0.01, 0.02, 0.03],
            "circ_mv": [100.0, 200.0, 300.0],
            "total_mv": [200.0, 400.0, 600.0],
            "future_return_5d": [999.0, -999.0, 123.0],
        }
    )


def test_registry_is_bounded_and_features_do_not_use_future_label() -> None:
    assert len(FACTOR_REGISTRY) == 10
    original = _frame()
    altered = original.copy()
    altered["future_return_5d"] *= -100
    left = build_factor_columns(original)
    right = build_factor_columns(altered)
    columns = [item.factor_id for item in FACTOR_REGISTRY]
    pd.testing.assert_frame_equal(left[columns], right[columns])
    assert left.loc[1, "reversal_20d"] == pytest.approx(0.2)
    assert left.loc[0, "intraday_strength"] == pytest.approx(0.05)


def test_transparent_combination_is_deterministic_under_row_reordering() -> None:
    features = build_factor_columns(_frame())
    columns = ["reversal_20d", "low_amplitude", "small_size", "intraday_strength"]
    left = add_transparent_combination(features, columns).set_index("instrument_id")
    right = add_transparent_combination(
        features.sample(frac=1, random_state=7), columns
    ).set_index("instrument_id")
    pd.testing.assert_series_equal(
        left["transparent_combo_v1"].sort_index(),
        right["transparent_combo_v1"].sort_index(),
    )


def test_qlib_status_never_misrepresents_fallback_as_qlib() -> None:
    status = qlib_integration_status()
    installed = importlib.util.find_spec("qlib") is not None
    assert status["qlib_available"] is installed
    if not installed:
        with pytest.raises(RuntimeError, match="Qlib is not installed"):
            to_qlib_static_loader(_frame(), ["return_1d"])
