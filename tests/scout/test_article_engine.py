"""Independent synthetic counterexamples; no provider or profitability claims."""

import json
import os
from collections import defaultdict
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantlab.scout.article_config import DEFAULT_CONFIG, config_hash
from quantlab.scout.article_engine import (
    allocate_budget,
    analyze_stock,
    assess_groups,
    assess_market,
    compute_metrics,
    evaluate_routes,
    normalize_bars,
)

SH = ZoneInfo("Asia/Shanghai")


@pytest.mark.skipif(
    not os.environ.get("SCOUT_ARTICLE_MARKET_AUDIT_ROOT"),
    reason="opt-in read-only actual window-coverage audit",
)
def test_actual_saved_market_windows_coverage_and_unknown_history():
    from quantlab.scout.article_data import _index, _number, _partition, _read, bind_context

    root = Path(os.environ["SCOUT_ARTICLE_MARKET_AUDIT_ROOT"])
    saved = json.loads((root / "prepared-real-230102.json").read_text(encoding="utf-8"))
    pack = json.loads((root / "full-source-pack-20261008T230102.json").read_text(encoding="utf-8"))
    signal = saved["environment"]["signal_date"]
    cutoff = saved["research"]["cutoff_at"]
    market_root = root / "market"
    calendar = sorted(
        {
            row["trade_date"]
            for row in _read(market_root / "calendar" / "calendar.parquet")
            if row.get("exchange") == "SSE" and row.get("is_open") is True
        }
    )
    index = calendar.index(signal)
    context = bind_context(
        {"securities": _read(market_root / "securities" / "securities.parquet")},
        pack,
        signal,
        cutoff,
    )
    by_code = defaultdict(list)
    for current in calendar[index - 19 : index + 1]:
        day = date.fromisoformat(current)
        daily, conflicts = _index(_read(_partition(market_root, "daily", day)), "daily", day)
        factors, factor_conflicts = _index(
            _read(_partition(market_root, "adj_factor", day)), "adj_factor", day
        )
        limits, _ = _index(
            _read(_partition(market_root, "daily_price_limit", day)), "daily_price_limit", day
        )
        for key, raw in daily.items():
            canonical = "instrument_id" in raw and "volume" in raw
            raw_supplier = "ts_code" in raw and "vol" in raw and "instrument_id" not in raw
            amount = _number(raw.get("amount"))
            volume = _number(raw.get("volume" if canonical else "vol"))
            invalid = key in conflicts or key in factor_conflicts
            by_code[key[0]].append(
                {
                    "date": current,
                    **{
                        name: None if invalid else _number(raw.get(name))
                        for name in ("open", "high", "low", "close")
                    },
                    "amount_cny": amount
                    if canonical
                    else amount * 1000
                    if raw_supplier and amount is not None
                    else None,
                    "volume_shares": volume
                    if canonical
                    else volume * 100
                    if raw_supplier and volume is not None
                    else None,
                    "adj_factor": None if invalid else factors.get(key, {}).get("adj_factor"),
                    "up_limit": limits.get(key, {}).get("up_limit"),
                    "down_limit": limits.get(key, {}).get("down_limit"),
                }
            )
    analyses = {
        code: analyze_stock(code, rows, calendar, signal, context["security_by_code"].get(code, {}))
        for code, rows in by_code.items()
    }
    result = assess_market(analyses, calendar, signal)
    metrics = result["metrics"]
    assert metrics["expected_count"] == saved["environment"]["metrics"]["expected_count"]
    assert metrics["valid_count"] == saved["environment"]["metrics"]["valid_count"]
    for dimension in ("ret3", "above_ma20"):
        assert metrics["window_coverage"][dimension]["expected_count"] == metrics["expected_count"]
        assert (
            metrics["window_coverage"][dimension]["coverage"] >= DEFAULT_CONFIG.market_coverage_min
        )
        assert metrics["window_coverage"][dimension]["status"] == "complete"
    assert metrics["median_ret3"] is not None and metrics["above_ma20_ratio"] is not None
    assert result["risk_flags"]["R4"] is None and result["state"] == "unknown"
    print(
        json.dumps(
            {
                "signal_date": signal,
                "expected_count": metrics["expected_count"],
                "valid_count": metrics["valid_count"],
                "window_coverage": metrics["window_coverage"],
                "median_ret3": metrics["median_ret3"],
                "above_ma20_ratio": metrics["above_ma20_ratio"],
                "R4": result["risk_flags"]["R4"],
                "state": result["state"],
            },
            ensure_ascii=False,
        )
    )


