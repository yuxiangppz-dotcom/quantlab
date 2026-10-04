"""Synthetic contracts and hand-checkable observations, never investment evidence."""

import json
from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest

from quantlab.data.storage import ParquetStorage
from quantlab.scout.facts import program_facts
from quantlab.scout.models import SHANGHAI, Candidate, Evidence, fingerprint
from quantlab.scout.opportunities import (
    annotate_universe,
    append_event_snapshot,
    event_records,
    historical_limit_context,
    load_event_history,
    model_record,
    nominal_contract_scale,
    price_reactions,
)
from quantlab.scout.opportunity_ai import validate_comparisons
from quantlab.scout.opportunity_demo import make_opportunity_example, synthetic_comparisons
from quantlab.scout.pipeline import balanced_evidence_packet, build_pool, read_config
from quantlab.scout.ranking_tracking import DEFINITION as RANK_DEFINITION
from quantlab.scout.ranking_tracking import observe_ranking, summarize_ranking

AT = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI)
SESSION = date(2026, 9, 30)


def source(**updates):
    values = dict(
        source="synthetic",
        title="重大订单公告",
        body="正式订单签订，盈利影响待确认",
        url="https://example.org/order",
        published_at="2026-09-30T19:00:00+08:00",
        retrieved_at="2026-10-01T10:00:00+08:00",
        kind="company_event_date_only",
        instrument_ids=("600001.SH",),
        event_dates=("2026-09-30",),
    )
    return Evidence(**(values | updates))


def candidate(code, score=0.5, **metrics):
    base = {
        "return_1d": 0.01,
        "return_5d": 0.03,
        "return_20d": 0.1,
        "prior_return_19d": 0.08,
        "previous_return_1d": -0.02,
        "amount_ratio_5d": 1.5,
        "amount_cny": 200000000.0,
        "close_location": 0.8,
        "close": 10,
        "up_limit": 11,
        "breakout_20d": 0.02,
        "one_price_session": False,
    }
    return Candidate(code, f"合成{code}", base | metrics, score, routes=["趋势突破"])


def test_five_syndications_are_one_event_not_five_catalysts():
    items = [
        source(
            source=f"provider{i}",
            title="转载：重大订单公告" if i else "重大订单公告",
            url=f"https://example.org/{i}",
        )
        for i in range(5)
    ]
    rows, excluded = event_records(items, [], SESSION, AT)
    assert not excluded and len(rows) == 1
    assert len(rows[0]["source_ids"]) == 5
    assert rows[0]["novelty"] == "unseen_history_unknown"


def test_material_looking_update_keeps_parent_and_original_snapshot(tmp_path):
    original, _ = event_records(
        [
            source(
                body=json.dumps({"event_identity": "contract-X", "content": "框架意向，尚未订单"})
            )
        ],
        [],
        SESSION,
        AT,
    )
    path = append_event_snapshot(tmp_path, original, AT)
    original_bytes = path.read_bytes()
    update = source(
        title="重大订单进展公告",
        body=json.dumps({"event_identity": "contract-X", "content": "正式订单获批，与旧框架不同"}),
        retrieved_at=AT.isoformat(),
    )
    current, _ = event_records([update], load_event_history(tmp_path, AT), SESSION, AT)
    assert current[0]["previous_record_id"] == original[0]["record_id"]
    assert current[0]["record_id"] != original[0]["record_id"]
    assert current[0]["novelty"] == "possible_update_requires_verification"
    append_event_snapshot(tmp_path, current, AT + timedelta(seconds=1))
    assert path.read_bytes() == original_bytes and len(list(tmp_path.glob("*.json"))) == 2


def test_first_ingestion_of_old_forecast_is_not_new_market_news():
    old = source(title="2025年年度业绩预告", published_at=None, event_dates=("2026-08-01",))
    rows, _ = event_records([old], [], SESSION, AT)
    assert rows[0]["first_seen_at"] == old.retrieved_at
    assert rows[0]["novelty"] == "long_term_background"
    assert rows[0]["publication_precision"] == "date_only"


