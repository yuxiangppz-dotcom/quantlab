from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.portfolio import (
    RankPortfolioConfig,
    TargetPortfolio,
    TargetWeight,
    construct_rank_portfolio,
    portfolio_to_frame,
)


def _frame(scores, extra=None) -> pd.DataFrame:
    rows = []
    for i, s in enumerate(scores):
        rows.append({
            "instrument_id": f"s{i}",
            "trade_date": date(2026, 1, 5),
            "alpha_score": s,
        })
    frame = pd.DataFrame(rows)
    if extra:
        for col, values in extra.items():
            frame[col] = values
    return frame


def _cfg(**kwargs):
    defaults = dict(selection_fraction=0.4, score_direction="higher_is_better")
    defaults.update(kwargs)
    return RankPortfolioConfig(**defaults)


def test_higher_is_better() -> None:
    pf = construct_rank_portfolio(_frame([1.0, 2.0, 3.0, 4.0, 5.0]), date(2026, 1, 5), _cfg())
    assert {p.instrument_id for p in pf.positions} == {"s3", "s4"}


def test_lower_is_better() -> None:
    pf = construct_rank_portfolio(
        _frame([1.0, 2.0, 3.0, 4.0, 5.0]),
        date(2026, 1, 5),
        _cfg(score_direction="lower_is_better"),
    )
    assert {p.instrument_id for p in pf.positions} == {"s0", "s1"}


def test_selection_fraction() -> None:
    pf = construct_rank_portfolio(
        _frame(list(range(1, 11))), date(2026, 1, 5), _cfg(selection_fraction=0.3)
    )
    assert len(pf.positions) == 3


def test_equal_weight_and_exposure() -> None:
    pf = construct_rank_portfolio(
        _frame(list(range(1, 11))), date(2026, 1, 5), _cfg(selection_fraction=0.5)
    )
    assert len(pf.positions) == 5
    assert all(abs(p.target_weight - 0.2) < 1e-9 for p in pf.positions)
    assert abs(sum(p.target_weight for p in pf.positions) + pf.cash_weight - 1.0) < 1e-9


def test_max_weight_cap_and_leftover_cash() -> None:
    pf = construct_rank_portfolio(
        _frame(list(range(1, 11))),
        date(2026, 1, 5),
        _cfg(selection_fraction=0.5, max_weight_per_name=0.15),
    )
    assert all(p.target_weight == 0.15 for p in pf.positions)
    assert abs(pf.cash_weight - 0.25) < 1e-9


def test_nan_alpha_excluded() -> None:
    pf = construct_rank_portfolio(
        _frame([1.0, 2.0, np.nan, 4.0, 5.0]), date(2026, 1, 5), _cfg(selection_fraction=0.5)
    )
    assert {p.instrument_id for p in pf.positions} == {"s3", "s4"}


def test_all_nan_all_cash() -> None:
    pf = construct_rank_portfolio(
        _frame([np.nan, np.nan]), date(2026, 1, 5), _cfg(selection_fraction=0.5)
    )
    assert pf.positions == ()
    assert pf.cash_weight == 1.0


def test_duplicate_instrument_raises() -> None:
    frame = pd.DataFrame({
        "instrument_id": ["a", "a"],
        "trade_date": [date(2026, 1, 5), date(2026, 1, 5)],
        "alpha_score": [1.0, 2.0],
    })
    with pytest.raises(DataValidationError):
        construct_rank_portfolio(frame, date(2026, 1, 5), _cfg())


def test_multiple_trade_date_raises() -> None:
    frame = pd.DataFrame({
        "instrument_id": ["a", "b"],
        "trade_date": [date(2026, 1, 5), date(2026, 1, 6)],
        "alpha_score": [1.0, 2.0],
    })
    with pytest.raises(DataValidationError):
        construct_rank_portfolio(frame, date(2026, 1, 5), _cfg())


def test_ties_not_split() -> None:
    pf = construct_rank_portfolio(
        _frame([1.0, 2.0, 2.0, 2.0, 3.0]), date(2026, 1, 5), _cfg(selection_fraction=0.4)
    )
    # k = 2, cutoff = 2.0 -> all 2.0 ties plus 3.0 selected (4 names)
    assert {p.instrument_id for p in pf.positions} == {"s1", "s2", "s3", "s4"}


def test_row_order_independent() -> None:
    frame = _frame([1.0, 2.0, 3.0, 4.0, 5.0])
    shuffled = frame.sample(frac=1, random_state=0).reset_index(drop=True)
    cfg = _cfg()
    pf1 = construct_rank_portfolio(frame, date(2026, 1, 5), cfg)
    pf2 = construct_rank_portfolio(shuffled, date(2026, 1, 5), cfg)
    assert pf1 == pf2


