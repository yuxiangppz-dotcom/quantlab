"""Compact evidence is loss-aware, not another eligibility or price calculation."""

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from quantlab.scout.article_input import compact_group, compact_program


def event(identifier, *, historical=False, action="pass"):
    return {
        "event_id": identifier,
        "ts_code": "600001.SH",
        "report_date": "2026-10-08",
        "scope_type": "single_day",
        "scope_start": "2026-10-08",
        "scope_end": "2026-10-08",
        "effective_target_date": "2026-10-09",
        "active_for_target": not historical,
        "historical_evidence": historical,
        "net_cny": -30_000_000,
        "net_ratio": -0.2,
        "institution_rank_net_cny": -20_000_000,
        "institution_rank_ratio": -0.15,
        "institution_status": "disclosed",
        "action": action,
        "reason_codes": ["lhb_institution_rank_side_severe_sell"] if action == "exclude" else [],
        "source_ids": [f"source-{identifier}"],
    }


def level(identifier, lower, upper, *, role="support", source="swing_low", **kwargs):
    return {
        "level_id": identifier,
        "lower": lower,
        "upper": upper,
        "center": (lower + upper) / 2,
        "current_role": role,
        "initial_role": role,
        "status": "active",
        "significant": True,
        "source_types": [source],
        "formed_at": "2026-09-01",
        "confirmed_at": "2026-09-03",
        "band_frozen_at": "2026-10-08",
        "historical_tests_use_frozen_band": True,
        "origins": [{"source_type": source, "confirmed_at": "2026-09-03", "price": lower}],
        "touch_events": [{"date": "2026-10-06"}] * 100,
        **kwargs,
    }


def program():
    return {
        "setup": {
            "route": "base_breakout",
            "status": "pass",
            "B": 10,
            "F": 9,
            "rule_results": [{"rule": "amount", "status": "pass", "value": [1] * 1000}],
        },
        "metrics": {
            "close": 10.2,
            "ma20": 9.8,
            "ret1": 0.02,
            "ret3": 0.04,
            "ret5": 0.05,
            "median_amount20": 150_000_000,
            "amount_ratio": 1.5,
        },
        "moneyflow": {
            "status": "complete",
            "action": "watch_only",
            "state": "opposing",
            "negative_days_5": 3,
            "source_ids": ["flow-source"],
            "observations": [{"net_cny": 99}] * 1000,
            "windows": {
                str(n): {
                    "status": "complete",
                    "net_cny": -n * 1_000_000,
                    "amount_cny": n * 100_000_000,
                    "flow_ratio": -0.01,
                    "large_order_difference_cny": -n * 2_000_000,
                    "large_order_ratio": -0.02,
                }
                for n in (1, 3, 5)
            },
        },
        "lhb": {
            "status": "known",
            "action": "exclude",
            "events": [event("serious", action="exclude")],
            "no_cross_event_netting": True,
        },
        "levels": {
            "status": "complete",
            "atr14": 0.3,
            "dynamic_references": {"MA20": 9.8},
            "levels": [
                level("below", 9.5, 9.6),
                level("straddles", 10.1, 10.3),
                level("chosen", 10.8, 10.9, role="resistance", source="box_upper"),
                level("far", 20, 21, role="resistance"),
            ]
            + [level(f"pivot-{i}", 1, 2, significant=False) for i in range(120)],
        },
        "entry": {
            "action": "watch_only",
            "nearest_resistance_id": "chosen",
            "entry_low": 10.1,
            "entry_high": 10.3,
            "reference": 10.2,
            "invalidation": 9.5,
            "entry_check": "pending",
        },
        "unlock": {"action": "pass", "status": "complete", "ratio_total_shares": 0.0},
        "notices": [],
        "risk_body_pending": False,
        "adjusted_bars": [{"close": 1}] * 120,
        "local_path": "C:/private/data.json",
    }


