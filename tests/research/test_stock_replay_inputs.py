from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.stock_replay_inputs import (
    canonical_bar,
    exact_units,
    observation,
    partition,
    prior20_amount,
)

DAYS = tuple(date(2021, 12, 1) + timedelta(days=i) for i in range(20))
BAR = {"open": 10, "low": 9, "high": 11, "close": 10.25, "amount": 1234.56, "volume": 700}


@pytest.mark.parametrize(
    "value,scale,expected",
    [
        (10.25, 100, 1025),
        (700, 1, 700),
        (1234.56, 100, 123456),
        (Decimal("0.01"), 100, 1),
        (0, 100, 0),
        (0, 1, 0),
        (0.001, 100, None),
        (700.5, 1, None),
        (True, 100, None),
        (False, 1, None),
        (None, 100, None),
        (float("nan"), 100, None),
        (float("inf"), 1, None),
        (-1, 100, None),
        ("10.25", 100, None),
        (10**16, 1, None),
        (Decimal("NaN"), 1, None),
    ],
)
def test_exact_canonical_units(value, scale, expected):
    assert exact_units(value, scale) == expected


@pytest.mark.parametrize("scale", [True, 1000, 10000, 100.0])
def test_no_provider_unit_multiplier_can_be_reapplied(scale):
    with pytest.raises(DataValidationError, match="canonical"):
        exact_units(100, scale)


def test_zero_cannot_be_a_price():
    assert exact_units(0, 100, positive=True) is None


def test_execution_bar_conserves_currency_and_shares():
    result = canonical_bar(BAR)
    assert result == {
        "open_fen": 1000,
        "low_fen": 900,
        "high_fen": 1100,
        "close_fen": 1025,
        "amount_fen": 123456,
        "volume_shares": 700,
        "ohlc_valid": True,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"close": 12},
        {"open": 8},
        {"low": 12},
        {"close": None},
        {"close": 10.251},
        {"open": 0},
        {"high": float("nan")},
    ],
)
def test_bad_ohlc_remains_unknown_not_rounded(changes):
    parsed = canonical_bar({**BAR, **changes})
    assert parsed["ohlc_valid"] is False
    assert all(parsed[name] is None for name in ("open_fen", "low_fen", "high_fen", "close_fen"))
    assert parsed["volume_shares"] == 700


def test_adv20_uses_each_explicit_session_and_floors_mean_once():
    values = dict.fromkeys(DAYS, Decimal("10.01"))
    values[DAYS[-1]] = Decimal("10.02")
    assert prior20_amount(values, DAYS, DAYS[-1]) == (1001, ())
    assert values[DAYS[-1]] == Decimal("10.02")


@pytest.mark.parametrize("invalid", [None, float("nan"), -1, 10.001])
def test_adv_unknown_date_is_not_compressed_or_zero_filled(invalid):
    values = dict.fromkeys(DAYS, 10)
    values[DAYS[3]] = invalid
    assert prior20_amount(values, DAYS, DAYS[-1]) == (None, (DAYS[3],))


def test_absent_adv_row_and_execution_liquidity_cannot_fill_gap():
    values = dict.fromkeys(DAYS, 10)
    values.pop(DAYS[2])
    assert prior20_amount(values, DAYS, DAYS[-1]) == (None, (DAYS[2],))
    values[DAYS[-1] + timedelta(days=1)] = 10**8
    with pytest.raises(DataValidationError, match="outside"):
        prior20_amount(values, DAYS, DAYS[-1])


@pytest.mark.parametrize("dates", [DAYS[:-1], DAYS[::-1], list(DAYS), (DAYS[0],) * 20])
def test_prior_window_requires_ordered_unique_full_calendar(dates):
    with pytest.raises(DataValidationError, match="20 explicit"):
        prior20_amount({}, dates, DAYS[-1])


def make_observation(**overrides):
    args = dict(
        code="A",
        execution=date(2021, 12, 21),
        following=date(2021, 12, 22),
        decision=DAYS[-1],
        prior_dates=DAYS,
        amounts=dict.fromkeys(DAYS, 10),
        bar=BAR,
        limits={"up_limit": 12, "down_limit": 8},
        score=0.2,
    )
    return observation(**{**args, **overrides})


def test_numeric_inputs_never_grant_trading_or_corporate_authority():
    row = make_observation()
    assert row["copied_S4_A"] == 0.2
    assert row["session"]["session_volume_shares"] == 700
    assert row["session"]["prior20_amount_fen"] == 1000
    assert row["bar_limit_relation_valid"] is True
    for key in ("market_open", "corporate_actions_processed", "rules", "fees", "participation"):
        assert row["session"][key] is None
    assert row["execution_authority"] is False
    assert row["historical_performance_eligible"] is False


def test_conflicting_limits_are_preserved_and_flagged():
    row = make_observation(limits={"up_limit": 10, "down_limit": 8})
    assert row["bar_limit_relation_valid"] is False
    assert row["session"]["up_limit_fen"] == 1000
    assert row["session"]["raw_close_fen"] == 1025


def test_missing_rows_and_nonfinite_signal_stay_unknown():
    row = make_observation(bar={}, limits={}, score=float("nan"))
    assert row["copied_S4_A"] is None
    assert row["session"]["raw_close_fen"] is None
    assert row["session"]["up_limit_fen"] is None


def test_identity_and_chronology_mismatch_rejected():
    with pytest.raises(DataValidationError, match="another instrument"):
        make_observation(bar={**BAR, "instrument_id": "B"})
    with pytest.raises(DataValidationError, match="chronology"):
        make_observation(execution=DAYS[-1])


def test_partition_duplicate_key_and_date_checks(tmp_path):
    p = tmp_path / "sample.parquet"
    row = {"instrument_id": "A", "trade_date": DAYS[-1], "amount": 10}
    pd.DataFrame([row, row]).to_parquet(p)
    with pytest.raises(DataValidationError, match="duplicate"):
        partition(p, DAYS[-1])
    pd.DataFrame([row]).to_parquet(p)
    with pytest.raises(DataValidationError, match="another date"):
        partition(p, DAYS[-2])


def test_distinct_exception_records_for_same_instrument_are_not_collapsed(tmp_path):
    p = tmp_path / "exceptions.parquet"
    rows = [
        {"instrument_id": "A", "trade_date": DAYS[-1], "source_record_id": key}
        for key in ("event1", "event2")
    ]
    pd.DataFrame(rows).to_parquet(p)
    assert len(partition(p, DAYS[-1], exception_table=True)) == 2