def calendar():
    start = date(2025, 1, 1)
    result = []
    while len(result) < 180:
        if start.weekday() < 5:
            result.append(start.isoformat())
        start += timedelta(days=1)
    return result


CALENDAR = calendar()
SIGNAL = CALENDAR[-1]


def security():
    return {
        "list_date": CALENDAR[0],
        "is_st": False,
        "is_delisting": False,
        "is_suspended": False,
        "status_by_date": {day: {"is_st": False, "is_delisting": False} for day in CALENDAR},
        "suspended_by_date": {day: False for day in CALENDAR},
    }


def bars():
    return [
        {
            "date": day,
            "open": 10,
            "high": 10.05,
            "low": 9.95,
            "close": 10,
            "amount_cny": 200_000_000,
            "volume_shares": 20_000_000,
            "adj_factor": 1,
            "up_limit": 11,
            "down_limit": 9,
        }
        for day in CALENDAR[-120:]
    ]


def breakout():
    result = bars()
    result[-1].update(open=10.04, high=10.23, low=10.02, close=10.20, amount_cny=320_000_000)
    return result


def recovery():
    result = bars()
    for row in result[-35:]:
        row.update(open=10.8, high=10.85, low=10.7, close=10.8)
    result[-22].update(low=10.0)
    result[-5].update(open=11.3, high=11.5, low=11.2, close=11.35)
    for row, high, low, close in zip(
        result[-4:-1],
        [11.25, 11.15, 11.05],
        [10.8, 10.75, 10.72],
        [11.0, 10.95, 10.85],
        strict=True,
    ):
        row.update(open=close + 0.05, high=high, low=low, close=close, amount_cny=80_000_000)
    result[-1].update(open=10.87, high=11.15, low=10.76, close=11.12, amount_cny=150_000_000)
    return result


def analyze(raw=None, code="600001.SH", sec=None):
    return analyze_stock(code, raw or breakout(), CALENDAR, SIGNAL, sec or security())


def test_frozen_config_and_hash_change_only_with_explicit_new_parameters():
    with pytest.raises(FrozenInstanceError):
        DEFAULT_CONFIG.breakout_max = 0.04
    assert config_hash() == config_hash(replace(DEFAULT_CONFIG))
    assert config_hash() != config_hash(replace(DEFAULT_CONFIG, breakout_max=0.04))
    assert DEFAULT_CONFIG.parameter_status == "unvalidated_initial_research_parameters"


def test_amount_median_excludes_today_and_exact_missing_session_stays_unknown():
    raw = breakout()
    raw[-1]["amount_cny"] = 2_000_000_000
    stock = analyze(raw)
    assert stock["metrics"]["median_amount20"] == 200_000_000
    assert stock["metrics"]["amount_ratio"] == 10
    assert stock["routes"]["base_breakout"]["status"] == "fail"
    deleted = raw.pop(-10)
    stock = analyze(raw)
    assert deleted["date"] in stock["history"]["missing_dates"]
    assert stock["metrics"]["amount_ratio"] is None
    assert stock["universe"]["candidate_status"] == "unknown"
    assert not stock["universe"]["rejection_reasons"]


