from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.liquidity_amount import adv_floor, amount_floor
from quantlab.research.stock_replay_inputs import exact_units

DAYS = tuple(date(2021, 12, 1) + timedelta(days=i) for i in range(20))


@pytest.mark.parametrize(
    "value,expected,changed",
    [
        (0, 0, False),
        (12.34, 1234, False),
        (Decimal("0.019"), 1, True),
        (131464165.00000001, 13146416500, True),
        (1061931554.9999999, 106193155499, True),
        (1e-100, 0, True),
        (10**13, 10**15, False),
        (Decimal("0.000001"), 0, True),
    ],
)
def test_fen_conversion_never_increases_observed_amount(value, expected, changed):
    assert amount_floor(value) == (expected, changed)
    difference = Decimal(str(value)) * 100 - expected
    assert 0 <= difference < 1


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        "1.23",
        -0.001,
        float("nan"),
        float("inf"),
        Decimal("Infinity"),
        Decimal("1e999999"),
        10**14,
        [],
        {},
    ],
)
def test_invalid_or_unbounded_amount_does_not_become_zero_liquidity(value):
    assert amount_floor(value) == (None, False)


def test_existing_exact_cash_and_price_conversion_is_unchanged():
    amount = 131464165.00000001
    assert exact_units(amount, 100) is None
    assert amount_floor(amount)[0] == 13146416500
    assert exact_units(10.255, 100, positive=True) is None


@pytest.mark.parametrize("value", [Decimal("0.00" + "9" * 100), Decimal("1e-9999999")])
def test_decimal_context_cannot_round_up_a_sub_fen_observation(value):
    assert amount_floor(value) == (0, True)


def test_complete_window_floors_daily_capacity_then_mean():
    rows = {d: Decimal("1.009") for d in DAYS}
    r = adv_floor(rows, DAYS, DAYS[-1])
    assert r["adv20_floor_fen"] == 100
    assert len(r["floored_dates"]) == 20 and r["unknown_dates"] == []
    assert all(x == 100 for x in r["daily_floor_fen"].values())


def test_missing_days_cannot_be_filled_or_compressed():
    rows = {d: 100 for d in DAYS[1:]}
    r = adv_floor(rows, DAYS, DAYS[-1])
    assert r["adv20_floor_fen"] is None
    assert r["unknown_dates"] == [DAYS[0].isoformat()]
    assert len(r["daily_floor_fen"]) == 20


@pytest.mark.parametrize("defect", ["future", "list", "short", "duplicate", "unsorted", "decision"])
def test_calendar_scope_is_exact(defect):
    days, rows, decision = DAYS, {d: 100 for d in DAYS}, DAYS[-1]
    if defect == "future":
        rows[date(2022, 1, 1)] = 100
    elif defect == "list":
        days = list(days)
    elif defect == "short":
        days = days[1:]
    elif defect == "duplicate":
        days = days[:-1] + (days[-2],)
    elif defect == "unsorted":
        days = days[::-1]
    else:
        decision = date(2022, 1, 1)
    with pytest.raises(DataValidationError):
        adv_floor(rows, days, decision)
