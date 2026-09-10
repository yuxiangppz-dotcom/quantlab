from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.round2_features import (
    AUGMENTED_FEATURES,
    build_round2_features,
    consecutive_macd,
    index_feature_frame,
)


def feature_fixture():
    dates = pd.bdate_range("2019-10-01", periods=280)
    frame = pd.DataFrame({"trade_date": dates, "instrument_id": "000001.SZ"})
    frame["close"] = 10 + np.arange(len(frame)) / 50 + np.sin(np.arange(len(frame)) / 7)
    frame["adj_close"] = frame["close"] * 2
    frame["open"] = frame["close"] - 0.2
    frame["low"] = frame["open"] - 0.3
    frame["high"] = frame["close"] + 0.1
    for key, value in {
        "amount": 1000.0,
        "circ_mv": 100.0,
        "total_mv": 200.0,
        "turnover_rate": 1.0,
        "return_1d": 0.01,
        "return_5d": 0.02,
        "return_20d": 0.03,
    }.items():
        frame[key] = value
    bundle = {"request": {"semantics": {"execution_authority": False}}, "series": []}
    for code in ("000001.SH", "000688.SH"):
        bundle["series"].append(
            {
                "metadata": {"ts_code": code},
                "rows": [
                    {
                        "trade_date": day.strftime("%Y-%m-%d"),
                        "close": float(i + 100),
                        "index_published_on_date": code != "000688.SH"
                        or day >= pd.Timestamp("2020-07-23"),
                    }
                    for i, day in enumerate(dates)
                ],
            }
        )
    return frame, list(dates.date), bundle


def test_macd_causal_and_continues_across_year_boundary():
    frame, dates, _ = feature_fixture()
    result = consecutive_macd(frame, dates)
    price = frame.adj_close
    dif = price.ewm(span=12, adjust=False).mean() - price.ewm(span=26, adjust=False).mean()
    expected = 2 * (dif - dif.ewm(span=9, adjust=False).mean()) / price
    expected.iloc[:59] = np.nan
    np.testing.assert_allclose(result, expected, equal_nan=True)
    changed = frame.copy()
    changed.loc[180:, "adj_close"] *= 8
    pd.testing.assert_series_equal(result.iloc[:180], consecutive_macd(changed, dates).iloc[:180])
    shuffled = frame.sample(frac=1, random_state=42)
    pd.testing.assert_series_equal(result, consecutive_macd(shuffled, dates).sort_index())


@pytest.mark.parametrize("mode", ["missing", "nan", "zero", "negative"])
def test_macd_resets_after_global_session_gap(mode):
    frame, dates, _ = feature_fixture()
    if mode == "missing":
        frame = frame.drop(index=100)
    else:
        frame.loc[100, "adj_close"] = {"nan": np.nan, "zero": 0, "negative": -1}[mode]
    result = consecutive_macd(frame, dates)
    assert result.loc[101:159].isna().all()
    assert np.isfinite(result.loc[160])
    assert np.isfinite(result.loc[99])


def test_macd_rejects_duplicate_and_unknown_dates():
    frame, dates, _ = feature_fixture()
    with pytest.raises(DataValidationError, match="duplicate"):
        consecutive_macd(pd.concat([frame, frame.iloc[:1]]), dates)
    with pytest.raises(DataValidationError, match="absent"):
        consecutive_macd(frame, dates[1:])
    with pytest.raises(DataValidationError, match="calendar"):
        consecutive_macd(frame, dates[::-1])


def test_index_publication_and_exact_complete_windows():
    _, dates, bundle = feature_fixture()
    result = index_feature_frame(bundle, dates).set_index("trade_date")
    assert result.loc[:"2020-07-22", "star50_close_to_ma120"].isna().all()
    assert np.isfinite(result.loc["2020-07-23", "star50_close_to_ma120"])
    broken = deepcopy(bundle)
    del broken["series"][0]["rows"][130]
    gap = index_feature_frame(broken, dates)
    assert gap.loc[130:150, "sse_return_20d"].isna().all()
    assert np.isfinite(gap.loc[151, "sse_return_20d"])
    assert gap.loc[130:249, "sse_close_to_ma120"].isna().all()
    assert np.isfinite(gap.loc[250, "sse_close_to_ma120"])


def test_all_features_are_causal_and_do_not_consume_labels():
    frame, dates, bundle = feature_fixture()
    expected = build_round2_features(frame, dates, bundle)
    changed = frame.copy()
    changed.loc[250:, ["open", "high", "low", "close", "adj_close", "circ_mv"]] *= 3
    changed["future_return_5d"] = np.arange(len(frame)) * 100
    later_bundle = deepcopy(bundle)
    for series in later_bundle["series"]:
        for row in series["rows"][250:]:
            row["close"] *= 5
    observed = build_round2_features(changed, dates, later_bundle)
    pd.testing.assert_frame_equal(
        expected.loc[:249, list(AUGMENTED_FEATURES)], observed.loc[:249, list(AUGMENTED_FEATURES)]
    )


def test_bad_caps_and_ohlc_mask_features_preserve_raw_rows():
    frame, dates, bundle = feature_fixture()
    frame.loc[250, "circ_mv"] = 201.0
    frame.loc[251, "total_mv"] = np.nan
    frame.loc[252, "low"] = 500.0
    result = build_round2_features(frame, dates, bundle)
    assert len(result) == len(frame)
    assert result.loc[250, "circ_mv"] == 201.0
    assert result.loc[250:251, ["small_size", "float_ratio"]].isna().all().all()
    assert result.loc[250:252, "common_features_available"].eq(False).all()
    assert np.isnan(result.loc[252, "wick_balance"])
    assert result.loc[253, "wick_balance"] == pytest.approx(0.2 / frame.loc[253, "open"])
    assert result.loc[253, "common_features_available"]


@pytest.mark.parametrize("mutation", ["authority", "duplicate", "missing"])
def test_index_identity_guard(mutation):
    _, dates, bundle = feature_fixture()
    if mutation == "authority":
        bundle["request"]["semantics"]["execution_authority"] = True
    elif mutation == "duplicate":
        bundle["series"].append(bundle["series"][0])
    else:
        bundle["series"].pop()
    with pytest.raises(DataValidationError):
        index_feature_frame(bundle, dates)