def test_historical_prices_anchor_to_signal_factor_without_false_split_return():
    raw = bars()
    for row in raw[:-1]:
        for key in ("open", "high", "low", "close"):
            row[key] *= 2
    raw[-1]["adj_factor"] = 2
    stock = analyze(raw)
    assert stock["metrics"]["ret1"] == pytest.approx(0)
    assert stock["adjusted_bars"][-1]["close"] == raw[-1]["close"]
    assert stock["adjusted_bars"][-2]["raw_close"] == 20
    assert stock["adjusted_bars"][-2]["close"] == 10


def test_breakout_uses_prior_box_not_signal_high_and_close_must_confirm():
    stock = analyze()
    route = stock["routes"]["base_breakout"]
    assert route["status"] == "pass"
    assert route["box_high"] == 10.05
    raw = breakout()
    raw[-1]["high"] = 12
    mutated = analyze(raw)
    assert mutated["routes"]["base_breakout"]["box_high"] == route["box_high"]
    raw[-1].update(close=10.02, open=10, high=10.23, low=9.99)
    intraday_only = analyze(raw)
    rules = intraday_only["routes"]["base_breakout"]["rule_results"]
    assert next(rule for rule in rules if rule["rule"] == "close_breakout")["status"] == "fail"


@pytest.mark.parametrize("excess,expected", [(0, "pass"), (1e-10, "fail"), (0.001, "fail")])
def test_breakout_three_percent_inclusive_boundary_and_real_excess(excess, expected):
    raw = breakout()
    price = 10.05 * (1.03 + excess)
    raw[-1].update(open=10.10, high=price + 0.02, low=10.05, close=price)
    route = analyze(raw)["routes"]["base_breakout"]
    rule = next(row for row in route["rule_results"] if row["rule"] == "breakout_distance")
    assert rule["status"] == expected
    assert route["status"] == expected
    if excess == 0:
        assert rule["value"] > 0.03  # Explicitly reproduces binary arithmetic noise.


def test_inclusive_ratio_tolerance_does_not_relax_tick_or_liquidity_qualification():
    raw = breakout()
    for row in raw[:-1]:
        row["high"] = 10
    raw[-1].update(open=10, high=10.02, low=10, close=10.01 - 5e-13)
    route = analyze(raw)["routes"]["base_breakout"]
    assert (
        next(row for row in route["rule_results"] if row["rule"] == "close_breakout")["status"]
        == "pass"
    )
    assert (
        next(row for row in route["rule_results"] if row["rule"] == "breakout_tick")["status"]
        == "fail"
    )
    raw = breakout()
    for row in raw[:-1]:
        row["amount_cny"] = 100_000_000 - 0.01
    stock = analyze(raw)
    assert stock["universe"]["candidate_status"] == "fail"
    assert "liquidity_fail" in stock["universe"]["rejection_reasons"]


@pytest.mark.parametrize(
    "field,bound,rule",
    [
        ("amount_ratio", 1.2, "moderate_amount"),
        ("close_location", 0.65, "close_quality"),
        ("upper_shadow", 0.25, "upper_shadow"),
        ("ret5", 0.12, "ret5_heat"),
        ("ma20_distance", 0.10, "ma20_heat"),
    ],
)
def test_route_a_inclusive_ratio_comparisons_use_only_absolute_roundoff(field, bound, rule):
    stock = analyze()
    m = deepcopy(stock["metrics"])
    # Lower boundaries drift down; upper boundaries drift up.
    direction = -1 if field in {"amount_ratio", "close_location"} else 1
    m[field] = bound + direction * 5e-13
    route = evaluate_routes(stock["adjusted_bars"], CALENDAR, SIGNAL, m)["base_breakout"]
    assert next(row for row in route["rule_results"] if row["rule"] == rule)["status"] == "pass"
    m[field] = bound + direction * 1e-10
    route = evaluate_routes(stock["adjusted_bars"], CALENDAR, SIGNAL, m)["base_breakout"]
    assert next(row for row in route["rule_results"] if row["rule"] == rule)["status"] == "fail"


