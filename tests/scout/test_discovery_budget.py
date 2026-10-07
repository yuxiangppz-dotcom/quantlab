"""Research allocation risk scenarios; synthetic sources, no market claims."""

from datetime import date, datetime

from quantlab.scout.discovery_budget import build_dynamic_pool
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


def source_hypothesis(codes, ref="ev-policy", relation="supply_chain"):
    return {
        "instrument_ids": codes,
        "relation": relation,
        "summary": "合成政策节点可能影响下游需求，需核对公司暴露",
        "counterargument": "公司关联与增量需求尚未验证",
        "evidence_ids": [ref],
    }


def news_source(ref="ev-policy", codes=(), kind="news"):
    return {
        ref: {
            "evidence_id": ref,
            "title": "合成政策进展",
            "body": "合成正文：公布了实施节点，仍须核实覆盖范围和公司的实际业务暴露。",
            "kind": kind,
            "instrument_ids": list(codes),
        }
    }


def momentum_universe():
    return {f"6001{i:02}.SH": candidate(f"6001{i:02}.SH", 0.9, ["量价异动"]) for i in range(24)}


def test_bound_policy_chain_and_theme_get_research_despite_24_momentum_candidates():
    universe = momentum_universe()
    codes = ["600001.SH", "600002.SH", "600003.SH"]
    universe.update({code: candidate(code) for code in codes})
    hypotheses = [
        {**source_hypothesis([code], relation=relation), "route_type": "policy"}
        for code, relation in zip(codes, ["direct", "supply_chain", "theme"], strict=True)
    ]
    diagnostics = {}
    pool = build_dynamic_pool(
        universe, hypotheses, [], 24, diagnostics=diagnostics, hypothesis_sources=news_source()
    )
    assert set(codes) <= {row["instrument_id"] for row in pool}
    for code in codes:
        record = next(row for row in pool if row["instrument_id"] == code)["research_budget"]
        assert record["priority"] == 45
        assert record["facts"]["source_bound_non_sentiment_hypothesis"]
        assert not record["facts"]["official"]
        assert not record["strong_direct_event"]
        assert record["hypothesis_ids"]
    catalog = [
        h
        for h in diagnostics["opportunity_hypotheses"]
        if h["opportunity_type"] == "source_linked_hypothesis"
    ]
    assert len(catalog) == 3
    assert all(not h["official_source"] and h["source_ids"] == ["ev-policy"] for h in catalog)
    assert all(h["verification_state"].startswith("hypothesis_requires") for h in catalog)


def test_bound_hypotheses_exploration_is_capped_without_fixed_route_or_forced_fill():
    universe = momentum_universe()
    codes = [f"6000{i:02}.SH" for i in range(8)]
    universe.update({code: candidate(code) for code in codes})
    hypotheses = [source_hypothesis([code], f"ev-{code}") for code in codes]
    sources = {key: row for code in codes for key, row in news_source(f"ev-{code}").items()}
    diagnostics = {}
    pool = build_dynamic_pool(
        universe, hypotheses, [], 24, diagnostics=diagnostics, hypothesis_sources=sources
    )
    assert len(pool) == 24
    assert diagnostics["exploration_used"] == diagnostics["exploration_cap"] == 6
    assert len(set(codes) & {row["instrument_id"] for row in pool}) == 6
    # No eligible clues means no seats are reserved or fabricated.
    plain = momentum_universe()
    assert len(build_dynamic_pool(plain, [], [], 24)) == 24
    assert build_dynamic_pool({"600001.SH": candidate("600001.SH")}, [], [], 24) == []


def test_same_source_link_shares_overlap_and_deduplicated_catalog_membership():
    codes = ["600001.SH", "600002.SH"]
    universe = {code: candidate(code) for code in codes}
    diagnostics = {}
    pool = build_dynamic_pool(
        universe,
        [source_hypothesis([code]) for code in codes],
        [],
        8,
        diagnostics=diagnostics,
        hypothesis_sources=news_source(),
    )
    assert len(diagnostics["opportunity_hypotheses"]) == 1
    assert diagnostics["opportunity_hypotheses"][0]["instrument_ids"] == codes
    assert [row["research_budget"]["marginal_priority_at_allocation"] for row in pool] == [45, 44]


def test_distinct_same_source_hypotheses_survive_catalog_without_evading_overlap():
    codes = ["600001.SH", "600002.SH"]
    hypotheses = [
        {**source_hypothesis([codes[0]]), "summary": "合成政策实施可能关联下游设备需求"},
        {**source_hypothesis([codes[1]]), "summary": "合成政策实施可能关联上游材料需求"},
    ]
    diagnostics = {}
    pool = build_dynamic_pool(
        {code: candidate(code) for code in codes},
        hypotheses,
        [],
        8,
        diagnostics=diagnostics,
        hypothesis_sources=news_source(),
    )
    catalog = diagnostics["opportunity_hypotheses"]
    assert {h["support"] for h in catalog} == {h["summary"] for h in hypotheses}
    assert len(catalog) == 2
    assert len({h["allocation_overlap_group_id"] for h in catalog}) == 1
    assert [row["research_budget"]["marginal_priority_at_allocation"] for row in pool] == [45, 44]


def test_unknown_comment_user_title_and_sentiment_refs_cannot_promote_priority():
    cases = [
        ({}, "supply_chain"),
        (news_source(kind="public_comment_unverified"), "theme"),
        (news_source(kind="unverified_user_clue"), "direct"),
        (news_source(kind="official_announcement_index_unverified"), "direct"),
        (news_source(), "sentiment"),
        ({"ev-policy": {**news_source()["ev-policy"], "body": "合成政策进展"}}, "theme"),
    ]
    for sources, relation in cases:
        universe = momentum_universe()
        code = "600001.SH"
        universe[code] = candidate(code)
        diagnostics = {}
        pool = build_dynamic_pool(
            universe,
            [source_hypothesis([code], relation=relation)],
            [],
            24,
            diagnostics=diagnostics,
            hypothesis_sources=sources,
        )
        assert code not in {row["instrument_id"] for row in pool}
        linked = next(
            h
            for h in diagnostics["opportunity_hypotheses"]
            if h["opportunity_type"] == "source_linked_hypothesis"
        )
        assert not linked["source_bound_research_priority"]


def test_issuer_specific_source_cannot_promote_another_issuer():
    own, other = "600001.SH", "600002.SH"
    universe = {code: candidate(code) for code in (own, other)}
    diagnostics = {}
    pool = build_dynamic_pool(
        universe,
        [source_hypothesis([own, other])],
        [],
        8,
        diagnostics=diagnostics,
        hypothesis_sources=news_source(codes=[own]),
    )
    by_code = {row["instrument_id"]: row["research_budget"] for row in pool}
    assert by_code[own]["priority"] == 45
    assert by_code[other]["priority"] == 0
    assert diagnostics["opportunity_hypotheses"][0]["research_priority_instrument_ids"] == [own]