def test_compaction_does_not_mutate_or_promote_actions_or_recalculate_ratios():
    original = program()
    before = deepcopy(original)
    result = compact_program(original)
    assert original == before
    assert result["lhb"]["action"] == "exclude"
    assert result["moneyflow"]["action"] == "watch_only"
    assert result["moneyflow"]["windows"]["3"]["flow_ratio"] == -0.01
    assert result["moneyflow"]["windows"]["5"]["large_order_difference_cny"] == -10_000_000
    assert result["moneyflow"]["negative_days_5"] == 3
    assert "observations" not in result["moneyflow"]
    assert "adjusted_bars" not in result
    assert result["setup"]["passed_rules"] == ["amount"]
    assert len(json.dumps(result)) < len(json.dumps(original)) / 5


def test_all_active_serious_events_retained_separately_despite_history_cap():
    original = program()
    active = [event(f"active-{i}", action="exclude") for i in range(5)]
    history = [event(f"old-{i}", historical=True) for i in range(10)]
    unresolved = {
        **event("unresolved"),
        "active_for_target": False,
        "historical_evidence": False,
        "institution_status": "unknown",
        "institution_rank_net_cny": None,
    }
    original["lhb"]["events"] = active + history + [unresolved]
    result = compact_program(original)["lhb"]
    by_id = {row["event_id"]: row for row in result["events"]}
    for source in active:
        assert by_id[source["event_id"]] == source
    assert by_id["unresolved"]["institution_rank_net_cny"] is None
    assert len(result["events"]) == 8
    assert result["historical_events_omitted"] == 8
    assert result["eligibility_action_preserved"] == "exclude"
    assert "net_cny" not in result  # Independent periods never netted.


def test_levels_select_support_below_actual_close_preserve_chosen_pressure_and_freeze():
    result = compact_program(program())["levels"]
    assert result["closest_support"]["level_id"] == "below"
    assert result["closest_support"]["upper"] <= 10.2
    assert result["nearest_significant_pressure"]["level_id"] == "chosen"
    assert result["nearest_significant_pressure"]["band_frozen_at"] == "2026-10-08"
    assert result["nearest_significant_pressure"]["confirmed_at"] == "2026-09-03"
    assert result["route_key_levels"][0]["level_id"] == "chosen"
    assert "levels" not in result
    assert result["other_levels_omitted"] == 122


def test_missing_close_or_selected_band_does_not_infer_favorable_replacement():
    original = program()
    del original["metrics"]["close"]
    original["entry"]["nearest_resistance_id"] = "missing"
    result = compact_program(original)["levels"]
    assert result["closest_support"] is None
    assert result["closest_support_status"] == "unknown_close"
    assert result["nearest_resistance_id"] == "missing"
    assert result["nearest_significant_pressure"] is None
    assert result["nearest_pressure_status"] == "unknown_missing_selected_band"


def test_notices_total_body_cap_marks_partial_review_and_preserves_stricter_action():
    original = program()
    original["notices"] = [
        {"title": f"notice{i}", "body": "公告正文" * 1000, "source_id": f"notice-{i}"}
        for i in range(3)
    ]
    result = compact_program(original)
    notices = result["notices"]
    assert len(notices["items"]) == 2
    assert sum(len(row["body"]) for row in notices["items"]) == 500
    assert notices["omitted_count"] == 1
    assert notices["truncated_body_count"] == 2
    assert notices["status"] == "partial"
    assert notices["items"][0]["source_id"] == "notice-0"
    assert result["risk_body_pending"] is True
    assert result["ai_input_ceiling"] == "watch_only"
    assert result["lhb"]["action"] == "exclude"


def test_empty_unknowns_stay_unknown_and_paths_removed_in_nested_text_and_sources():
    original = program()
    original["moneyflow"] = {
        "status": "unknown",
        "action": "review_required",
        "windows": {"5": {"net_cny": None, "flow_ratio": None}},
    }
    original["notices"] = [
        {
            "body": "文件 C:/private/data.csv unavailable",
            "source_id": "real-id",
            "source": {"snapshot_path": "/home/private", "provider": "official"},
        }
    ]
    result = compact_program(original)
    serialized = json.dumps(result, ensure_ascii=False)
    assert (
        "C:/" not in serialized and "/home/" not in serialized and "snapshot_path" not in serialized
    )
    assert result["notices"]["items"][0]["source_id"] == "real-id"
    assert result["moneyflow"]["windows"]["5"]["net_cny"] is None
    assert result["moneyflow"]["status"] == "unknown"
    assert result["moneyflow"]["action"] == "review_required"