def test_extreme_old_momentum_never_overrides_new_shape_limits():
    raw = breakout()
    raw[-1].update(open=12.1, high=12.2, low=12.0, close=12.18)
    stock = analyze(raw)
    assert stock["metrics"]["ret5"] > 0.12
    assert stock["metrics"]["ma20_distance"] > 0.10
    assert all(route["status"] != "pass" for route in stock["routes"].values())


def test_recovery_requires_real_rise_pullback_confirmation_and_exports_dates():
    stock = analyze(recovery())
    route = stock["routes"]["pullback_recovery"]
    assert route["status"] == "pass", route["rule_results"]
    assert route["p_date"] == CALENDAR[-5]
    assert route["q_date"] == CALENDAR[-22]
    assert route["pullback_dates"] == CALENDAR[-4:-1]
    assert set(route["pullback_dates"]).isdisjoint(route["rise_end_dates"])
    assert route["pullback_amount_ratio"] == pytest.approx(0.4)
    assert route["confirmation_amount_ratio"] == pytest.approx(1.875)
    raw = recovery()
    raw[-1].update(close=10.9, open=10.87, high=11.0)
    route = analyze(raw)["routes"]["pullback_recovery"]
    assert route["status"] == "fail"
    assert route["process_state"] == "awaiting_confirmation"
    assert analyze(bars())["routes"]["pullback_recovery"]["status"] == "fail"


def test_recovery_increasing_amount_or_broken_fixed_ma_cannot_be_called_washout():
    raw = recovery()
    for row in raw[-4:-1]:
        row["amount_cny"] = 210_000_000
    route = analyze(raw)["routes"]["pullback_recovery"]
    assert (
        next(rule for rule in route["rule_results"] if rule["rule"] == "pullback_contraction")[
            "status"
        ]
        == "fail"
    )
    raw = recovery()
    raw[-2].update(low=10.1, close=10.2, open=10.3)
    route = analyze(raw)["routes"]["pullback_recovery"]
    assert (
        next(rule for rule in route["rule_results"] if rule["rule"] == "pullback_structure")[
            "status"
        ]
        == "fail"
    )


def test_duplicates_invalid_values_zero_range_and_listing_use_explicit_states():
    raw = breakout()
    duplicate = deepcopy(raw[-2])
    duplicate["amount_cny"] = 1
    stock = analyze([*raw, duplicate])
    assert stock["history"]["errors"][duplicate["date"]] == "conflicting_duplicate"
    raw = bars()
    raw[-1].update(open=10, high=10, low=10, close=10)
    metrics = analyze(raw)["metrics"]
    assert metrics["one_price_bar"] is True
    assert metrics["close_location"] is None and metrics["upper_shadow"] is None
    sec = security()
    sec["list_date"] = CALENDAR[-119]
    stock = analyze(sec=sec)
    assert "listing_age_fail" in stock["universe"]["rejection_reasons"]
    sec["list_date"] = CALENDAR[-120]
    assert analyze(sec=sec)["universe"]["listing_age_sessions"] == 120
    raw[-1]["adj_factor"] = float("nan")
    assert normalize_bars(raw, CALENDAR, SIGNAL)["history_status"] == "unknown"


def market_stock(code, growth=0.01, amount=10_000_000):
    raw = bars()
    for index, row in enumerate(raw):
        close = 10 * (1 + growth) ** index
        row.update(
            open=close * 0.999,
            high=close * 1.002,
            low=close * 0.998,
            close=close,
            amount_cny=amount,
            up_limit=close * 1.10,
            down_limit=close * 0.90,
        )
    return analyze(raw, code)


def test_market_denominator_does_not_use_candidate_liquidity_gate_or_hide_missing():
    stocks = [market_stock(f"600{index:03}.SH") for index in range(20)]
    assert all(stock["universe"]["candidate_status"] == "fail" for stock in stocks)
    market = assess_market(stocks, CALENDAR, SIGNAL)
    assert market["state"] == "active"
    assert market["metrics"]["valid_count"] == 20
    stocks[0]["adjusted_bars"] = []
    stocks[1]["adjusted_bars"] = []
    market = assess_market(stocks, CALENDAR, SIGNAL)
    assert market["metrics"]["expected_count"] == 20
    assert market["metrics"]["coverage"] == 0.9
    assert market["state"] == "unknown"
    assert market["empty_reason"] == "data_insufficient"


