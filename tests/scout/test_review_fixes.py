"""Regression cases for the independent fdfb54b review, using only synthetic inputs."""

import json
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from quantlab.scout.demo import make_demo_market
from quantlab.scout.facts import claim_fact_id, extract_core_claims, program_facts
from quantlab.scout.models import SHANGHAI, Candidate, Coverage, Evidence, fingerprint
from quantlab.scout.opportunities import event_records, nominal_contract_scale, price_reactions
from quantlab.scout.opportunity_ai import validate_comparisons
from quantlab.scout.opportunity_demo import synthetic_analysis, synthetic_comparisons
from quantlab.scout.pipeline import DEFAULT_CONFIG, run_scout
from quantlab.scout.ranking_tracking import DEFINITION, observe_ranking, summarize_ranking

SESSION = date(2026, 9, 30)
CUTOFF = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI)
INDEX_BODY = "巨潮公告索引仅提供标题和PDF链接；正文未由Scout读取。公告日期不等于精确发布时间。"


def index_source(title, url, day="2026-09-30", **updates):
    values = {
        "source": "synthetic_cninfo",
        "title": title,
        "body": INDEX_BODY,
        "url": url,
        "published_at": None,
        "retrieved_at": "2026-10-01T10:00:00+08:00",
        "kind": "official_announcement_index_unverified",
        "instrument_ids": ("600001.SH",),
        "event_dates": (day,),
    }
    return Evidence(**(values | updates))


def separate_notices():
    return [
        index_source("关于与甲客户签订销售合同的公告", "https://example.org/a.PDF", "2026-09-28"),
        index_source("关于与乙客户签订采购合同的公告", "https://example.org/b.PDF"),
    ]


def test_different_index_documents_do_not_merge_placeholder_body():
    rows, _ = event_records(separate_notices(), [], SESSION, CUTOFF)
    assert len(rows) == 2 and len({r["record_id"] for r in rows}) == 2
    assert all(len(r["source_ids"]) == 1 for r in rows)


def test_new_index_document_is_not_repeated_across_snapshots():
    first, second = separate_notices()
    old, _ = event_records([first], [], SESSION, CUTOFF)
    current, _ = event_records([second], old, SESSION, CUTOFF)
    assert current[0]["record_id"] != old[0]["record_id"]
    assert current[0]["novelty"] == "unseen_history_unknown"
    assert current[0]["previous_record_id"] is None


def test_same_document_index_and_pdf_share_one_event():
    index = separate_notices()[0]
    pdf = index_source(
        index.title,
        index.url,
        "2026-09-28",
        kind="official_pdf_text_unverified",
        body="正式销售合同原文（合成）",
    )
    rows, _ = event_records([index, pdf], [], SESSION, CUTOFF)
    assert len(rows) == 1 and len(rows[0]["source_ids"]) == 2
    assert rows[0]["title_only"] is False
    # Obtaining the same document's body later does not create a new market event.
    old, _ = event_records([index], [], SESSION, CUTOFF)
    enriched, _ = event_records([pdf], old, SESSION, CUTOFF)
    assert enriched[0]["novelty"] == "repeated_content"


def test_real_body_syndications_still_merge_but_index_without_url_needs_identity():
    first, second = separate_notices()
    full = [
        index_source(s.title, s.url, kind="news", body="完全相同的正式合同新闻正文")
        for s in (first, second)
    ]
    rows, _ = event_records(full, [], SESSION, CUTOFF)
    assert len(rows) == 1 and len(rows[0]["source_ids"]) == 2
    rows, _ = event_records(
        [
            index_source(first.title, None, "2026-09-28"),
            index_source(second.title, None),
        ],
        [],
        SESSION,
        CUTOFF,
    )
    assert len(rows) == 2


def comparison_case(text):
    pool = [
        Candidate(
            f"600{i:03d}.SH",
            f"合成{i}",
            {
                "return_1d": 0.01,
                "return_5d": 0.05,
                "return_20d": 0.20 if i else 0.10,
                "relative_return_5d": -0.02,
                "amount_ratio_5d": 1.5,
                "breakout_20d": 0.02,
            },
            0.5,
        ).to_dict()
        for i in range(24)
    ]
    rows = synthetic_comparisons(pool)
    for candidate in pool:
        candidate["opportunity_record"] = {"events": []}
    peer = pool[0]
    peer["opportunity_record"]["events"] = [
        {
            "record_id": "synthetic-event-peer",
            "instrument_id": peer["instrument_id"],
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
    for candidate in pool:
        candidate["program_facts"] = program_facts(candidate)
    own = pool[-1]
    row = rows[0]
    row["difference"] = "600000.SH" + text + "。本例仅检验比较事实，不确认盈利贡献。"
    row["evidence_ids"].append("market:600000.SH")
    parsed = extract_core_claims("difference", row["difference"], own, pool)
    assert len(parsed) == 1 and parsed[0].subject_id == "600000.SH"
    claim = parsed[0]
    row["fact_ids"] = [claim_fact_id(claim)]
    row["quant_claims"] = [
        {
            "field": claim.field,
            "text": claim.text,
            "subject_id": claim.subject_id,
            "metric": claim.metric,
            "period": claim.period,
            "value": str(claim.value),
            "unit": claim.unit,
            "direction": claim.direction,
            "fact_id": claim_fact_id(claim),
        }
    ]
    return pool, rows


@pytest.mark.parametrize(
    "text",
    [
        "近20日涨幅10%",
        "近5日相对行业价格差-2%",
        "额比1.5倍",
        "突破前20日高点幅度2%",
        "名义合同金额占年收入0.01%",
    ],
)
def test_valid_peer_new_metrics_pass_the_complete_comparison_path(text):
    pool, rows = comparison_case(text)
    assert validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})[
        "selected"
    ]