@pytest.mark.parametrize("denominator", [0, -1, None])
def test_bad_denominators_never_create_contract_scale(denominator):
    scale = nominal_contract_scale(
        {
            "order_amount_cny": 100,
            "annual_revenue_cny": denominator,
            "currency": "CNY",
            "denominator_scope": "annual_consolidated_revenue",
            "revenue_period": "20251231",
        }
    )
    assert scale["nominal_ratio"] is None


def test_small_exposure_is_scale_not_automatic_core_benefit():
    scale = nominal_contract_scale(
        {
            "order_amount_cny": 100,
            "annual_revenue_cny": 1000000,
            "currency": "CNY",
            "denominator_scope": "annual_consolidated_revenue",
            "revenue_period": "20251231",
        }
    )
    assert scale["nominal_ratio"] == pytest.approx(0.0001)
    assert scale["status"] == "nominal_scale_only"
    assert (
        nominal_contract_scale(
            {"order_amount_cny": 100, "annual_revenue_cny": 1000000, "currency": "USD"}
        )["nominal_ratio"]
        is None
    )


def test_routine_schedule_and_order_route_are_not_sorted_by_price():
    universe = {"600001.SH": candidate("600001.SH", 0.01), "600002.SH": candidate("600002.SH", 1.0)}
    event = source()
    meeting = source(
        title="半年度业绩说明会公告",
        instrument_ids=("600002.SH",),
        url="https://example.org/meeting",
    )
    rows, _ = event_records([event, meeting], [], SESSION, AT)
    annotate_universe(universe, rows, {})
    leads = [
        {
            "instrument_ids": list(e.instrument_ids),
            "relation": "direct",
            "route_type": "event",
            "evidence_ids": [e.evidence_id],
        }
        for e in [meeting, event]
    ]
    pool = build_pool(universe, leads, [], 1)
    assert pool[0]["instrument_id"] == "600001.SH"
    assert (
        next(r for r in rows if r["instrument_id"] == "600002.SH")["novelty"] == "routine_schedule"
    )


def test_no_new_event_does_not_exclude_trend_and_large_fall_needs_improvement():
    trend = candidate("600001.SH")
    crash = candidate(
        "600002.SH", return_1d=-0.09, return_5d=-0.2, close_location=0.1, prior_return_19d=-0.2
    )
    crash.routes = ["回撤放量"]
    universe = {c.instrument_id: c for c in [trend, crash]}
    annotate_universe(universe, [], {})
    assert "trend_continuation" in trend.context["opportunity"]["type_hints"]
    assert not crash.context["opportunity"]["pullback_qualified"]
    assert [r["instrument_id"] for r in build_pool(universe, [], [], 24)] == [trend.instrument_id]


def test_multi_route_unique_count_and_attention_favors_new_question():
    universe = {f"60000{i}.SH": candidate(f"60000{i}.SH") for i in range(6)}
    rows, _ = event_records(
        [source(instrument_ids=("600005.SH",), kind="unverified_user_clue")], [], SESSION, AT
    )
    annotate_universe(universe, rows, {})
    pool = build_pool(
        universe,
        [
            {
                "instrument_ids": ["600005.SH"],
                "route_type": "event",
                "relation": "direct",
                "evidence_ids": rows[0]["source_ids"],
            }
        ],
        ["600005.SH", "600004.SH"],
        6,
        ["600000.SH", "600005.SH"],
    )
    assert len({r["instrument_id"] for r in pool}) == len(pool) == 6
    assert len(next(r for r in pool if r["instrument_id"] == "600005.SH")["recall_routes"]) >= 3


def test_date_uncertainty_and_after_cutoff_remain_explicit():
    undated = source(published_at=None, event_dates=())
    future = source(retrieved_at=(AT + timedelta(seconds=1)).isoformat())
    rows, excluded = event_records([undated, future], [], SESSION, AT)
    assert rows[0]["source_date"] is None
    assert excluded == [{"evidence_id": future.evidence_id, "reason": "after_cutoff"}]