def test_market_risk_priority_and_unknown_limit_are_not_false():
    stocks = [market_stock(f"600{index:03}.SH", growth=-0.02) for index in range(20)]
    for stock in stocks:
        stock["adjusted_bars"][-1]["down_limit"] = None
    market = assess_market(stocks, CALENDAR, SIGNAL)
    assert market["risk_flags"]["R3"] is None
    assert market["state"] == "risk_off"
    assert market["priority_cap"] == 0
    assert market["empty_reason"] == "strategy_pauses_priority"
    stocks = [market_stock(f"600{index:03}.SH") for index in range(20)]
    stocks[0]["adjusted_bars"][-1]["down_limit"] = None
    assert assess_market(stocks, CALENDAR, SIGNAL)["state"] == "unknown"
    stocks[0]["adjusted_bars"][-1]["down_limit"] = stocks[0]["adjusted_bars"][-1]["raw_close"] * 0.9
    stocks[0]["adjusted_bars"][-1]["up_limit"] = None
    market = assess_market(stocks, CALENDAR, SIGNAL)
    assert market["state"] == "active"
    assert market["metrics"]["up_limit_ratio"] is None
    assert market["metrics"]["down_limit_ratio"] == 0


def test_market_each_window_uses_95_percent_expected_coverage_not_100_percent():
    stocks = [market_stock(f"600{index:03}.SH") for index in range(20)]
    for stock in stocks[:1]:
        stock["adjusted_bars"] = [
            row for row in stock["adjusted_bars"] if row["date"] != CALENDAR[-4]
        ]
    result = assess_market(stocks, CALENDAR, SIGNAL)["metrics"]
    assert result["coverage"] == 1
    assert result["window_coverage"]["ret3"]["expected_count"] == 20
    assert result["window_coverage"]["ret3"]["valid_count"] == 19
    assert result["window_coverage"]["ret3"]["coverage"] == 0.95
    assert result["window_coverage"]["ret3"]["status"] == "complete"
    assert result["median_ret3"] == pytest.approx(1.01**3 - 1)
    assert result["above_ma20_ratio"] == 1  # Missing is not an implicit below-MA stock.
    assert result["window_coverage"]["above_ma20"]["valid_count"] == 19
    stocks[1]["adjusted_bars"] = [
        row for row in stocks[1]["adjusted_bars"] if row["date"] != CALENDAR[-4]
    ]
    result = assess_market(stocks, CALENDAR, SIGNAL)["metrics"]
    assert result["coverage"] == 1
    assert result["window_coverage"]["ret3"]["coverage"] == 0.9
    assert result["median_ret3"] is None
    assert result["above_ma20_ratio"] is None


def test_market_ret3_coverage_denominator_includes_missing_expected_market_stock():
    stocks = [market_stock(f"600{index:03}.SH") for index in range(100)]
    for stock in stocks[:5]:
        stock["adjusted_bars"] = []
    stocks[5]["adjusted_bars"] = [
        row for row in stocks[5]["adjusted_bars"] if row["date"] != CALENDAR[-4]
    ]
    result = assess_market(stocks, CALENDAR, SIGNAL)["metrics"]
    assert result["coverage"] == 0.95
    assert result["window_coverage"]["ret3"]["expected_count"] == 100
    assert result["window_coverage"]["ret3"]["valid_count"] == 94
    assert result["window_coverage"]["ret3"]["coverage"] == 0.94
    assert result["median_ret3"] is None


