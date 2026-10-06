"""Research allocation risk scenarios; synthetic sources, no market claims."""

from datetime import date, datetime

from quantlab.scout.models import SHANGHAI, Candidate, Evidence
from quantlab.scout.opportunities import annotate_universe, event_records
from quantlab.scout.pipeline import build_pool

NOW = datetime(2026, 10, 5, 20, tzinfo=SHANGHAI)


def candidate(code, score=0.01, routes=()):
    return Candidate(
        code,
        code,
        {
            "return_1d": 0.0,
            "return_5d": 0.0,
            "return_20d": 0.0,
            "amount_ratio_5d": 1,
            "close_location": 0.5,
        },
        score,
        list(routes),
    )


def direct_events(codes):
    item = Evidence(
        "cninfo:official_pdf_text",
        "重大合同公告",
        "合同条款和付款风险（合成）",
        "https://example.org/synthetic-contract",
        NOW.isoformat(),
        NOW.isoformat(),
        "official_pdf_text_unverified",
        tuple(codes),
    )
    rows, _ = event_records([item], [], date(2026, 9, 30), NOW)
    # A shared official document explicitly lists participating subsidiaries.
    for row in rows:
        row["relation"] = "direct_subject"
    return rows


def test_single_strong_event_can_take_many_seats_without_momentum_or_route_quota():
    codes = [f"60000{i}.SH" for i in range(8)]
    universe = {code: candidate(code) for code in codes}
    events = direct_events(codes)
    annotate_universe(universe, events, {})
    diagnostics = {}
    pool = build_pool(
        universe,
        [],
        [],
        8,
        diagnostics=diagnostics,
        policy="evidence_marginal_v2",
        asof=NOW.isoformat(),
    )
    assert len(pool) == 8
    assert all(row["research_budget"]["strong_direct_event"] for row in pool)
    assert len(diagnostics["opportunity_hypotheses"]) == 1
    assert diagnostics["opportunity_hypotheses"][0]["novelty"] == "unseen_history_unknown"
    assert all(row["metrics"]["return_1d"] == 0 for row in pool)


def test_same_stock_multiroute_counts_once_and_does_not_fill_weak_evidence():
    code, weak = "600001.SH", "600002.SH"
    universe = {code: candidate(code, routes=["趋势突破"]), weak: candidate(weak)}
    annotate_universe(universe, direct_events([code]), {})
    pool = build_pool(
        universe,
        [
            {
                "instrument_ids": [code],
                "relation": "direct",
                "route_type": "event",
                "evidence_ids": ["ev-a"],
            }
        ],
        [code],
        24,
        [code],
        policy="evidence_marginal_v2",
    )
    assert [row["instrument_id"] for row in pool] == [code]
    assert {"event", "sector", "attention", "momentum"} <= set(pool[0]["recall_routes"])


def test_attention_is_bounded_exploration_and_budget_exclusion_is_not_rejection():
    codes = [f"60000{i}.SH" for i in range(8)]
    universe = {code: candidate(code) for code in codes}
    diagnostics = {}
    pool = build_pool(universe, [], [], 8, codes, diagnostics, policy="evidence_marginal_v2")
    assert len(pool) == diagnostics["exploration_used"] == 2
    assert all(not row["research_budget"]["strong_direct_event"] for row in pool)
    assert len(diagnostics["budget_exclusions"]) == 6
    assert all(
        row["reason"] == "exploration_budget_excluded" for row in diagnostics["budget_exclusions"]
    )
    assert len(diagnostics["funnel"]) == 8
    assert all(row["selected"] is None for row in diagnostics["funnel"])


def test_strong_event_budget_exclusion_is_explicit_and_repeated_news_is_not_increment():
    codes = [f"60000{i}.SH" for i in range(6)]
    universe = {code: candidate(code) for code in codes}
    annotate_universe(universe, direct_events(codes), {})
    diagnostics = {}
    pool = build_pool(universe, [], [], 4, diagnostics=diagnostics, policy="evidence_marginal_v2")
    assert len(pool) == 4
    assert all(row["strong_direct_event"] for row in diagnostics["budget_exclusions"])
    assert all(
        row["reason"] == "research_budget_excluded" for row in diagnostics["budget_exclusions"]
    )
    for row in universe.values():
        row.context["opportunity"]["events"][0]["novelty"] = "repeated_content"
    assert build_pool(universe, [], [], 24, policy="evidence_marginal_v2") == []
