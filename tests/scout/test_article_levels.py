from copy import deepcopy
from datetime import date, timedelta

import pytest

from quantlab.scout.article_levels import (
    check_open_reference,
    compute_price_levels,
    conservative_tick,
    evaluate_frozen_level,
    freeze_entry_reference,
    level_epsilon,
    merge_level_candidates,
)


def price_samples(n=120):
    dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    rows = [
        {
            "date": day,
            "open": 10,
            "high": 10.1,
            "low": 9.9,
            "close": 10,
            "amount_cny": 100_000_000,
            "volume_shares": 10_000_000,
            "adj_factor": 1,
        }
        for day in dates
    ]
    return rows, dates


def candidate(price, date="2026-01-01", source="swing_high"):
    return {
        "price": price,
        "source_type": source,
        "formed_at": date,
        "confirmed_at": date,
        "significant": source == "swing_high",
    }


def frozen_band(dates, index=0, initial_role="support"):
    return {
        "level_id": "old-band",
        "lower": 9.9,
        "upper": 10.1,
        "center": 10,
        "source_types": ["swing_low"],
        "origins": [],
        "formed_at": dates[index],
        "confirmed_at": dates[index],
        "significant_origin": True,
        "initial_role": initial_role,
    }


def manual_levels(atr=0.2, levels=None):
    return {"status": "complete", "atr14": atr, "levels": levels or []}


def resistance(lower, upper, level_id="near"):
    return {
        "level_id": level_id,
        "lower": lower,
        "upper": upper,
        "center": (lower + upper) / 2,
        "status": "active",
        "significant": True,
        "source_types": ["swing_high"],
        "origins": [{"source_type": "swing_high", "significant": True}],
    }


def freeze(rows, levels, **kwargs):
    return freeze_entry_reference(
        rows,
        levels,
        setup={"route": "base_breakout", "B": 10, "F": 9.5},
        market_mode="active",
        signal_date=rows[-1]["date"],
        **kwargs,
    )


def test_cluster_total_span_prevents_chain_and_original_band_width_is_not_shrunk():
    result = merge_level_candidates([candidate(10.10), candidate(10), candidate(10.05)], atr14=0)
    assert len(result) == 2
    assert len(result[0]["origins"]) == 2
    expected_lower = 10 - level_epsilon(10, 0)
    expected_upper = 10.05 + level_epsilon(10.05, 0)
    assert result[0]["lower"] == expected_lower
    assert result[0]["upper"] == expected_upper
    assert result[0]["center"] == 10.025
    assert (
        merge_level_candidates(
            list(reversed([candidate(10.10), candidate(10), candidate(10.05)])), 0
        )
        == result
    )


def test_pivots_need_two_completed_right_bars_and_equal_platform_is_not_a_pivot():
    rows, dates = price_samples()
    rows[-3]["high"] = 12
    baseline = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    peak = baseline["latest_confirmed_swing_high"]
    assert peak["date"] == dates[-3]
    assert peak["confirmed_at"] == dates[-1]
    before = compute_price_levels(rows, dates, signal_date=dates[-2], setup={})
    assert before["latest_confirmed_swing_high"] is None
    rows[-2]["high"] = 12
    assert (
        compute_price_levels(rows, dates, signal_date=dates[-1], setup={})[
            "latest_confirmed_swing_high"
        ]
        is None
    )


def test_future_injection_and_input_row_order_cannot_change_frozen_levels():
    rows, dates = price_samples()
    rows[-4]["high"] = 11
    result = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    future = {**rows[-1], "date": "2027-01-01", "high": 100, "close": 50, "open": 50}
    assert (
        compute_price_levels(
            list(reversed(rows)) + [future], dates, signal_date=dates[-1], setup={}
        )
        == result
    )


def test_current_atr_cannot_backfill_old_touches_or_change_a_saved_frozen_band():
    rows, dates = price_samples()
    rows[30]["high"] = 11
    rows[50]["high"] = 10.985
    old = {
        "level_id": "actual-old-freeze",
        "lower": 10.97,
        "upper": 11.03,
        "center": 11,
        "source_types": ["swing_high"],
        "origins": [],
        "formed_at": dates[30],
        "confirmed_at": dates[32],
        "band_frozen_at": dates[32],
        "significant_origin": True,
    }
    before = evaluate_frozen_level(old, rows, dates, signal_date=dates[-1])
    assert [item["date"] for item in before["touch_events"]] == [dates[50]]
    # A large later signal-day range changes ATR[t] and today's band widths.
    rows[-1]["high"] = 40
    current = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    assert all(not level["touch_events"] for level in current["levels"])
    assert all(level["band_frozen_at"] == dates[-1] for level in current["levels"])
    pivot_origins = [
        item
        for level in current["levels"]
        for item in level["origins"]
        if item["source_type"] == "swing_high" and item["formed_at"] == dates[30]
    ]
    assert pivot_origins[0]["confirmed_at"] == dates[32]
    after = evaluate_frozen_level(old, rows, dates, signal_date=dates[-1])
    assert after["lower"] == before["lower"] == 10.97
    assert [item for item in after["touch_events"] if item["date"] < dates[-1]] == before[
        "touch_events"
    ]