def test_minimum_evidence_budget_precedes_extra_material():
    codes = {f"600{i:03d}.SH" for i in range(24)}
    items = [
        source(instrument_ids=(c,), body="关键材料" * 1000, url=f"https://example.org/{c}")
        for c in codes
    ]
    items += [
        source(
            instrument_ids=("600000.SH",),
            body=f"额外资料{i}" * 500,
            url=f"https://example.org/extra{i}",
        )
        for i in range(20)
    ]
    packet = balanced_evidence_packet(items, codes)
    assert {c for r in packet for c in r["instrument_ids"]} == codes
    assert len(json.dumps(packet, ensure_ascii=False)) <= 32000
    with pytest.raises(ValueError, match="minimum_source_budget"):
        balanced_evidence_packet(items, codes, max_chars=100)


def pool24():
    rows = [candidate(f"600{i:03d}.SH").to_dict() for i in range(24)]
    for row in rows:
        row["program_facts"] = program_facts(row)
    return rows


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate_rank", "duplicate_stock", "made_up_comparator"]
)
def test_24_comparisons_must_be_complete_unique_and_real(fault):
    pool = pool24()
    rows = synthetic_comparisons(pool)
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate_rank":
        rows[0]["rank"] = rows[1]["rank"]
    elif fault == "duplicate_stock":
        rows[0]["instrument_id"] = rows[1]["instrument_id"]
    else:
        rows[0]["comparator_id"] = "601999.SH"
    with pytest.raises(ValueError):
        validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})


def test_core_number_guard_also_checks_new_explanation_fields():
    pool = pool24()
    pool[0]["metrics"]["return_5d"] = -0.1234
    pool[0]["program_facts"] = program_facts(pool[0])
    rows = synthetic_comparisons(pool)
    own = next(r for r in rows if r["instrument_id"] == pool[0]["instrument_id"])
    own["difference"] = "五日涨幅12.34%"
    with pytest.raises(ValueError):
        validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})


@pytest.fixture(scope="module")
def example(tmp_path_factory):
    return make_opportunity_example(tmp_path_factory.mktemp("opportunity") / "new-example")


def test_three_stage_integration_freezes_all_deep_and_keeps_small_topn(example):
    from pathlib import Path

    run = Path(example["run"])
    report = json.loads((run / "report.json").read_text())
    assert report["status"] == "demo" and report["synthetic"]
    assert len(report["opportunity_freeze"]["rows"]) == 24
    assert 1 <= len(report["selection"]["selected"]) <= 8
    assert len(report["candidate_stages"]["eligible_candidates"]) == 180
    assert len(report["candidate_stages"]["cheap_candidates"]) <= 160
    assert len(example["calls"]) == 3
    assert all(c["input_chars"] <= 180000 for c in example["calls"])
    assert report["opportunity_freeze"]["input_sha256"] == fingerprint(
        report["selection_input_packet"]
    )
    html = (run / "report.html").read_text()
    assert "合成演示，不是真实预测" in html and "全部深查股比较" in html


def test_price_gap_is_separate_h5_is_d_plus_four_and_peers_can_outperform(example):
    from pathlib import Path

    item = json.loads(Path(example["mature_observation"]).read_text())
    h5 = [r for r in item["rows"] if r["horizon_sessions"] == 5]
    assert len(item["rows"]) == 96 and len(h5) == 24
    assert all(r["report_reference_close_to_d_open"] == pytest.approx(0.08) for r in h5)
    assert all(r["adjusted_price_return"] < r["report_reference_close_to_d_open"] for r in h5)
    assert any(
        r["adjusted_price_return"] > 0 and r["relative_to_peer_price_change"] < 0 for r in h5
    )
    assert all(r["reference_valid_count"] == r["reference_original_count"] == 59 for r in h5)
    assert all(r["adverse_daily_low_change"] == pytest.approx(-0.015) for r in h5)
    pending = json.loads(Path(example["pending_observation"]).read_text())
    assert all(
        r["adjusted_price_return"] is None and r["status"] == "d_open_not_yet_due"
        for r in pending["rows"]
    )