def test_explicit_unknown_notice_coverage_and_titles_only_cannot_promote():
    original = program()
    original["notices"] = {"status": "unknown", "items": []}
    result = compact_program(original)
    assert result["notices"]["status"] == "unknown"
    assert result["risk_body_pending"] is True
    assert result["ai_input_ceiling"] == "watch_only"
    original["notices"] = [{"title": "业务进展", "source_id": "title-only"}]
    result = compact_program(original)
    assert result["notices"]["missing_body_count"] == 1
    assert result["notices"]["status"] == "partial"
    assert result["risk_body_pending"] is True


def test_third_notice_serious_counter_and_subject_period_source_survive_body_omission():
    original = program()
    counter = {
        "action": "exclude",
        "confirmed_severity": True,
        "scope": {"ts_code": "600001.SH"},
        "effective_period": {"start": "2026-10-09", "end": "2026-10-09"},
        "source_id": "verified-qualification",
        "reason_code": "trade_restricted",
    }
    original["notices"] = [
        {"body": "正文"},
        {"body": "另一正文"},
        {
            "ts_code": "600001.SH",
            "title": "限制交易",
            "body": "禁止",
            "risk_flags": [counter],
            "_source_id": "official-3",
            "_published_at": "2026-10-08T20:00:00+08:00",
        },
    ]
    notices = compact_program(original)["notices"]
    assert notices["omitted_notice_counter_evidence"][0]["risk_flags"] == [counter]
    assert notices["omitted_notice_counter_evidence"][0]["_source_id"] == "official-3"
    assert (
        notices["omitted_notice_counter_evidence"][0]["_published_at"]
        == "2026-10-08T20:00:00+08:00"
    )
    assert notices["status"] == "partial"


def test_group_keeps_strength_pit_persistence_unknown_and_drops_members_daily_snapshots():
    group = {
        "group_id": "industry:bank",
        "group_type": "industry",
        "group_state": "emerging",
        "coverage_status": "partial",
        "strength": None,
        "persistence": None,
        "share_expansion": 1.2,
        "membership_source": "dated-real-id",
        "snapshot_at": "2026-10-08T20:00:00+08:00",
        "members": ["600001.SH"] * 200,
        "snapshot": {"local_path": "C:/data"},
        "daily_history": [{}] * 120,
        "metrics": {
            "coverage": 0.8,
            "breadth": 0.65,
            "excess3": 0.03,
            "amount_share": 0.1,
            "limit_density": None,
        },
    }
    result = compact_group(group)
    assert result["group_state"] == "emerging" and result["persistence"] is None
    assert result["metrics"]["limit_density"] is None
    assert result["share_expansion"] == 1.2
    assert result["snapshot_at"] == group["snapshot_at"]
    assert not {"members", "daily_history", "snapshot"} & result.keys()


def test_synthetic_24_stock_pressure_budget_without_suppressing_serious_counter():
    samples = [program() for _ in range(24)]
    for sample in samples:
        sample["notices"] = [
            {"source_id": "notice1", "body": "真实来源格式的测试正文" * 500},
            {"source_id": "notice2", "body": "另一条待审查风险反证" * 500},
        ]
    payload = {
        "programs": [compact_program(row) for row in samples],
        "deep": [{"ts_code": f"600{i:03}.SH", "fact_ids": [f"fact-{i}"]} for i in range(24)],
    }
    # Match actual ArticleAI.preflight serialization, including normal whitespace.
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) <= 120_000
    assert all(row["lhb"]["events"][0]["action"] == "exclude" for row in payload["programs"])