def test_gap_partial_fill_and_complete_fill_use_real_ranges():
    rows, dates = price_samples()
    for row in rows[-2:]:
        row.update({"open": 11, "high": 11.2, "low": 10.8, "close": 11})
    rows[-1]["low"] = 10.5
    result = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    gap = next(item for item in result["levels"] if "unfilled_up_gap" in item["source_types"])
    assert gap["lower"] == 10.1
    assert gap["upper"] == 10.5
    assert gap["band_build_rule"] == "actual_unfilled_gap"
    rows[-1]["low"] = 10.1
    result = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    assert not any("unfilled_up_gap" in item["source_types"] for item in result["levels"])


def test_signal_day_new_gap_has_its_original_upper_bound():
    rows, dates = price_samples()
    rows[-1].update({"open": 11, "high": 11.2, "low": 10.8, "close": 11})
    result = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    gap = next(item for item in result["levels"] if "unfilled_up_gap" in item["source_types"])
    assert gap["upper"] == 10.8
    assert gap["confirmed_at"] == dates[-1]


def test_touch_events_are_separated_by_three_calendar_sessions_and_no_backfill():
    rows, dates = price_samples(8)
    for row in rows:
        row["close"] = row["open"] = 10.04
    band = frozen_band(dates, index=1)
    result = evaluate_frozen_level(band, rows, dates, signal_date=dates[-1])
    assert [item["date"] for item in result["touch_events"]] == [dates[2], dates[5]]
    assert result["independent_touches"] == 2
    assert all(item["date"] > band["confirmed_at"] for item in result["touch_events"])
    before = evaluate_frozen_level(band, rows, dates, signal_date=dates[0])
    assert before["status"] == "unknown"
    assert not before["touch_events"]


def test_two_closes_break_resistance_but_without_retest_only_potential_support():
    rows, dates = price_samples(5)
    rows[0].update({"open": 9.7, "high": 9.8, "low": 9.6, "close": 9.7})
    for row in rows[1:]:
        row.update({"open": 10.3, "high": 10.4, "low": 10.2, "close": 10.3})
    band = frozen_band(dates, initial_role="resistance")
    first = evaluate_frozen_level(band, rows, dates, signal_date=dates[1])
    assert not first["role_events"]
    result = evaluate_frozen_level(band, rows, dates, signal_date=dates[-1])
    assert result["current_role"] == "potential_support"
    assert result["role_events"][0]["date"] == dates[2]
    assert result["independent_touches"] == 0


def test_support_break_uses_historically_frozen_bounds_not_a_new_band():
    rows, dates = price_samples(24)
    band = frozen_band(dates, index=20)
    rows[21].update({"open": 9.9, "high": 10, "low": 9.7, "close": 9.8, "amount_cny": 130_000_000})
    result = evaluate_frozen_level(band, rows, dates, signal_date=dates[-1])
    assert result["status"] == "invalidated"
    assert result["role_events"][0]["band_low_at_test"] == 9.9
    new_band = {**band, "lower": 9.5, "center": 9.8, "upper": 10.1, "confirmed_at": dates[-1]}
    current = evaluate_frozen_level(new_band, rows, dates, signal_date=dates[-1])
    assert current["status"] == "active"
    assert not current["touch_events"]
    assert result["status"] == "invalidated"


def test_missing_calendar_history_and_nonfinite_prices_are_unknown():
    rows, dates = price_samples()
    rows.pop(5)
    result = compute_price_levels(rows, dates, signal_date=dates[-1], setup={})
    assert result["status"] == "unknown"
    assert result["missing_dates"] == [dates[5]]
    assert freeze(rows, result)["action"] == "review_required"
    rows, dates = price_samples()
    rows[5]["adj_factor"] = float("nan")
    assert compute_price_levels(rows, dates, signal_date=dates[-1], setup={})["status"] == "unknown"


