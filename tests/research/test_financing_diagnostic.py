import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.financing_diagnostic import daily_metrics, financing_features


def inputs():
    days = pd.bdate_range("2020-01-02", periods=15)
    codes = ["A", "B"]
    grid = pd.MultiIndex.from_product([codes, days], names=["instrument_id", "trade_date"])
    frame = grid.to_frame(index=False)
    frame["amount"] = 10.0
    frame["rzye"] = np.r_[np.arange(15), 100 - np.arange(15)].astype(float)
    return frame, days, codes


def calculate(frame, days, codes):
    return financing_features(frame, frame, days, codes, days[6:])


def test_exact_window_delay_weekend_and_no_cross_code():
    f, days, codes = inputs()
    result = calculate(f, days, codes)
    assert len(result) == 18
    assert result.F7.tolist() == pytest.approx([0.1] * 9 + [-0.1] * 9)
    first = result.iloc[0]
    assert first.source_trade_date == days[5]
    assert first.source_rzye == 5 and first.source_rzye_five_sessions_before == 0
    assert first.source_amount5_cny == 50
    monday = result[result.trade_date.dt.dayofweek.eq(0)].iloc[0]
    assert monday.source_trade_date.dayofweek == 4


@pytest.mark.parametrize(
    "field,bad",
    [
        ("rzye", None),
        ("rzye", -1),
        ("rzye", np.inf),
        ("rzye", True),
        ("rzye", "10"),
        ("amount", None),
        ("amount", 0),
        ("amount", -1),
        ("amount", -np.inf),
        ("amount", False),
        ("amount", "10"),
    ],
)
def test_invalid_input_remains_unknown(field, bad):
    f, days, codes = inputs()
    f[field] = f[field].astype(object)
    f.loc[4, field] = bad
    result = calculate(f, days, codes)
    assert pd.isna(result.iloc[0].F7)
    assert result[result.instrument_id.eq("B")].F7.notna().all()
    assert result.iloc[0].balance_window_complete == (field != "rzye")
    assert result.iloc[0].amount_window_complete == (field != "amount")


def test_all_six_balances_required_even_unused_interior_and_gap_not_compressed():
    f, days, codes = inputs()
    result = calculate(f.drop(index=2), days, codes)
    assert len(result) == 18
    assert result.iloc[:3].F7.isna().all()
    assert result.iloc[3].F7 == pytest.approx(0.1)


def test_oldest_balance_does_not_need_same_day_amount():
    f, days, codes = inputs()
    f.loc[0, "amount"] = np.nan
    assert calculate(f, days, codes).iloc[0].F7 == pytest.approx(0.1)


def test_current_and_future_mutation_cannot_affect_current_score():
    f, days, codes = inputs()
    before = calculate(f, days, codes)
    f.loc[f.trade_date.ge(days[8]), ["rzye", "amount"]] = 9e8
    after = calculate(f, days, codes)
    pd.testing.assert_frame_equal(
        before[before.trade_date.le(days[8])], after[after.trade_date.le(days[8])]
    )


@pytest.mark.parametrize("mutation", ["duplicate", "null_code", "other_code", "other_date"])
def test_invalid_keys_rejected(mutation):
    f, days, codes = inputs()
    if mutation == "duplicate":
        f = pd.concat([f, f.iloc[:1]])
    elif mutation == "null_code":
        f.loc[0, "instrument_id"] = None
    elif mutation == "other_code":
        f.loc[0, "instrument_id"] = "C"
    else:
        f.loc[0, "trade_date"] = pd.Timestamp("1990-01-01")
    with pytest.raises(DataValidationError):
        calculate(f, days, codes)


@pytest.mark.parametrize(
    "mutation", ["reversed", "duplicate", "too_short", "missing_warmup", "codes"]
)
def test_invalid_calendar(mutation):
    f, days, codes = inputs()
    decisions = days[6:]
    if mutation == "reversed":
        days = days[::-1]
    elif mutation == "duplicate":
        days = days.insert(1, days[0])
    elif mutation == "too_short":
        days = days[:5]
    elif mutation == "missing_warmup":
        decisions = days[5:]
    else:
        codes = codes[::-1]
    with pytest.raises(DataValidationError):
        financing_features(f, f, days, codes, decisions)


def metric_frame(n=32):
    ranks = np.arange(n, dtype=float) // 2
    return pd.DataFrame(
        {
            "trade_date": pd.Timestamp("2020-02-03"),
            "F7": ranks,
            "momentum20": -ranks,
            "label_5": ranks,
            "circ_mv": ranks + 1,
            "turnover_rate": ranks,
            "balance_window_complete": True,
            "amount_window_complete": True,
        }
    )


def test_pairwise_comparator_and_ties():
    f = metric_frame()
    f.loc[0, "F7"] = np.nan
    f.loc[1, "momentum20"] = np.nan
    m = daily_metrics(f).iloc[0]
    assert m.pairs == 30 and m.feature_valid == 31
    assert m.rank_ic == pytest.approx(1) and m.reference_rank_ic == pytest.approx(1)
    assert m.paired_difference == pytest.approx(0)


@pytest.mark.parametrize(
    "problem", ["less30", "constant_score", "constant_reference", "constant_label"]
)
def test_both_comparator_ics_unknown_together(problem):
    f = metric_frame()
    if problem == "less30":
        f.loc[:2, "momentum20"] = np.inf
    else:
        f[
            {
                "constant_score": "F7",
                "constant_reference": "momentum20",
                "constant_label": "label_5",
            }[problem]
        ] = 0.0
    m = daily_metrics(f).iloc[0]
    assert pd.isna(m.rank_ic) and pd.isna(m.reference_rank_ic) and pd.isna(m.paired_difference)


def test_alternate_proof_arithmetic_ties_and_calendar_bootstrap():
    from decimal import Decimal
    from fractions import Fraction

    from quantlab.research.funding_pilot import block_interval

    proof = runpy.run_path(str(Path(__file__).parents[2] / "scripts/prove_f7_financing.py"))
    assert proof["number"](Decimal("0.15")) == Fraction(3, 20)
    assert proof["number"](True) is None
    assert proof["number"](0, True) is None
    assert proof["ranks"]([4, 1, 4, 2]) == [3.5, 1.0, 3.5, 2.0]
    assert proof["correlation"](list(range(30)), list(range(29, -1, -1))) == -1
    assert proof["correlation"]([1] * 30, list(range(30))) is None
    values = np.sin(np.arange(100)).tolist()
    values[40:50] = [np.nan] * 10
    assert proof["interval"](values) == pytest.approx(block_interval(values))
