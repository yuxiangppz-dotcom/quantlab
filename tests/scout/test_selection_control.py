from copy import deepcopy
from types import SimpleNamespace

import pytest

from quantlab.scout.selection_control import freeze_control, observe_control, summarize_control


def case(status="focus"):
    rows = [
        {
            "instrument_id": code,
            "rank": rank,
            "final_status": state,
            "primary_type": "trend_continuation",
        }
        for code, rank, state in (("a", 1, status), ("b", 2, "unselected"), ("c", 3, "unselected"))
    ]
    universe = {
        code: SimpleNamespace(score=score, metrics={"return_5d": 0.3 if code == "a" else 0.01})
        for code, score in (("a", 0.2), ("b", 0.8), ("c", 0.8))
    }
    candidates = [{"instrument_id": c} for c in universe]
    return rows, universe, candidates


def observed(control):
    return [
        {
            "instrument_id": r["instrument_id"],
            "status": "observed",
            "horizon_sessions": 5,
            "adjusted_price_return": value,
            "adverse_daily_low_change": -0.05,
            "d_open_at_upper_limit": True,
            "actual_execution": "unknown",
        }
        for r, value in zip(control["rows"], [0.1, -0.1, 0.2], strict=True)
    ]


def test_same_pool_ai_score_k_and_ties_are_frozen_without_reselection():
    rows, universe, candidates = case()
    control = freeze_control(rows, universe, candidates, [])
    assert control["k"] == 1 and control["score_order"] == ["b", "c", "a"]
    assert control["rows"][0]["recent_extension"] == "extended_5d_gt20pct"
    result = observe_control(control, observed(control))
    assert result["groups"]["ai_selected"]["ids"] == ["a"]
    assert result["groups"]["score_top_k"]["ids"] == ["b"]
    assert result["ai_minus_score_price_change"] == pytest.approx(0.2)
    assert result["groups"]["ai_selected"]["relative_to_common_pool"] == pytest.approx(
        0.1 - 0.2 / 3
    )
    assert result["groups"]["ai_selected"]["actual_execution"] == "unknown_daily_ohlc_is_not_fill"
    rows.reverse()
    assert freeze_control(rows, universe, list(reversed(candidates)), []) == control


def test_abstention_day_kept_and_missing_prices_do_not_get_zero_or_replacement():
    rows, universe, candidates = case("unselected")
    control = freeze_control(rows, universe, candidates, [])
    result = observe_control(control, observed(control))
    assert result["abstention"] and result["k"] == 0
    assert result["ai_minus_score_price_change"] is None
    marks = observed(control)
    marks[0].update(status="bar_missing_or_halt_unknown", adjusted_price_return=None)
    result = observe_control(control, marks)
    assert result["groups"]["all_pool"]["mean_price_change"] is None
    assert result["common_pool_mean"] is None
    assert result["groups"]["ai_selected"]["original_count"] == 0


def test_unknown_score_or_partial_pool_cannot_enter_paired_comparison():
    rows, universe, candidates = case()
    universe["b"].score = float("nan")
    control = freeze_control(rows, universe, candidates, [{"source": "news", "status": "failed"}])
    assert control["status"] == "discovery_score_incomplete"
    assert all(r["source_coverage"] == "missing" for r in control["rows"])
    result = observe_control(control, observed(control))
    assert not result["paired_complete"] and result["ai_minus_score_price_change"] is None
    marks = observed(control)
    with pytest.raises(ValueError, match="complete frozen pool"):
        observe_control(control, marks[:-1])
    forged = deepcopy(control)
    forged["k"] = 2
    with pytest.raises(ValueError, match="hash mismatch"):
        observe_control(forged, marks)


def test_uncertainty_by_signal_date_blocks_not_individual_stock_count():
    dates = [
        {
            "target_day": f"2026-10-{i:02d}",
            "paired_complete": True,
            "abstention": False,
            "ai_minus_score_price_change": 0.1 if i <= 5 else -0.1,
        }
        for i in range(1, 11)
    ]
    result = summarize_control(dates)
    assert result["mean_ai_minus_score_price_change"] == pytest.approx(0)
    assert result["complete_block_count"] == 2
    assert result["descriptive_block_bootstrap_interval95"] is None
    dates[0].update(paired_complete=False, abstention=True, ai_minus_score_price_change=None)
    result = summarize_control(dates)
    assert result["abstention_dates"] == 1 and result["complete_block_count"] == 1
    assert result["realizable_net_returns"].startswith("not_measured")