def test_nearest_resistance_is_not_skipped_and_invalidation_never_moved_to_improve_rr():
    rows, _ = price_samples()
    original = deepcopy(rows)
    levels = manual_levels(levels=[resistance(10.12, 10.2), resistance(13, 13.1, "far")])
    result = freeze(rows, levels)
    assert result["nearest_resistance_id"] == "near"
    assert result["invalidation_raw"] == pytest.approx(9.8)
    assert result["invalidation"] == 9.8
    assert result["entry_high_before_resistance"] == 10.2
    assert result["entry_high_after_resistance"] < result["entry_low"]
    assert result["status"] == "invalid"
    assert result["action"] == "watch_only"
    assert rows == original


def test_at_resistance_is_watch_and_route_reference_only_band_is_not_double_counted():
    rows, dates = price_samples()
    at = freeze(rows, manual_levels(levels=[resistance(9.98, 10.1)]))
    assert "at_resistance" in at["reason_codes"]
    assert at["action"] == "watch_only"
    only_reference = resistance(9.95, 10.05)
    only_reference.update(
        {
            "source_types": ["box_upper"],
            "origins": [{"source_type": "box_upper", "significant": True}],
        }
    )
    allowed = freeze(rows, manual_levels(levels=[only_reference]))
    assert allowed["status"] == "valid"
    assert allowed["nearest_resistance_id"] is None
    only_reference["origins"].append({"source_type": "swing_high", "significant": True})
    assert freeze(rows, manual_levels(levels=[only_reference]))["action"] == "watch_only"


def test_no_significant_resistance_is_unknown_geometry_not_infinite_reward():
    rows, _ = price_samples()
    result = freeze(rows, manual_levels())
    assert result["status"] == "valid"
    assert result["geometric_rr"] is None
    assert "rr_unknown_no_significant_resistance" in result["reason_codes"]
    assert result["entry_check"] == "pending"


def test_selective_caps_price_and_risk_more_tightly():
    rows, dates = price_samples()
    rows[-1].update({"open": 10.1, "close": 10.1, "high": 10.2, "low": 9.9})
    active = freeze(rows, manual_levels())
    selective = freeze_entry_reference(
        rows,
        manual_levels(),
        setup={"route": "base_breakout", "B": 10},
        market_mode="selective",
        signal_date=dates[-1],
    )
    assert active["r_max"] == 0.08
    assert selective["r_max"] == 0.06
    assert selective["entry_high"] < active["entry_high"]


def test_route_b_invalidation_uses_pullback_through_signal_not_just_past_low():
    rows, dates = price_samples()
    rows[-5]["high"] = 10.4
    rows[-1]["low"] = 9.7
    setup = {"route": "pullback_recovery", "p": dates[-5], "q": dates[-15]}
    result = freeze_entry_reference(
        rows, manual_levels(), setup=setup, market_mode="active", signal_date=dates[-1]
    )
    assert result["reference"] == 10.1
    assert result["invalidation_raw"] == pytest.approx(9.6)
    assert result["invalidation"] == 9.6


def test_tick_rounding_is_conservative_without_binary_float_surprises():
    assert conservative_tick(10.001, 0.01, up=True) == 10.01
    assert conservative_tick(10.009, 0.01, up=False) == 10.00
    assert conservative_tick(9.81, 0.01, up=False) == 9.81


def test_open_reference_keeps_failures_and_does_not_infer_fill_or_later_prices():
    rows, _ = price_samples()
    frozen = freeze(rows, manual_levels())
    before = deepcopy(frozen)
    assert (
        check_open_reference(frozen, open_price=11, trading_status="tradable")["entry_check"]
        == "fail"
    )
    assert (
        check_open_reference(frozen, open_price=10.05, trading_status="tradable")["entry_check"]
        == "pass"
    )
    assert (
        check_open_reference(frozen, open_price=10.05, trading_status="tradable")["fill_status"]
        == "not_inferred"
    )
    assert (
        check_open_reference(frozen, open_price=10.05, trading_status="suspended")["entry_check"]
        == "fail"
    )
    assert (
        check_open_reference(frozen, open_price=10.05, trading_status="unknown")["entry_check"]
        == "unobservable"
    )
    assert (
        check_open_reference(
            frozen, open_price=10.05, trading_status="tradable", price_scale_confirmed=False
        )["entry_check"]
        == "unobservable"
    )
    assert frozen == before