@pytest.mark.skipif(
    not os.environ.get("SCOUT_ARTICLE_REAL_REPLAY"), reason="opt-in saved-real-data replay"
)
def test_saved_real_24_stock_program_replay_budget():
    """Read already saved programs only; never download data or alter run artifacts."""
    path = Path(os.environ["SCOUT_ARTICLE_REAL_REPLAY"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    rows = saved["programs"]
    assert len(rows) == 24
    compact = [compact_program(row) for row in rows]
    assert len(json.dumps({"programs": compact}, ensure_ascii=False)) <= 120_000
    for original, reduced in zip(rows, compact, strict=True):
        serious = [
            row
            for row in original.get("lhb", {}).get("events", [])
            if row.get("active_for_target") and row.get("action") == "exclude"
        ]
        remaining = {row["event_id"]: row for row in reduced["lhb"]["events"]}
        for event_row in serious:
            assert remaining[event_row["event_id"]]["net_cny"] == event_row["net_cny"]
            assert remaining[event_row["event_id"]]["action"] == "exclude"


@pytest.mark.skipif(
    not os.environ.get("SCOUT_ARTICLE_REAL_ROOT"), reason="opt-in read-only real daily sample"
)
def test_real_stock_daily_evidence_budget():
    """Actual N≤24 saved passing shapes; group/risk unknown, not formal candidates."""
    from quantlab.scout.article_data import load_market
    from quantlab.scout.article_engine import compute_metrics, evaluate_routes, normalize_bars
    from quantlab.scout.article_levels import compute_price_levels, freeze_entry_reference
    from quantlab.scout.article_risks import evaluate_moneyflow, normalize_lhb_events

    market = load_market(Path(os.environ["SCOUT_ARTICLE_REAL_ROOT"]), signal_date="2026-09-30")
    signal = str(market["signal_date"])
    sessions = [str(value) for value in market["sessions"]]
    samples = []
    for code, raw in sorted(market["bars_by_code"].items()):
        normalized = normalize_bars(raw, sessions, signal)
        if normalized["history_status"] != "complete":
            continue
        bars = normalized["rows"]
        metrics = {**compute_metrics(bars, sessions, signal), "close": bars[-1]["close"]}
        # Dedicated deep-research inputs contain only program-passing shapes;
        # group and risk eligibility remain unknown, so these are not recommendations.
        setups = [
            row
            for row in evaluate_routes(bars, sessions, signal, metrics).values()
            if row["status"] == "pass"
        ]
        if not setups:
            continue
        setup = setups[0]
        levels = compute_price_levels(bars, sessions, signal_date=signal, setup=setup)
        entry = freeze_entry_reference(
            bars, levels, signal_date=signal, setup=setup, market_mode="unknown"
        )
        flow = evaluate_moneyflow(
            [],
            bars,
            sessions,
            signal_date=signal,
            setup=setup,
            source_status="unknown",
            units_verified=False,
        )
        lhb = normalize_lhb_events(
            [],
            [],
            target_date="2026-10-08",
            cutoff_at="2026-09-30T23:00:00+08:00",
            source_status="unknown",
        )
        samples.append(
            {
                "ts_code": code,
                "metrics": metrics,
                "setup": setup,
                "levels": levels,
                "entry": entry,
                "moneyflow": flow,
                "lhb": lhb,
                "notices": {"status": "unknown", "items": []},
                "risk_body_pending": True,
            }
        )
        if len(samples) == 24:
            break
    assert 0 < len(samples) <= 24
    compact = [compact_program(row) for row in samples]
    text = json.dumps({"programs": compact}, ensure_ascii=False, allow_nan=False)
    assert len(text) <= 120_000
    assert all(row["moneyflow"]["status"] == "unknown" for row in compact)
    assert all(row["lhb"]["status"] == "unknown" for row in compact)
    assert all(row["ai_input_ceiling"] == "watch_only" for row in compact)
    print(
        f"Read-only 2026-09-30 real daily sample:{len(samples)} A/B-passing shapes; "
        f"compact text:{len(text)} chars; group and risk sources unknown; not formal candidates."
    )
