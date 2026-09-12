"""Research labels, calendar gaps and missing flow must not leak into feature construction."""

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.funding_pilot import (
    FEATURES,
    FLOW,
    block_interval,
    compute_features,
    prepare_grid,
    rank_correlation,
    start_attempt,
)


@pytest.fixture
def sample():
    days = pd.bdate_range("2020-01-02", periods=65)
    codes = ["000001.SZ", "000002.SZ", "600000.SH"]
    records = []
    for i, code in enumerate(codes):
        for j, day in enumerate(days):
            price = 10 + (i + 1) * j / 100
            records.append(
                {
                    "instrument_id": code,
                    "trade_date": day,
                    "open": price,
                    "close": price,
                    "high": price + 1,
                    "low": price - 1,
                    "volume": 100000,
                    "amount": 1000000.0,
                    "adj_factor": 2.0,
                    "circ_mv": 1e9 * (i + 1),
                    "turnover_rate": 0.01,
                    "buy_lg_amount": 10.0 + i,
                    "buy_elg_amount": 2.0,
                    "sell_lg_amount": 6.0,
                    "sell_elg_amount": 1.0,
                }
            )
    return pd.DataFrame(records), days, codes


def for_code(result):
    return result[result.instrument_id == "000001.SZ"].reset_index(drop=True)


def test_hand_computed_units_windows_and_next_close_label(sample):
    frame, days, codes = sample
    result = for_code(compute_features(frame, days, codes))
    assert result.loc[3, "F1"] != result.loc[3, "F1"]
    assert result.loc[4, "F1"] == pytest.approx(0.05)
    assert result.loc[19, "F2"] == pytest.approx(0.05)
    assert result.loc[19, "F3"] == 1
    assert result.loc[20, "momentum20"] == pytest.approx(10.2 / 10 - 1)
    assert result.loc[20, "label_entry_date"] == days[21]
    assert result.loc[20, "label_exit_date"] == days[26]
    assert result.loc[20, "label_5"] == pytest.approx(10.26 / 10.21 - 1)


def test_future_changes_do_not_change_past_features(sample):
    frame, days, codes = sample
    before = compute_features(frame, days, codes)
    changed = frame.copy()
    changed.loc[changed.trade_date >= days[40], [*FLOW, "amount", "close", "adj_factor"]] *= 3
    after = compute_features(changed, days, codes)
    mask = before.trade_date < days[40]
    pd.testing.assert_frame_equal(before.loc[mask, list(FEATURES)], after.loc[mask, list(FEATURES)])


def test_missing_flow_breaks_windows_and_is_not_zero(sample):
    frame, days, codes = sample
    mask = (frame.instrument_id == codes[0]) & frame.trade_date.eq(days[25])
    frame.loc[mask, "buy_lg_amount"] = np.nan
    result = for_code(compute_features(frame, days, codes))
    assert result.loc[25:29, "F1"].isna().all()
    assert result.loc[30, "F1"] == pytest.approx(0.05)
    assert result.loc[25:44, ["F2", "F3"]].isna().all().all()
    assert result.loc[45, "F3"] == 1
    assert pd.notna(result.loc[25, "label_5"])


def test_missing_session_is_not_compressed_and_breaks_label(sample):
    frame, days, codes = sample
    mask = (frame.instrument_id == codes[0]) & frame.trade_date.eq(days[25])
    result = for_code(compute_features(frame[~mask], days, codes))
    assert len(result) == len(days)
    assert result.loc[25, "trade_date"] == days[25]
    assert result.loc[19:24, "label_5"].isna().all()
    assert result.loc[25:29, "F1"].isna().all()
    assert result.loc[25:45, "momentum20"].isna().all()


def test_label_requires_same_year_and_does_not_affect_features(sample):
    frame, _, codes = sample
    days = pd.bdate_range("2020-11-02", periods=65)
    frame["trade_date"] = list(days) * len(codes)
    result = for_code(compute_features(frame, days, codes))
    crossing = result.trade_date.dt.year.eq(2020) & result.label_exit_date.dt.year.eq(2021)
    assert crossing.any() and result.loc[crossing, "label_5"].isna().all()
    assert result.loc[crossing, "F1"].notna().all()