def test_missing_factor_cannot_be_zero_or_silently_change_peer_members(example, tmp_path):
    from pathlib import Path

    original = Path(example["run"])
    canonical = original.parents[1] / "synthetic_canonical"
    storage = ParquetStorage(canonical)
    report = json.loads((original / "report.json").read_text())
    member = report["opportunity_freeze"]["rows"][0]["reference_ids"][0]
    target = date.fromisoformat(report["timing"]["target_session"])
    values = storage.load_adj_factors_by_date(target)
    # Changes are confined to the module's synthetic fixture, restored before returning.
    storage.save_adj_factors_by_date([r for r in values if r.instrument_id != member], target)
    try:
        path = observe_ranking(
            original,
            canonical,
            tmp_path / "marks",
            allow_synthetic=True,
            observed_at=datetime(2026, 3, 1, 19, tzinfo=SHANGHAI),
        )
        rows = json.loads(path.read_text())["rows"]
        own = next(r for r in rows if r["instrument_id"] == member)
        assert own["adjusted_price_return"] is None and "factor" in own["status"]
        affected = [r for r in rows if member in r["reference_ids"]]
        assert affected and all(r["peer_equal_weight_price_return"] is None for r in affected)
        assert all(r["reference_valid_count"] < r["reference_original_count"] for r in affected)
    finally:
        storage.save_adj_factors_by_date(values, target)


def test_legacy_outputs_never_gain_reconstructed_ranks(example, tmp_path):
    from pathlib import Path

    run = tmp_path / "legacy"
    run.mkdir()
    report = deepcopy(json.loads((Path(example["run"]) / "report.json").read_text()))
    report.pop("opportunity_freeze")
    report.update(run_id="legacy", status="complete", synthetic=False)
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    path = observe_ranking(
        run, Path(example["run"]).parents[1] / "synthetic_canonical", tmp_path / "legacy-marks"
    )
    output = json.loads(path.read_text())
    assert output["rows"] == [] and output["primary_eligibility"] == "legacy_structure_no_ranking"


def test_synthetic_example_cannot_enter_real_summary(example, tmp_path):
    from pathlib import Path

    run = Path(example["run"])
    result = summarize_ranking(
        run.parent, run.parents[1] / "synthetic_ranking_tracking", tmp_path / "summary.json"
    )
    summary = json.loads(result.read_text())
    assert summary["effective_reports"] == [] and summary["groups"]["focus"]["mean"] is None
    assert summary["excluded_reports"][0]["reason"] == "synthetic"


def test_new_config_enables_logic_without_changing_daily_model():
    from pathlib import Path

    config = read_config(Path("config/scout_opportunity.example.json"))
    assert config["opportunity_selection"] and config["model"] == "deepseek-flash"
    assert config["candidate_limit"] == 24 and config["discovery_limit"] == 160


def test_unknown_publication_never_anchors_reaction_to_retrieval(tmp_path):
    from quantlab.scout.demo import make_demo_market

    session = make_demo_market(tmp_path)
    rows, _ = event_records([source(published_at=None, event_dates=())], [], session, AT)
    reactions = price_reactions(rows, {"600001.SH"}, ParquetStorage(tmp_path), session)
    assert reactions[0]["start_session"] is None and reactions[0]["adjusted_close_change"] is None
    record = model_record(
        {
            "events": rows,
            "type_hints": ["insufficient_evidence"],
            "pullback_qualified": False,
            "industry_context": None,
        },
        set(),
    )
    assert record["events"] == [] and record["omitted_event_ids"]


def test_publication_date_is_shanghai_date_not_utc_date():
    rows, _ = event_records([source(published_at="2026-09-30T16:30:00+00:00")], [], SESSION, AT)
    assert rows[0]["source_date"] == "2026-10-01"


def test_sparse_limit_records_do_not_treat_missing_next_record_as_failure():
    c = candidate("600001.SH")
    c.context["tushare_upgrade"] = {
        "limit_history": [
            {"trade_date": "20260928", "limit": "U"},
            {"trade_date": "20260929", "limit": "U"},
        ]
    }
    value = historical_limit_context({c.instrument_id: c}, [date(2026, 9, d) for d in (28, 29, 30)])
    assert value["prior_u_pairs"] == 2
    assert value["counts"]["next_record_unknown_or_conflicting"] == 1
    assert value["conditional_fraction_among_observed_records"] == 1


def test_unverified_schedule_date_cannot_be_presented_as_known():
    pool = pool24()
    rows = synthetic_comparisons(pool)
    rows[0]["analysis"].update(next_observation_date="2026-10-09", next_node_is_hypothesis=False)
    with pytest.raises(ValueError, match="date_not_in_source"):
        validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})