@pytest.mark.parametrize(
    "fault", ["subject", "unit", "direction", "value", "missing_fact", "hidden_table"]
)
def test_peer_comparison_still_rejects_wrong_or_unshown_facts(fault):
    pool, rows = comparison_case("近20日涨幅10%")
    declaration = rows[0]["quant_claims"][0]
    if fault == "subject":
        declaration["subject_id"] = pool[-1]["instrument_id"]
    elif fault == "unit":
        declaration["unit"] = "times"
    elif fault == "direction":
        rows[0]["difference"] = rows[0]["difference"].replace("涨幅10%", "涨幅-10%")
        declaration.update(text="近20日涨幅-10%", value="-10")
    elif fault == "value":
        rows[0]["difference"] = rows[0]["difference"].replace("10%", "20%")
        declaration.update(text="近20日涨幅20%", value="20")
    elif fault == "missing_fact":
        pool[0]["metrics"].pop("return_20d")
        pool[0]["program_facts"] = program_facts(pool[0])
    else:
        pool[0]["program_facts"] = []
    with pytest.raises(ValueError):
        validate_comparisons({"market_view": "合成市场", "comparisons": rows}, pool, [], {})


class ReactionStorage:
    """Two daily closes suffice to expose pre-publication price leakage."""

    def __init__(self, first=date(2026, 9, 29), last=SESSION, closed=()):
        self.closes = {first: 10.0, last: 11.0}
        self.closed = closed

    def load_trading_calendar(self):
        return [
            SimpleNamespace(trade_date=day, exchange="SSE", is_open=True) for day in self.closes
        ] + [SimpleNamespace(trade_date=day, exchange="SSE", is_open=False) for day in self.closed]

    def load_daily_bars_by_date(self, day):
        return [SimpleNamespace(instrument_id="600001.SH", close=self.closes[day])]

    def load_adj_factors_by_date(self, day):
        return [SimpleNamespace(instrument_id="600001.SH", adj_factor=1.0)]


@pytest.mark.parametrize(
    "published,asof,expected_start,status,value",
    [
        ("2026-09-30T08:00:00+08:00", SESSION, "2026-09-29", "preopen_daily_observation", 0.1),
        ("2026-09-30T10:30:00+08:00", SESSION, "2026-09-29", "intraday_daily_window_mixed", 0.1),
        ("2026-09-30T19:00:00+08:00", SESSION, "2026-09-30", "post_event_not_observed", None),
        ("2026-09-30T15:00:00+08:00", SESSION, "2026-09-30", "post_event_not_observed", None),
        ("2026-09-29T19:00:00+08:00", SESSION, "2026-09-29", "after_close_daily_observation", 0.1),
        (None, SESSION, "2026-09-29", "date_bounded_observation_timing_uncertain", 0.1),
    ],
)
def test_reaction_respects_known_publication_time(published, asof, expected_start, status, value):
    evidence = index_source("重大订单公告", "https://example.org/time", published_at=published)
    rows, _ = event_records([evidence], [], SESSION, CUTOFF)
    result = price_reactions(rows, {"600001.SH"}, ReactionStorage(), asof)[0]
    assert result["start_session"] == expected_start and result["status"] == status
    assert (
        result["adjusted_close_change"] == pytest.approx(value)
        if value is not None
        else (result["adjusted_close_change"] is None)
    )


@pytest.mark.parametrize("asof,expected", [(date(2026, 9, 25), None), (date(2026, 9, 28), 0.1)])
def test_nontrading_publication_waits_for_a_later_trading_session(asof, expected):
    evidence = index_source(
        "重大订单公告", "https://example.org/weekend", published_at="2026-09-26T12:00:00+08:00"
    )
    rows, _ = event_records([evidence], [], SESSION, CUTOFF)
    storage = ReactionStorage(date(2026, 9, 25), date(2026, 9, 28), (date(2026, 9, 26),))
    result = price_reactions(rows, {"600001.SH"}, storage, asof)[0]
    assert result["start_session"] == "2026-09-25"
    assert result["status"] == (
        "nontrading_day_daily_observation" if expected else "post_event_not_observed"
    )
    assert (
        result["adjusted_close_change"] == pytest.approx(expected)
        if expected is not None
        else (result["adjusted_close_change"] is None)
    )