def test_market_window_fix_does_not_fill_missing_historical_security_status():
    stocks = [market_stock(f"600{index:03}.SH") for index in range(20)]
    for stock in stocks:
        stock["security"]["status_by_date"] = {}
    result = assess_market(stocks, CALENDAR, SIGNAL)
    assert result["metrics"]["window_coverage"]["ret3"]["coverage"] == 1
    assert result["metrics"]["median_ret3"] is not None
    assert result["risk_flags"]["R4"] is None
    assert result["state"] == "unknown"
    assert all(row["core_status"] == "unknown" for row in result["daily_metrics"][:-1])


def group_fixture():
    stocks = {
        f"600{index:03}.SH": market_stock(
            f"600{index:03}.SH", growth=(0.01 if index < 10 else -0.005)
        )
        for index in range(20)
    }
    snapshots = []
    for day in CALENDAR[-6:]:
        for group_id, members in (
            ("industry:strong", list(stocks)[:10]),
            ("industry:weak", list(stocks)[10:]),
            ("theme:strong", list(stocks)[:10]),
            ("theme:weak", list(stocks)[10:]),
        ):
            snapshots.append(
                {
                    "group_id": group_id,
                    "group_type": group_id.split(":")[0],
                    "members": members,
                    "snapshot_date": day,
                    "known_at": f"{day}T20:00:00+08:00",
                    "complete": True,
                    "source": "synthetic-actual-dated-snapshot",
                }
            )
    asof = f"{SIGNAL}T21:00:00+08:00"
    return stocks, snapshots, asof


def test_groups_separate_types_persistence_recomputes_ranks_from_dated_snapshots():
    stocks, snapshots, asof = group_fixture()
    groups = assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof)
    for group in groups:
        if group["group_id"].endswith("strong"):
            assert group["strength"] == pytest.approx(5 / 6)
            assert group["persistence"] == 5
            assert group["status"] == "established"
        else:
            assert group["status"] == "paused"
    assert {group["group_type"] for group in groups} == {"industry", "theme"}


def test_newly_downloaded_history_and_postcutoff_members_never_forge_persistence():
    stocks, snapshots, asof = group_fixture()
    for row in snapshots:
        row["known_at"] = f"{SIGNAL}T20:00:00+08:00"
    groups = assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof)
    assert all(group["persistence"] is None for group in groups)
    before = deepcopy(groups)
    snapshots.append(
        {
            "group_id": "industry:strong",
            "group_type": "industry",
            "members": list(stocks)[10:],
            "snapshot_date": SIGNAL,
            "known_at": f"{SIGNAL}T22:00:00+08:00",
            "complete": True,
            "source": "future-members",
        }
    )
    assert assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof) == before
    snapshots.append(
        {
            "group_id": "theme:future_group",
            "group_type": "theme",
            "members": list(stocks)[:10],
            "snapshot_date": SIGNAL,
            "known_at": f"{SIGNAL}T22:00:00+08:00",
            "complete": True,
            "source": "future-catalogue",
        }
    )
    assert assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof) == before


def test_conflicting_snapshot_and_confirmed_suspension_preserve_denominators():
    stocks, snapshots, asof = group_fixture()
    stocks["600000.SH"]["security"]["suspended_by_date"][SIGNAL] = True
    groups = assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof)
    group = next(row for row in groups if row["group_id"] == "industry:strong")
    assert group["metrics"]["expected_count"] == 9
    assert group["metrics"]["valid_count"] == 9
    assert group["status"] == "partial"
    conflict = deepcopy(
        next(
            row
            for row in snapshots
            if row["group_id"] == "industry:weak" and row["snapshot_date"] == SIGNAL
        )
    )
    conflict["members"] = list(stocks)[:10]
    snapshots.append(conflict)
    groups = assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof)
    group = next(row for row in groups if row["group_id"] == "industry:weak")
    assert group["coverage_status"] == "membership_incomplete"
    assert group["reason_code"] == "conflicting_membership_snapshot"
    reversed_groups = assess_groups(list(reversed(snapshots)), stocks, CALENDAR, SIGNAL, asof)
    assert reversed_groups == groups


