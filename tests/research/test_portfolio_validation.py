from datetime import date

import pandas as pd

import pytest

from quantlab.research.portfolio_validation import _require_comparable, _targets


def test_candidate_targets_use_higher_score_and_stable_ties() -> None:
    day = date(2026, 9, 9)
    frame = pd.DataFrame(
        [
            {"instrument_id": "000003.SZ", "trade_date": day, "factor": 3.0},
            {"instrument_id": "000002.SZ", "trade_date": day, "factor": 2.0},
            {"instrument_id": "000001.SZ", "trade_date": day, "factor": 3.0},
            {"instrument_id": "000004.SZ", "trade_date": day, "factor": 1.0},
            {"instrument_id": "000005.SZ", "trade_date": day, "factor": 0.0},
        ]
    )
    portfolio = _targets(frame.sample(frac=1, random_state=7), "factor", [day])[day]
    # 20% selects one rank bucket; equal cutoff scores stay together.
    assert [item.instrument_id for item in portfolio.positions] == [
        "000001.SZ",
        "000003.SZ",
    ]
    assert sum(item.target_weight for item in portfolio.positions) == 1.0


def test_comparison_fails_closed_on_blocked_control_or_partial_alignment() -> None:
    with pytest.raises(RuntimeError, match="run status"):
        _require_comparable("blocked_by_unsupported_event", 10, 10, "control")
    with pytest.raises(RuntimeError, match="coverage mismatch"):
        _require_comparable("completed", 9, 10, "candidate")