@pytest.mark.parametrize(
    "column,value",
    [
        ("amount", -1),
        ("high", 0),
        ("close", 100),
        ("buy_lg_amount", -2),
        ("buy_elg_amount", True),
        ("sell_lg_amount", "12"),
        ("volume", 0),
    ],
)
def test_invalid_values_remain_unknown_not_clean_signal(sample, column, value):
    frame, days, codes = sample
    frame[column] = frame[column].astype(object)
    frame.loc[25, column] = value
    result = for_code(compute_features(frame, days, codes))
    assert pd.isna(result.loc[25, "F1"])


def test_missing_exposure_fields_do_not_prune_factor_or_label(sample):
    frame, days, codes = sample
    expected = compute_features(frame, days, codes)
    frame[["circ_mv", "turnover_rate"]] = np.nan
    actual = compute_features(frame, days, codes)
    pd.testing.assert_frame_equal(expected[[*FEATURES, "label_5"]], actual[[*FEATURES, "label_5"]])


def test_duplicate_input_identity_is_rejected(sample):
    frame, days, codes = sample
    with pytest.raises(DataValidationError, match="duplicate"):
        prepare_grid(pd.concat([frame, frame.iloc[:1]]), days, codes)


def test_wrong_date_and_unknown_code_are_rejected(sample):
    frame, days, codes = sample
    with pytest.raises(DataValidationError):
        prepare_grid(frame, days[1:], codes)
    with pytest.raises(DataValidationError):
        prepare_grid(frame, days, codes[:-1])


def test_f4_uses_same_cross_section_and_average_ties(sample):
    frame, days, codes = sample
    frame.loc[frame.instrument_id == codes[1], FLOW] = frame.loc[
        frame.instrument_id == codes[0], FLOW
    ].to_numpy()
    result = compute_features(frame, days, codes)
    g = result[result.trade_date == days[30]]
    expected = g.F2.rank(pct=True, method="average") - g.momentum20.rank(pct=True, method="average")
    np.testing.assert_allclose(g.F4, expected)


def test_constant_and_small_cross_sections_are_unknown():
    assert rank_correlation([1, 1, 1], [1, 2, 3], minimum=3) == (None, 3)
    assert rank_correlation([1, 2], [2, 3], minimum=3) == (None, 2)
    value, n = rank_correlation([1, 2, 2, 4], [4, 2, 2, 1], minimum=3)
    assert value == pytest.approx(-1) and n == 4


def test_rank_ic_missing_pairs_do_not_become_zeros():
    value, n = rank_correlation([1, 2, np.nan, 4], [4, 3, 2, np.inf], minimum=2)
    assert value == pytest.approx(-1) and n == 2


def test_block_bootstrap_is_fixed_and_handles_unknown_intervals():
    assert block_interval([1, 2, 3]) is None
    assert block_interval([np.nan] * 100) is None
    a = block_interval(np.arange(100, dtype=float), samples=100)
    b = block_interval(np.arange(100, dtype=float), samples=100)
    assert a == b and a[0] < 49.5 < a[1]
    assert block_interval([0.2] * 100, samples=100) == pytest.approx([0.2, 0.2])


def test_actual_attempt_cannot_be_reset_or_restarted(tmp_path):
    result = start_attempt(tmp_path, "bound-inputs", "clean-pushed-source")
    assert result["funding_feature_ids"] == list(FEATURES)
    assert result["economic_paths_used"] == 0
    with pytest.raises(DataValidationError, match="already consumed"):
        start_attempt(tmp_path, "another-id", "another-source")


def test_prior_outputs_without_intent_cannot_be_overwritten(tmp_path):
    (tmp_path / "features_and_labels.parquet").write_bytes(b"unexplained prior run")
    with pytest.raises(DataValidationError, match="unexplained prior"):
        start_attempt(tmp_path, "bound-inputs", "clean-pushed-source")