def test_single_limit_up_with_group_declines_does_not_create_mainline():
    stocks, snapshots, asof = group_fixture()
    codes = list(stocks)[:10]
    for code in codes[1:]:
        stocks[code] = market_stock(code, growth=-0.005)
    stock = stocks[codes[0]]
    row = stock["adjusted_bars"][-1]
    row["up_limit"] = row["raw_close"]
    groups = assess_groups(snapshots, stocks, CALENDAR, SIGNAL, asof)
    group = next(group for group in groups if group["group_id"] == "industry:strong")
    assert group["metrics"]["breadth"] == 0.1
    assert group["metrics"]["limit_count"] == 1
    assert group["status"] == "paused"


def budget_candidate(code, group_ids, route, eligibility="priority_candidate", position=0.01):
    return {
        "ts_code": code,
        "group_ids": group_ids,
        "eligibility_ceiling": eligibility,
        "routes": {
            route: {"status": "pass", "breakout_distance": position, "pullback_amount_ratio": 0.5}
        },
        "metrics": {
            "upper_shadow": 0.1,
            "amount_ratio": 1.6,
            "median_amount20": 200_000_000,
            "close_location": 0.8,
            "ma20_distance": 0.02,
        },
    }


def test_budget_balances_both_stages_routes_and_prioritizes_eligibility_globally():
    groups = [
        {
            "group_id": "G1",
            "group_type": "theme",
            "status": "established",
            "strength": 1,
            "persistence": 5,
        },
        {
            "group_id": "G2",
            "group_type": "industry",
            "status": "established",
            "strength": 0.5,
            "persistence": 3,
        },
    ]
    candidates = [
        budget_candidate("600001.SH", ["G1"], "base_breakout", "watch", position=0),
        budget_candidate("600002.SH", ["G1"], "base_breakout"),
        budget_candidate("600003.SH", ["G1"], "pullback_recovery"),
        budget_candidate("600004.SH", ["G2"], "base_breakout"),
        budget_candidate("600005.SH", ["G2"], "pullback_recovery"),
        budget_candidate("600006.SH", ["G1"], "base_breakout", "excluded"),
    ]
    result = allocate_budget(candidates, groups, context_limit=5, deep_limit=4)
    assert [row["ts_code"] for row in result["deep"]] == [
        "600002.SH",
        "600003.SH",
        "600004.SH",
        "600005.SH",
    ]
    assert result["context"][-1]["ts_code"] == "600001.SH"
    assert result["excluded"] == ["600006.SH"]
    assert (
        allocate_budget(
            list(reversed(candidates)), list(reversed(groups)), context_limit=5, deep_limit=4
        )
        == result
    )


def test_budget_deduplicates_stock_preserving_all_groups_and_routes():
    groups = [
        {"group_id": "A", "group_type": "theme", "status": "established"},
        {"group_id": "B", "group_type": "theme", "status": "emerging"},
    ]
    first = budget_candidate("600001.SH", ["A", "B"], "base_breakout")
    first["routes"]["pullback_recovery"] = {"status": "pass", "pullback_amount_ratio": 0.6}
    result = allocate_budget([first, deepcopy(first)], groups, context_limit=2, deep_limit=2)
    assert len(result["deep"]) == 1
    assert result["deep"][0]["group_ids"] == ["A", "B"]
    assert set(result["deep"][0]["routes"]) == {"base_breakout", "pullback_recovery"}


def test_shuffled_bars_keep_metrics_and_routes_identical():
    stock = analyze()
    shuffled = analyze(list(reversed(breakout())))
    assert stock == shuffled
    metrics = compute_metrics(stock["adjusted_bars"], CALENDAR, SIGNAL)
    assert metrics["atr14"] == pytest.approx((13 * 0.1 + 0.23) / 14)
    assert (
        datetime.fromisoformat(f"{SIGNAL}T21:00:00+08:00").astimezone(SH).date().isoformat()
        == SIGNAL
    )