def test_fully_unknown_publication_cannot_use_retrieval_as_price_anchor():
    rows, _ = event_records(
        [index_source("重大订单公告", None, event_dates=())], [], SESSION, CUTOFF
    )
    result = price_reactions(rows, {"600001.SH"}, ReactionStorage(), SESSION)[0]
    assert result["status"] == "window_or_prices_unknown"
    assert result["start_session"] is None and result["adjusted_close_change"] is None


def ranking_case(root):
    run = root / "runs" / "artificial-review-fixture"
    run.mkdir(parents=True)
    packet = {"fixture_only": True}
    frozen = [
        {
            "instrument_id": code,
            "final_status": "watch",
            "rank": index + 1,
            "rank_band": "top" if index == 0 else "middle",
            "primary_type": "trend_continuation",
        }
        for index, code in enumerate(("600001.SH", "600002.SH"))
    ]
    freeze = {
        "version": "opportunity_v1",
        "status": "complete",
        "rows": frozen,
        "input_sha256": fingerprint(packet),
        "market_asof_session": "2026-09-29",
        "metadata": {
            "provider": "fixture",
            "model": "fixture",
            "prompt_version": "opportunity_v1",
            "config_sha256": "fixture",
        },
    }
    report = {
        "run_id": run.name,
        "status": "complete",
        "fixture_only": True,
        "finished_at": "2026-09-29T19:00:00+08:00",
        "selection_input_packet": packet,
        "timing": {
            "target_session": "2026-09-30",
            "generated_at": "2026-09-29T19:00:00+08:00",
            "primary_eligible": True,
        },
        "opportunity_freeze": freeze,
    }
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    marks = root / "marks"
    marks.mkdir()
    snapshot = {
        "run_id": run.name,
        "report_sha256": fingerprint(report),
        "frozen": deepcopy(freeze),
        "definition_version": DEFINITION,
        "synthetic": False,
        "observed_at": "2026-10-03T19:00:00+08:00",
        "primary_eligibility": "eligible",
        "d_session": "2026-09-30",
        "rows": [
            {
                **r,
                "horizon_sessions": 5,
                "status": "observed",
                "adjusted_price_return": 0.1 if i == 0 else -0.1,
                "relative_to_peer_price_change": None,
                "adverse_daily_low_change": -0.05,
            }
            for i, r in enumerate(frozen)
        ],
    }
    return run, marks, snapshot


def summarize_case(root, marks, snapshot):
    if snapshot is not None:
        (marks / "fixture.json").write_text(json.dumps(snapshot))
    path = summarize_ranking(root / "runs", marks, root / "summary.json")
    return json.loads(path.read_text())


def test_missing_exchange_calendar_preserves_all_frozen_stock_days(tmp_path):
    run, marks, _ = ranking_case(tmp_path)
    before = summarize_case(tmp_path, marks, None)
    assert before["groups"]["watch"]["original_stock_days"] == 2
    path = observe_ranking(run, tmp_path / "empty-data", marks)
    assert json.loads(path.read_text())["primary_eligibility"] == "target_not_in_exchange_calendar"
    after = summarize_case(tmp_path, marks, None)
    group = after["groups"]["watch"]
    assert group["original_stock_days"] == 2 and group["valid_stock_days"] == 0
    assert group["missing_reasons"] == {"target_not_in_exchange_calendar": 2}


@pytest.mark.parametrize(
    "kind,valid,reason",
    [
        ("none", 0, "observation_not_run"),
        ("empty", 0, "empty_observation_snapshot"),
        ("partial", 1, "h5_observation_missing"),
        ("full", 2, None),
    ],
)
def test_h5_summary_uses_freeze_as_left_table(tmp_path, kind, valid, reason):
    _, marks, snapshot = ranking_case(tmp_path)
    if kind == "none":
        snapshot = None
    elif kind == "empty":
        snapshot["rows"] = []
    elif kind == "partial":
        snapshot["rows"].pop()
    output = summarize_case(tmp_path, marks, snapshot)
    group = output["groups"]["watch"]
    assert group["original_stock_days"] == 2 and group["valid_stock_days"] == valid
    assert group["independent_target_days"] == int(valid == 2)
    assert group["missing_reasons"] == ({reason: 2 - valid} if reason else {})
    assert group["mean"] == (0 if valid == 2 else None)


