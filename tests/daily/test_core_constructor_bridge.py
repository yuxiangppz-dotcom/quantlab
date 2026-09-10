from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quantlab.daily.service import _apply_portfolio_contract
from quantlab.data.models import DataValidationError

DAY = date(2026, 9, 9)


def _config(**updates: object) -> dict:
    config = {
        "target_count": 2,
        "score_direction": "higher_is_better",
        "gross_exposure": 1.0,
        "max_weight_per_name": 0.4,
        "tie_policy": "alpha_score_then_instrument_id",
    }
    config.update(updates)
    return config


def _frame(scores: list[tuple[str, float | None]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": instrument_id, "trade_date": DAY, "alpha_score": score}
            for instrument_id, score in scores
        ]
    )


def test_bridge_uses_core_tie_break_and_residual_cash() -> None:
    ranking = _frame(
        [
            ("000003.SZ", 1.0),
            ("000001.SZ", 1.0),
            ("000002.SZ", 1.0),
        ]
    )

    materialized, selected_count, per_name, cash_weight = _apply_portfolio_contract(
        ranking,
        DAY,
        _config(),
    )

    selected = materialized.loc[materialized["selected"]]
    assert set(selected["instrument_id"]) == {"000001.SZ", "000002.SZ"}
    assert selected_count == 2
    assert set(selected["target_weight"]) == {pytest.approx(0.4)}
    assert per_name == pytest.approx(0.4)
    assert cash_weight == pytest.approx(0.2)


def test_bridge_all_nan_scores_stays_fully_in_cash() -> None:
    ranking = _frame([("000001.SZ", None), ("600000.SH", None)])

    materialized, selected_count, per_name, cash_weight = _apply_portfolio_contract(
        ranking,
        DAY,
        _config(),
    )

    assert not materialized["selected"].any()
    assert materialized["target_weight"].eq(0.0).all()
    assert selected_count == 0
    assert per_name == pytest.approx(0.0)
    assert cash_weight == pytest.approx(1.0)


def test_bridge_fails_closed_on_noncanonical_daily_tie_policy() -> None:
    ranking = _frame([("000001.SZ", 1.0)])

    with pytest.raises(DataValidationError, match="tie_policy"):
        _apply_portfolio_contract(
            ranking,
            DAY,
            _config(tie_policy="keep_all_cutoff_ties"),
        )
