from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.portfolio import (
    FixedCountPortfolioConfig,
    construct_fixed_count_portfolio,
)


DAY = date(2026, 1, 5)


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": instrument_id, "trade_date": DAY, "alpha_score": score}
            for instrument_id, score in rows
        ]
    )


def test_fixed_count_is_exact_and_breaks_score_ties_by_instrument_id() -> None:
    frame = _frame(
        [
            ("000003.SZ", 3.0),
            ("000002.SZ", 2.0),
            ("000001.SZ", 3.0),
            ("000004.SZ", 1.0),
        ]
    )
    config = FixedCountPortfolioConfig(target_count=1, score_direction="higher_is_better")

    portfolio = construct_fixed_count_portfolio(frame, DAY, config)

    assert [item.instrument_id for item in portfolio.positions] == ["000001.SZ"]
    assert portfolio.positions[0].target_weight == pytest.approx(1.0)
    assert portfolio.cash_weight == pytest.approx(0.0)


def test_fixed_count_lower_is_better_and_row_order_independent() -> None:
    frame = _frame(
        [
            ("000004.SZ", 4.0),
            ("000002.SZ", 2.0),
            ("000003.SZ", 3.0),
            ("000001.SZ", 1.0),
        ]
    )
    config = FixedCountPortfolioConfig(target_count=2, score_direction="lower_is_better")

    first = construct_fixed_count_portfolio(frame, DAY, config)
    second = construct_fixed_count_portfolio(
        frame.sample(frac=1, random_state=7).reset_index(drop=True), DAY, config
    )

    assert first == second
    assert [item.instrument_id for item in first.positions] == ["000001.SZ", "000002.SZ"]


def test_fixed_count_cap_leaves_residual_cash() -> None:
    frame = _frame([(f"s{index}", float(index)) for index in range(5)])
    config = FixedCountPortfolioConfig(
        target_count=3,
        score_direction="higher_is_better",
        gross_exposure=1.0,
        max_weight_per_name=0.2,
    )

    portfolio = construct_fixed_count_portfolio(frame, DAY, config)

    assert len(portfolio.positions) == 3
    assert all(item.target_weight == pytest.approx(0.2) for item in portfolio.positions)
    assert portfolio.cash_weight == pytest.approx(0.4)


def test_fixed_count_uses_all_valid_names_when_universe_is_smaller_than_target() -> None:
    frame = _frame([("a", 1.0), ("b", 2.0)])
    config = FixedCountPortfolioConfig(target_count=20, score_direction="higher_is_better")

    portfolio = construct_fixed_count_portfolio(frame, DAY, config)

    assert len(portfolio.positions) == 2
    assert portfolio.cash_weight == pytest.approx(0.0)


def test_fixed_count_excludes_nan_and_ignores_future_columns() -> None:
    frame = _frame([("a", 1.0), ("b", np.nan), ("c", 3.0)])
    frame["future_return_5d"] = [999.0, -999.0, 0.0]
    config = FixedCountPortfolioConfig(target_count=2, score_direction="higher_is_better")

    portfolio = construct_fixed_count_portfolio(frame, DAY, config)

    assert [item.instrument_id for item in portfolio.positions] == ["a", "c"]


@pytest.mark.parametrize("target_count", [0, -1, True, 1.5])
def test_fixed_count_rejects_invalid_target_count(target_count: object) -> None:
    with pytest.raises(ValueError):
        FixedCountPortfolioConfig(
            target_count=target_count,  # type: ignore[arg-type]
            score_direction="higher_is_better",
        )


def test_fixed_count_rejects_invalid_exposure_and_cap() -> None:
    with pytest.raises(ValueError):
        FixedCountPortfolioConfig(
            target_count=20,
            score_direction="higher_is_better",
            gross_exposure=float("nan"),
        )
    with pytest.raises(ValueError):
        FixedCountPortfolioConfig(
            target_count=20,
            score_direction="higher_is_better",
            max_weight_per_name=0.0,
        )
