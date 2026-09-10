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

    materialized, valid_count, selected_count, per_name, cash_weight = (
        _apply_portfolio_contract(ranking, DAY, _config())
    )

    selected = materialized.loc[materialized["selected"]]
    assert list(materialized["instrument_id"]) == ["000001.SZ", "000002.SZ", "000003.SZ"]
    assert list(materialized["rank"].astype(int)) == [1, 2, 3]
    assert set(selected["instrument_id"]) == {"000001.SZ", "000002.SZ"}
    assert valid_count == 3
    assert selected_count == 2
    assert selected["target_weight"].tolist() == pytest.approx([0.4, 0.4])
    assert per_name == pytest.approx(0.4)
    assert cash_weight == pytest.approx(0.2)


def test_bridge_all_nan_scores_stays_fully_in_cash() -> None:
    ranking = _frame([("000001.SZ", None), ("600000.SH", None)])

    materialized, valid_count, selected_count, per_name, cash_weight = (
        _apply_portfolio_contract(ranking, DAY, _config())
    )

    assert materialized["rank"].isna().all()
    assert not materialized["selected"].any()
    assert materialized["target_weight"].eq(0.0).all()
    assert valid_count == 0
    assert selected_count == 0
    assert per_name == pytest.approx(0.0)
    assert cash_weight == pytest.approx(1.0)


def test_bridge_is_invariant_to_input_row_order() -> None:
    first = _frame(
        [
            ("600001.SH", 3.0),
            ("000001.SZ", 2.0),
            ("300001.SZ", 1.0),
        ]
    )
    second = first.iloc[::-1].reset_index(drop=True)

    left = _apply_portfolio_contract(first, DAY, _config())
    right = _apply_portfolio_contract(second, DAY, _config())

    pd.testing.assert_frame_equal(left[0], right[0])
    assert left[1:] == pytest.approx(right[1:])


def test_bridge_respects_lower_is_better_direction() -> None:
    ranking = _frame(
        [
            ("000001.SZ", 3.0),
            ("000002.SZ", 1.0),
            ("000003.SZ", 2.0),
        ]
    )

    materialized, _, _, _, _ = _apply_portfolio_contract(
        ranking,
        DAY,
        _config(score_direction="lower_is_better"),
    )

    assert list(materialized["instrument_id"]) == ["000002.SZ", "000003.SZ", "000001.SZ"]
    assert set(materialized.loc[materialized["selected"], "instrument_id"]) == {
        "000002.SZ",
        "000003.SZ",
    }


def test_bridge_fails_closed_on_noncanonical_daily_tie_policy() -> None:
    ranking = _frame([("000001.SZ", 1.0)])

    with pytest.raises(DataValidationError, match="tie_policy"):
        _apply_portfolio_contract(
            ranking,
            DAY,
            _config(tie_policy="keep_all_cutoff_ties"),
        )
