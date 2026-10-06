"""Formula vectors and point-in-time boundaries, not alpha tests."""

from datetime import date, timedelta

import pytest

from quantlab.data.models import DailyBar, DailyBasic
from quantlab.scout.demo import make_demo_market
from quantlab.scout.facts import program_facts
from quantlab.scout.market import scan_market
from quantlab.scout.technical import VERSION, technical_snapshot

CODE = "600001.SH"


def sample(closes):
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    bars = [
        DailyBar(CODE, day, close, close + 1, close - 1, close, close, 1000, 100000)
        for day, close in zip(days, closes, strict=True)
    ]
    factors = {(CODE, day): 1.0 for day in days}
    return days, bars, factors


def snap(closes):
    days, bars, factors = sample(closes)
    return technical_snapshot(CODE, bars, factors, days, days[-1])


def number(snapshot, key):
    return snapshot["metrics"][key]["value"]


def test_linear_macd_sma_seed_and_wilder_vectors():
    result = snap(list(range(10, 130)))
    assert number(result, "ma5") == 127
    assert number(result, "ma60") == 99.5
    # A linear slope of one has EMA12 lag 5.5 and EMA26 lag 12.5:
    # DIF=7, DEA=7, histogram*2=0 independent of the current price scale.
    assert number(result, "macd_dif") == pytest.approx(7)
    assert number(result, "macd_dea") == pytest.approx(7)
    assert number(result, "macd_hist2") == pytest.approx(0, abs=1e-12)
    assert number(result, "macd_dif_to_close") == pytest.approx(7 / 129)
    assert number(result, "rsi14") == 100
    assert number(result, "atr14") == 2
    assert number(result, "atr14_to_close") == pytest.approx(2 / 129)


def test_published_wilder_rsi_seed_vector():
    # The conventional Wilder worked vector: 14 initial changes -> RSI 70.4641.
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
    ]
    initial = snap(closes[:-1])
    updated = snap(closes)
    assert number(initial, "rsi14") == pytest.approx(70.4641350211)
    assert number(updated, "rsi14") == pytest.approx(66.2496185536)
    assert number(snap(closes[:14]), "rsi14") is None


def test_atr_true_range_includes_gap_then_wilder_update():
    days, bars, factors = sample([10] * 15 + [15])
    result = technical_snapshot(CODE, bars, factors, days, days[-1])
    # Fourteen initial TR=2; the last TR=max(2,16-10,14-10)=6.
    assert number(result, "atr14") == pytest.approx((2 * 13 + 6) / 14)
    assert number(result, "open_gap_1d") == pytest.approx(0.5)


def test_future_append_does_not_change_frozen_snapshot_or_anchor():
    days, bars, factors = sample(list(range(10, 131)))
    asof = days[-2]
    before = technical_snapshot(CODE, bars[:-1], factors, days[:-1], asof)
    factors[(CODE, days[-1])] = 100
    after = technical_snapshot(CODE, bars, factors, days, asof)
    assert after == before
    assert len(after["history"]) == 120
    assert len(after["short_context"]) == 10


def test_split_uses_one_d0_anchor_no_false_breakout():
    days, bars, factors = sample([100] * 119 + [50])
    # Raw history high=101 -> adjusted D0 price high=50.5.
    factors[(CODE, days[-1])] = 2
    last = bars[-1]
    bars[-1] = DailyBar(CODE, last.trade_date, 50, 50.5, 49.5, 50, 50, 1000, 100000)
    result = technical_snapshot(CODE, bars, factors, days, days[-1])
    assert number(result, "ma60") == 50
    assert number(result, "breakout_20d") == pytest.approx(50 / 50.5 - 1)
    assert number(result, "macd_dif") == 0
    assert result["history"][0]["raw_ohlc"] == [100, 101, 99, 100]


def test_missing_or_suspended_day_resets_indicators_no_zero_fill():
    days, bars, factors = sample(list(range(10, 130)))
    result = technical_snapshot(CODE, bars[:60] + bars[61:], factors, days, days[-1])
    assert result["contiguous_sessions"] == 59
    assert number(result, "ma60") is None
    assert result["metrics"]["ma60"]["status"] == "insufficient_warmup"
    assert number(result, "macd_dif") is None
    assert number(result, "ma20") is not None
    latest = bars[-1]
    bars[-1] = DailyBar(CODE, latest.trade_date, 129, 129, 129, 129, 129, 0, 0)
    stopped = technical_snapshot(CODE, bars, factors, days, days[-1])
    assert stopped["status"] == "no_trading_activity"
    assert all(item["value"] is None for item in stopped["metrics"].values())


def test_zero_range_flat_rsi_and_separate_amount_volume():
    days, bars, factors = sample([10] * 120)
    latest = bars[-1]
    bars[-1] = DailyBar(CODE, latest.trade_date, 10, 10, 10, 10, 10, 2000, 500000)
    basic = DailyBasic(CODE, latest.trade_date, 0.052, 9000000, 8000000)
    result = technical_snapshot(CODE, bars, factors, days, days[-1], basic)
    assert number(result, "body_ratio") is None
    assert result["metrics"]["close_location"]["status"] == "zero_range"
    assert number(result, "rsi14") is None
    assert result["metrics"]["rsi14"]["status"] == "no_price_change"
    assert number(result, "amount_ratio_5d") == 5
    assert number(result, "volume_ratio_5d") == 2
    assert number(result, "turnover_rate_pct") == pytest.approx(5.2)
    assert number(result, "circ_mv_cny") == 8000000


def test_typed_snapshot_facts_retain_null_status_and_dependencies():
    result = snap([10] * 21)
    candidate = {"instrument_id": CODE, "metrics": {}, "context": {"technical_snapshot": result}}
    facts = {row["metric"]: row for row in program_facts(candidate, "2026-01-21")}
    assert facts["ma60"]["value"] is None
    assert facts["ma60"]["status"] == "insufficient_warmup"
    assert facts["macd_hist2"]["calculation_version"] == VERSION
    assert facts["macd_hist2"]["raw_dependencies"]["required_contiguous_sessions"] == 78
    assert facts["volume_ratio_5d"]["unit"] == "times"
    assert facts["ma5"]["unit"] == "adjusted_CNY"
    assert all(row["subject_id"] == CODE for row in facts.values())


@pytest.mark.parametrize("count", [1, 5, 10])
def test_short_history_is_null_not_calculation_failure(count):
    result = snap([10] * count)
    assert number(result, "return_10d") is None
    assert number(result, "macd_dif") is None
    assert number(result, "rsi14") is None
    assert result["metrics"]["return_10d"]["status"] == "insufficient_warmup"


def test_scanner_opt_out_preserves_legacy_eligibility_and_payload(tmp_path):
    day = make_demo_market(tmp_path)
    current, current_audit = scan_market(tmp_path, day)
    legacy, legacy_audit = scan_market(tmp_path, day, technical_enabled=False)
    assert set(current) == set(legacy)
    assert current_audit["rejected"] == legacy_audit["rejected"]
    for code in current:
        assert current[code].metrics == legacy[code].metrics
        assert legacy[code].context == {}
        assert "technical_snapshot" in current[code].context
