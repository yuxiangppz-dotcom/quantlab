from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.s4_future_window import LABEL_DATES, evaluate_s4_price_window

CODES = tuple(f"{i:06}.SZ" for i in range(1, 257))


def inputs(known=32):
    scores = pd.DataFrame(
        {
            "instrument_id": CODES,
            "price_as_of": "2026-09-11",
            "S4-A": [float(i) if i < known else np.nan for i in range(256)],
            "reference_reversal20": [-float(i) if i < known else np.nan for i in range(256)],
            "S4_A_known": [i < known for i in range(256)],
            "reference20_known": [i < known for i in range(256)],
        }
    )
    future = pd.DataFrame(
        [
            {
                "instrument_id": code,
                "trade_date": pd.Timestamp(day),
                "open": 10.0,
                "high": 100,
                "low": 1,
                "close": 10 + i * 0.1 if j == 5 else 10.0,
                "volume": 1000.0,
                "amount": 10000.0,
                "adj_factor": 2.0,
            }
            for i, code in enumerate(CODES)
            for j, day in enumerate(LABEL_DATES)
        ]
    )
    times = dict(
        created_at=datetime(2026, 9, 13, 1, 27, 31, tzinfo=UTC),
        signal_inputs_available_at=datetime(2026, 9, 13, 1, 27, 30, tzinfo=UTC),
        future_sources_available_at=tuple(
            (d, datetime(2026, 9, d.day, 8, tzinfo=UTC)) for d in LABEL_DATES
        ),
        evaluated_at=datetime(2026, 9, 21, 8, 1, tzinfo=UTC),
    )
    return scores, future, times


def evaluate(scores, future, times, codes=CODES):
    return evaluate_s4_price_window(scores, future, codes, **times)


def test_unchanged_five_session_price_window_and_separate_false_authority():
    scores, future, times = inputs()
    before_scores, before_future = scores.copy(deep=True), future.copy(deep=True)
    r = evaluate(scores, future, times)
    assert len(r.rows) == r.labels_known == 256 and r.common_pairs == 32
    assert r.s4_rank_ic == pytest.approx(1) and r.reference_rank_ic == pytest.approx(-1)
    assert r.paired_ic_difference == pytest.approx(2)
    assert r.rows[31].adjusted_price_change == pytest.approx(0.31)
    assert r.status == "single_window_price_association"
    assert not r.source_files_certified and not r.net_return_evidence and not r.execution_authority
    assert not r.stability_or_significance_established and not r.promotion_authority
    with pytest.raises(FrozenInstanceError):
        r.execution_authority = True
    pd.testing.assert_frame_equal(scores, before_scores)
    pd.testing.assert_frame_equal(future, before_future)


def test_average_ties_have_independent_hand_computable_rank_difference():
    scores, future, times = inputs()
    for i in range(32):
        group = i // 8
        scores.loc[i, "S4-A"] = [1, 2, 3, 4][group]
        scores.loc[i, "reference_reversal20"] = [1, 3, 2, 4][group]
        future.loc[i * 6 + 5, "close"] = 10 + group
    r = evaluate(scores, future, times)
    # Four equally sized tie groups: swapping the middle ranks gives rho=0.8.
    assert r.s4_rank_ic == pytest.approx(1)
    assert r.reference_rank_ic == pytest.approx(0.8)
    assert r.paired_ic_difference == pytest.approx(0.2)


def test_score_and_future_input_order_do_not_change_instrument_alignment():
    scores, future, times = inputs()
    expected = evaluate(scores, future, times)
    assert evaluate(scores.iloc[::-1], future.iloc[::-1], times) == expected


def test_nullable_unknown_price_asof_does_not_pass_an_all_skipping_nulls():
    scores, future, times = inputs()
    scores["price_as_of"] = scores.price_as_of.astype("string")
    scores.loc[0, "price_as_of"] = pd.NA
    with pytest.raises(DataValidationError, match="price-as-of"):
        evaluate(scores, future, times)


def test_close_boundary_is_admitted_only_when_all_source_times_already_available():
    scores, future, times = inputs()
    times["evaluated_at"] = datetime(2026, 9, 21, 7, tzinfo=UTC)
    times["future_sources_available_at"] = tuple(
        (day, datetime(2026, 9, day.day, 7, tzinfo=UTC)) for day in LABEL_DATES
    )
    assert evaluate(scores, future, times).common_pairs == 32


def test_both_metrics_use_the_same_joint_rows_even_if_individual_samples_differ():
    scores, future, times = inputs(60)
    scores.loc[:29, "S4-A"] = np.arange(1000, 970, -1)
    scores.loc[:29, "reference_reversal20"] = np.nan
    scores.loc[:29, "reference20_known"] = False
    r = evaluate(scores, future, times)
    assert r.common_pairs == 30
    assert r.s4_rank_ic == pytest.approx(1) and r.reference_rank_ic == pytest.approx(-1)
    assert all(not x.common_pair for x in r.rows[:30])


@pytest.mark.parametrize("known", [0, 1, 29, 30])
def test_minimum_common_pair_boundary_cannot_be_lowered(known):
    scores, future, times = inputs(known)
    r = evaluate(scores, future, times)
    assert r.common_pairs == known and len(r.rows) == 256
    if known < 30:
        assert r.status == "insufficient_common_pairs"
        assert r.s4_rank_ic is r.reference_rank_ic is r.paired_ic_difference is None
    else:
        assert r.s4_rank_ic == pytest.approx(1)