def test_future_return_ignored() -> None:
    frame1 = _frame([1.0, 2.0, 3.0], extra={"future_return_5d": [0.1, 0.2, 0.3]})
    frame2 = _frame([1.0, 2.0, 3.0], extra={"future_return_5d": [999.0, -999.0, 0.0]})
    cfg = _cfg(selection_fraction=1.0)
    assert construct_rank_portfolio(frame1, date(2026, 1, 5), cfg) == construct_rank_portfolio(
        frame2, date(2026, 1, 5), cfg
    )


def test_invalid_config() -> None:
    with pytest.raises(ValueError):
        RankPortfolioConfig(selection_fraction=0.0, score_direction="higher_is_better")
    with pytest.raises(ValueError):
        RankPortfolioConfig(selection_fraction=1.5, score_direction="higher_is_better")
    with pytest.raises(ValueError):
        RankPortfolioConfig(selection_fraction=0.5, score_direction="sideways")
    with pytest.raises(ValueError):
        RankPortfolioConfig(
            selection_fraction=0.5,
            score_direction="higher_is_better",
            max_weight_per_name=-1.0,
        )


def test_positions_sorted() -> None:
    pf = construct_rank_portfolio(
        _frame([5.0, 1.0, 3.0, 4.0, 2.0]), date(2026, 1, 5), _cfg(selection_fraction=1.0)
    )
    ids = [p.instrument_id for p in pf.positions]
    assert ids == sorted(ids)


def test_target_portfolio_duplicate_position_raises() -> None:
    with pytest.raises(DataValidationError):
        TargetPortfolio(
            as_of=date(2026, 1, 5),
            positions=(TargetWeight("a", 0.5), TargetWeight("a", 0.5)),
            cash_weight=0.0,
        )


def test_target_portfolio_weight_sum_mismatch_raises() -> None:
    with pytest.raises(DataValidationError):
        TargetPortfolio(
            as_of=date(2026, 1, 5),
            positions=(TargetWeight("a", 0.5),),
            cash_weight=0.6,  # 0.5 + 0.6 = 1.1 != 1.0
        )


def test_portfolio_to_frame() -> None:
    pf = TargetPortfolio(
        as_of=date(2026, 1, 5),
        positions=(TargetWeight("b", 0.4), TargetWeight("a", 0.6)),
        cash_weight=0.0,
    )
    df = portfolio_to_frame(pf)
    assert list(df.columns) == ["as_of", "instrument_id", "target_weight"]
    assert len(df) == 2
    assert "cash" not in df.columns


def test_signed_target_weight_allowed() -> None:
    pf = TargetPortfolio(
        as_of=date(2026, 1, 5),
        positions=(TargetWeight("a", 1.3), TargetWeight("b", -0.3)),
        cash_weight=0.0,
    )
    assert pf.net_exposure == pytest.approx(1.0)
    assert pf.gross_exposure == pytest.approx(1.6)


def test_exposure_properties_long_only() -> None:
    pf = TargetPortfolio(
        as_of=date(2026, 1, 5),
        positions=(TargetWeight("a", 0.6), TargetWeight("b", 0.4)),
        cash_weight=0.0,
    )
    assert pf.net_exposure == pytest.approx(1.0)
    assert pf.gross_exposure == pytest.approx(1.0)


def test_nav_balance_is_one() -> None:
    pf = TargetPortfolio(
        as_of=date(2026, 1, 5),
        positions=(TargetWeight("a", 0.6),),
        cash_weight=0.4,
    )
    assert sum(p.target_weight for p in pf.positions) + pf.cash_weight == pytest.approx(1.0)


def test_constructor_gross_08_cash_02() -> None:
    pf = construct_rank_portfolio(
        _frame(list(range(1, 6))),
        date(2026, 1, 5),
        _cfg(selection_fraction=1.0, gross_exposure=0.8),
    )
    assert all(abs(p.target_weight - 0.16) < 1e-9 for p in pf.positions)
    assert pf.cash_weight == pytest.approx(0.2)


def test_all_nan_cash_one() -> None:
    pf = construct_rank_portfolio(_frame([np.nan]), date(2026, 1, 5), _cfg())
    assert pf.positions == ()
    assert pf.cash_weight == pytest.approx(1.0)


def test_config_nan_inf_rejected() -> None:
    with pytest.raises(ValueError):
        RankPortfolioConfig(selection_fraction=float("nan"), score_direction="higher_is_better")
    with pytest.raises(ValueError):
        RankPortfolioConfig(
            selection_fraction=0.5,
            score_direction="higher_is_better",
            gross_exposure=float("inf"),
        )
    with pytest.raises(ValueError):
        RankPortfolioConfig(
            selection_fraction=0.5,
            score_direction="higher_is_better",
            max_weight_per_name=float("nan"),
        )
