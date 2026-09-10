from datetime import date

import pandas as pd

from quantlab.research.portfolio_validation import _targets


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