def test_nominal_contract_ratio_keeps_subject_denominator_and_declaration():
    from quantlab.scout.ai import semantic_numeric_issue

    own = candidate("600001.SH").to_dict()
    own["opportunity_record"] = {
        "events": [
            {
                "record_id": "event-X",
                "nominal_scale": nominal_contract_scale(
                    {
                        "order_amount_cny": 100,
                        "annual_revenue_cny": 1000000,
                        "currency": "CNY",
                        "denominator_scope": "annual_consolidated_revenue",
                        "revenue_period": "20251231",
                    }
                ),
            }
        ]
    }
    own["program_facts"] = program_facts(own)
    fact = "fact:600001.SH:event:nominal_contract_ratio:event-X"
    claim = {
        "field": "importance",
        "text": "名义合同金额占年收入0.01%",
        "fact_id": fact,
        "subject_id": "600001.SH",
        "metric": "nominal_contract_ratio",
        "period": "event-X",
        "value": "0.01",
        "unit": "%",
        "direction": "neutral",
    }
    assert (
        semantic_numeric_issue("importance", claim["text"], own, [], [fact], [own], [claim]) is None
    )
    wrong = {**claim, "text": "名义合同金额占年收入10%", "value": "10"}
    assert (
        semantic_numeric_issue("importance", wrong["text"], own, [], [fact], [own], [wrong])
        is not None
    )


def _summary_fixture(root, name, target, finished, values):
    run = root / "runs" / name
    run.mkdir(parents=True)
    frozen = [
        {"instrument_id": code, "final_status": "focus", "rank_band": "top"} for code in values
    ]
    report = {
        "run_id": name,
        "status": "complete",
        "finished_at": finished,
        "timing": {"target_session": target, "generated_at": finished, "primary_eligible": True},
        "opportunity_freeze": {
            "rows": frozen,
            "metadata": {
                "provider": "fixture",
                "model": "fixture",
                "prompt_version": "opportunity_v1",
                "config_sha256": "fixture",
                "input_sha256": name,
            },
        },
    }
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    marks = root / "marks"
    marks.mkdir(exist_ok=True)
    observed = [
        {
            **r,
            "horizon_sessions": 5,
            "status": "observed",
            "adjusted_price_return": values[r["instrument_id"]],
            "relative_to_peer_price_change": -0.01,
            "adverse_daily_low_change": -0.05,
        }
        for r in frozen
    ]
    snapshot = {
        "definition_version": RANK_DEFINITION,
        "run_id": name,
        "synthetic": False,
        "report_sha256": fingerprint(report),
        "observed_at": "2026-10-30T19:00:00+08:00",
        "rows": observed,
    }
    (marks / f"{name}.json").write_text(json.dumps(snapshot))
    return run


def test_summary_uses_target_day_means_not_stock_day_weighting(tmp_path):
    _summary_fixture(
        tmp_path,
        "a",
        "2026-10-08",
        "2026-10-07T19:00:00+08:00",
        {"600001.SH": 0.2, "600002.SH": 0.2},
    )
    _summary_fixture(tmp_path, "b", "2026-10-09", "2026-10-08T19:00:00+08:00", {"600001.SH": -0.1})
    path = summarize_ranking(tmp_path / "runs", tmp_path / "marks", tmp_path / "summary.json")
    output = json.loads(path.read_text())
    assert output["groups"]["focus"]["mean"] == pytest.approx(0.05)
    assert output["groups"]["focus"]["positive_fraction"] == 0.5
    assert output["groups"]["focus"]["relative_to_peer_mean"] == -0.01
    assert output["groups"]["focus"]["independent_target_days"] == 2
    assert output["unique_stock_count"] == 2 and output["repeat_stock_days"] == 1
    assert len(output["version_counts"]) == 1
    assert path.with_suffix(".md").is_file()


