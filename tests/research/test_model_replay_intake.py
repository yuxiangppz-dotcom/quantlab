from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.model_replay_intake import bound_scores, nav_metrics, rank_targets


def test_targets_ignore_future_labels_and_have_next_session_execution():
    cal = pd.date_range("2024-01-01", periods=3)
    frame = pd.DataFrame({"instrument_id": [f"{i:06d}.SZ" for i in range(22)],
                          "trade_date": cal[0], "score": 1.0,
                          "future_return_5d": np.nan})
    first = rank_targets(frame, cal)
    frame["future_return_5d"] = np.arange(22)[::-1]
    second = rank_targets(frame, cal)
    pd.testing.assert_frame_equal(first, second)
    assert len(first) == 20
    assert first.instrument_id.iloc[0] == "000000.SZ"
    assert first.intended_execution_date.eq(cal[1]).all()
    assert first.target_weight.sum() == pytest.approx(1)


def test_short_candidate_list_keeps_cash_and_does_not_bridge_calendar():
    cal = pd.DatetimeIndex(["2024-01-05", "2024-01-08"])
    frame = pd.DataFrame({"instrument_id": ["A", "B"], "trade_date": cal[0],
                          "score": [1.0, np.nan]})
    result = rank_targets(frame, cal)
    assert len(result) == 1
    assert result.cash_weight.iloc[0] == pytest.approx(.95)
    assert result.intended_execution_date.iloc[0] == cal[1]


def test_duplicate_scores_reject():
    cal = pd.date_range("2024-01-01", periods=2)
    frame = pd.DataFrame({"instrument_id": ["A", "A"], "trade_date": cal[0], "score": 1})
    with pytest.raises(DataValidationError, match="duplicate"):
        rank_targets(frame, cal)


def test_bound_scores_checks_actual_file_and_ignores_label(tmp_path):
    path = tmp_path / "scores.parquet"
    pd.DataFrame({"instrument_id": ["A"], "trade_date": [pd.Timestamp("2024-01-01")],
                  "score": [1], "future_return_5d": [999]}).to_parquet(path)
    raw = path.read_bytes()
    receipt = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert list(bound_scores(path, receipt)) == ["instrument_id", "trade_date", "score"]
    with pytest.raises(DataValidationError, match="receipt"):
        bound_scores(path, {**receipt, "bytes": 0})


def test_metrics_hand_calculation():
    nav = pd.Series([100., 110., 99., 108.9], index=pd.date_range("2024-01-01", periods=4))
    result = nav_metrics(nav)
    assert result["total_return"] == pytest.approx(.089)
    assert result["max_drawdown"] == pytest.approx(-.1)
    returns = np.array([.1, -.1, .1])
    assert result["sharpe_rf0_252"] == pytest.approx(
        returns.mean() / returns.std(ddof=1) * np.sqrt(252)
    )
    with pytest.raises(DataValidationError, match="missing"):
        nav_metrics(pd.Series([1, np.nan], index=nav.index[:2]))