@pytest.mark.parametrize("which", ["signal", "reference", "label"])
def test_constant_ranks_do_not_become_zero_or_trigger_sample_fallback(which):
    scores, future, times = inputs()
    if which == "label":
        future["close"] = 10.0
    else:
        scores.loc[:31, "S4-A" if which == "signal" else "reference_reversal20"] = 1.0
    r = evaluate(scores, future, times)
    assert r.status == "constant_common_ranks" and r.common_pairs == 32
    assert r.paired_ic_difference is None
    if which in ("signal", "label"):
        assert r.s4_rank_ic is None
    if which in ("reference", "label"):
        assert r.reference_rank_ic is None


@pytest.mark.parametrize("index", list(range(6)))
def test_every_intermediate_bar_is_required_and_missing_stock_is_retained(index):
    scores, future, times = inputs()
    future = future.drop(index=index)
    r = evaluate(scores, future, times)
    assert r.rows[0].valid_future_bars == 5 and r.rows[0].adjusted_price_change is None
    assert not r.rows[0].common_pair and r.common_pairs == 31 and len(r.rows) == 256


@pytest.mark.parametrize(
    "field,value",
    [
        ("volume", 0),
        ("amount", np.nan),
        ("adj_factor", -1),
        ("high", 1),
        ("low", 100),
        ("close", np.inf),
        ("open", 0),
    ],
)
def test_invalid_future_bar_stays_unknown(field, value):
    scores, future, times = inputs()
    future.loc[2, field] = value
    r = evaluate(scores, future, times)
    assert r.rows[0].adjusted_price_change is None and not r.rows[0].common_pair


def test_adjusted_prices_are_used_and_overflow_does_not_become_a_giant_return():
    scores, future, times = inputs()
    future.loc[5, "adj_factor"] = 4.0
    r = evaluate(scores, future, times)
    assert r.rows[0].adjusted_price_change == pytest.approx(1.0)
    future.loc[5, "adj_factor"] = 1e308
    assert evaluate(scores, future, times).rows[0].adjusted_price_change is None


@pytest.mark.parametrize("defect", ["duplicate", "outside_day", "foreign_code", "boolean_bar"])
def test_future_grid_cannot_expand_or_duplicate(defect):
    scores, future, times = inputs()
    if defect == "duplicate":
        future = pd.concat([future, future.iloc[:1]])
    elif defect == "outside_day":
        future.loc[0, "trade_date"] = pd.Timestamp("2026-09-22")
    elif defect == "foreign_code":
        future.loc[0, "instrument_id"] = "999999.SZ"
    else:
        future["close"] = True
    with pytest.raises(DataValidationError):
        evaluate(scores, future, times)


@pytest.mark.parametrize(
    "defect", ["asof", "duplicate", "missing", "boolean", "mask", "infinite", "cohort"]
)
def test_registered_scores_cannot_be_changed_or_missing_rows_dropped(defect):
    scores, future, times = inputs()
    codes = CODES
    if defect == "asof":
        scores.loc[0, "price_as_of"] = "2026-09-14"
    elif defect == "duplicate":
        scores.loc[0, "instrument_id"] = CODES[1]
    elif defect == "missing":
        scores = scores.iloc[1:]
    elif defect == "boolean":
        scores["S4-A"] = True
    elif defect == "mask":
        scores.loc[0, "S4_A_known"] = False
    elif defect == "infinite":
        scores.loc[0, "S4-A"] = np.inf
    else:
        codes = codes[:-1]
    with pytest.raises(DataValidationError):
        evaluate(scores, future, times, codes)


def test_premature_call_does_not_inspect_either_frame():
    _, _, times = inputs()
    times["evaluated_at"] = datetime(2026, 9, 13, tzinfo=UTC)
    with pytest.raises(DataValidationError, match="has not closed"):
        evaluate(object(), object(), times)


@pytest.mark.parametrize(
    "defect",
    [
        "naive",
        "backdated",
        "input_after_creation",
        "missing_day",
        "reorder",
        "early_source",
        "late_source",
        "before_close",
    ],
)
def test_explicit_future_source_times_and_original_chronology_are_required(defect):
    scores, future, times = inputs()
    source = list(times["future_sources_available_at"])
    if defect == "naive":
        times["evaluated_at"] = times["evaluated_at"].replace(tzinfo=None)
    elif defect == "backdated":
        times["created_at"] = datetime(2026, 9, 11, 8, tzinfo=UTC)
    elif defect == "input_after_creation":
        times["signal_inputs_available_at"] = times["created_at"] + timedelta(seconds=1)
    elif defect == "missing_day":
        source = source[:-1]
    elif defect == "reorder":
        source = source[::-1]
    elif defect == "early_source":
        source[0] = (source[0][0], datetime(2026, 9, 14, 6, 59, tzinfo=UTC))
    elif defect == "late_source":
        source[0] = (source[0][0], times["evaluated_at"] + timedelta(seconds=1))
    else:
        times["evaluated_at"] = datetime(2026, 9, 21, 6, 59, tzinfo=UTC)
    times["future_sources_available_at"] = tuple(source)
    with pytest.raises(DataValidationError):
        evaluate(scores, future, times)