def test_newer_unobserved_rank_report_never_falls_back_to_older_winner(tmp_path):
    _summary_fixture(
        tmp_path, "older", "2026-10-08", "2026-10-07T19:00:00+08:00", {"600001.SH": 0.2}
    )
    _summary_fixture(
        tmp_path, "newer", "2026-10-08", "2026-10-08T08:00:00+08:00", {"600002.SH": -0.1}
    )
    (tmp_path / "marks" / "newer.json").unlink()
    output = json.loads(
        summarize_ranking(
            tmp_path / "runs", tmp_path / "marks", tmp_path / "summary.json"
        ).read_text()
    )
    assert output["effective_reports"][0]["run_id"] == "newer"
    assert output["groups"]["focus"]["mean"] is None
    assert output["groups"]["focus"]["missing_reasons"] == {"observation_not_run": 1}


def test_explanation_does_not_make_exhaustive_claim_across_separate_clauses():
    pool = pool24()
    rows = synthetic_comparisons(pool)
    assert validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})[
        "selected"
    ]


def test_hidden_pdf_cannot_upgrade_shown_index_to_body_or_scale():
    index = source(kind="official_announcement_index_unverified", body="仅标题线索")
    pdf = source(kind="official_pdf_text_unverified", body="机器抽取正式合同正文")
    events, _ = event_records([index, pdf], [], SESSION, AT)
    record = {
        "events": events,
        "type_hints": ["event_update"],
        "pullback_qualified": False,
        "industry_context": None,
    }
    packet = [{"evidence_id": index.evidence_id, "kind": index.kind, "body": index.body}]
    shown = model_record(record, {index.evidence_id}, packet)
    assert len(shown["events"]) == 1 and shown["events"][0]["title_only"]
    assert shown["events"][0]["content_excerpt"] == index.body
    assert shown["events"][0]["source_ids"] == [index.evidence_id]
    assert shown["events"][0]["nominal_scale"]["nominal_ratio"] is None
    assert events[0]["title_only"] is False  # acquisition archive stays intact


def test_trade_condition_cannot_escape_unsupported_microstructure_checks():
    pool = pool24()
    rows = synthetic_comparisons(pool)
    rows[0]["trade_conditions"]["known"] = "实际可买性高，资金承接强"
    with pytest.raises(ValueError, match="unsupported_explanation"):
        validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})


def test_opportunity_relative_and_trend_numbers_are_typed_not_generic_tokens():
    from quantlab.scout.facts import check_core_claim, claim_fact_id, extract_core_claims

    own = candidate("600001.SH", relative_return_5d=-0.02).to_dict()
    own["opportunity_record"] = {"events": []}
    facts = {f["fact_id"]: f for f in program_facts(own)}
    text = "近20日涨幅10%。近5日相对行业价格差-2%。额比1.5倍。突破前20日高点幅度2%"
    parsed = extract_core_claims("difference", text, own, [own])
    assert len(parsed) == 4
    assert all(claim_fact_id(c) in facts and check_core_claim(c, facts) is None for c in parsed)
    wrong = extract_core_claims("difference", text.replace("-2%", "2%"), own, [own])[1]
    assert check_core_claim(wrong, facts)[0] == "core_fact_direction_or_value"


def test_investigation_run_schema_requires_full_pool():
    from jsonschema import ValidationError, validate

    from quantlab.scout.opportunity_ai import investigation_schema

    codes = [f"{i:06d}.SZ" for i in range(24)]
    schema = investigation_schema([{"instrument_id": code} for code in codes])
    analysis = {
        "novelty": "unknown", "event_ids": [], "incremental_change": "unknown",
        "economic_link": "unknown", "exposure": "unknown", "importance": "unknown",
        "scale_fact_ids": [], "h5_mechanism": "unknown", "next_observation_date": None,
        "next_node_basis": "unknown", "next_node_is_hypothesis": False,
    }
    rows = [{"instrument_id": code, "analysis": analysis} for code in codes]
    validate({"hypotheses": [], "opportunities": rows}, schema)
    with pytest.raises(ValidationError):
        validate({"hypotheses": [], "opportunities": rows[:8]}, schema)
    with pytest.raises(ValidationError):
        validate({"hypotheses": [], "opportunities": rows + rows[:1]}, schema)
    with pytest.raises(ValidationError):
        validate({"hypotheses": [], "opportunities": rows[:-1] + [
            {"instrument_id": "999999.SH", "analysis": analysis}
        ]}, schema)