@pytest.mark.parametrize(
    "fault", ["duplicate", "foreign", "rank", "type", "status", "root_identity"]
)
def test_h5_snapshot_rejects_duplicate_foreign_or_changed_identity(tmp_path, fault):
    _, marks, snapshot = ranking_case(tmp_path)
    if fault == "duplicate":
        snapshot["rows"].append(deepcopy(snapshot["rows"][0]))
    elif fault == "foreign":
        snapshot["rows"][0]["instrument_id"] = "600999.SH"
    elif fault == "rank":
        snapshot["rows"][0]["rank"] = 99
    elif fault == "type":
        snapshot["rows"][0]["primary_type"] = "event_update"
    elif fault == "status":
        snapshot["rows"][0]["final_status"] = "focus"
    else:
        snapshot["frozen"]["input_sha256"] = "different-input"
    with pytest.raises(ValueError):
        summarize_case(tmp_path, marks, snapshot)


@pytest.mark.parametrize("failure", ["schema", "truncated", "transport"])
def test_final_request_failure_keeps_exact_built_input_and_public_output(
    tmp_path, monkeypatch, failure
):
    canonical = tmp_path / "synthetic-canonical"
    day = make_demo_market(canonical)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret-not-for-archive")
    submitted = {}
    config = DEFAULT_CONFIG | {
        "provider": "deepseek",
        "opportunity_selection": True,
        "tushare_news_sources": [],
        "tushare_industry": False,
        "tushare_disclosures": False,
        "tushare_upgrade": False,
        "cninfo_announcements": False,
        "akshare_hot_rank": False,
    }
    now = datetime.now(SHANGHAI).isoformat()
    evidence = Evidence(
        "synthetic",
        "合成正式订单线索",
        "规模与盈利贡献未知",
        "https://example.org/failure",
        now,
        now,
        "company_event_date_only",
        ("600001.SH",),
    )

    def ask(client, prompt, schema, search=False):
        client.calls.append({"status": "started", "search": search})
        stage = len(client.calls)
        client.failed_response = None
        if stage == 1:
            return {"hypotheses": []}, {"synthetic": True}
        if stage == 2:
            packet = json.loads(prompt[prompt.index('{"candidates":') :])
            return {
                "hypotheses": [],
                "opportunities": [
                    {"instrument_id": c["instrument_id"], "analysis": synthetic_analysis()}
                    for c in packet["candidates"]
                ],
            }, {"synthetic": True}
        submitted.update(prompt=prompt, schema=schema)
        if failure == "transport":
            client.calls[-1]["status"] = "network_error"
            raise RuntimeError("Synthetic transport failure; delivery unknown")
        client.calls[-1]["status"] = "schema_error" if failure == "schema" else "length"
        client.failed_response = {
            "choices": [
                {
                    "finish_reason": "stop" if failure == "schema" else "length",
                    "message": {"role": "assistant", "content": "invalid synthetic public output"},
                }
            ]
        }
        raise ValueError(
            "Synthetic schema validation failure"
            if failure == "schema"
            else "Synthetic response incomplete"
        )

    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch(
            "quantlab.scout.pipeline.collect_sources",
            return_value=([evidence], [Coverage("synthetic", "synthetic", 1)]),
        ),
        patch("quantlab.scout.ai.DeepSeekResearch.ask", new=ask),
        patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")),
    ):
        path, report = run_scout(
            canonical, tmp_path / "synthetic-failure-runs", config, online=True
        )
    assert len(report["ai_calls"]) == 3 and submitted
    assert report["status"] == "incomplete" and report["selection"]["selected"] == []
    assert "opportunity_freeze" not in report
    packet = report["selection_input_packet"]
    assert packet and packet["candidates"] and packet["evidence"]
    assert report["selection_input_evidence"] == packet["evidence"]
    assert report["selection_input_prompt"] == submitted["prompt"]
    assert report["selection_input_schema"] == submitted["schema"]
    request = report["selection_request"]
    assert request["packet_sha256"] == fingerprint(packet)
    assert request["prompt_sha256"] == sha256(submitted["prompt"].encode()).hexdigest()
    assert request["schema_sha256"] == fingerprint(submitted["schema"])
    assert request["delivery_status"] == (
        "attempted_delivery_unknown" if failure == "transport" else "response_received"
    )
    assert request["state"] == (
        "request_failed" if failure == "transport" else "model_response_failed"
    )
    saved = json.loads((path / "report.json").read_text())
    raw = json.loads((path / "ai_responses.json").read_text())
    assert saved["selection_input_packet"] == json.loads(json.dumps(packet))
    assert fingerprint(saved["selection_input_packet"]) == request["packet_sha256"]
    assert bool(raw[-1].get("validation_failed")) == (failure != "transport")
    assert "synthetic-secret" not in json.dumps(saved) + json.dumps(raw)
