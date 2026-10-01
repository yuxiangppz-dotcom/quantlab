"""Guardrails for timing, missing evidence, citation binding and offline operation."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from unittest.mock import patch

import pytest

from quantlab.data.models import DailyBasic, DailyPriceLimit
from quantlab.data.storage import ParquetStorage
from quantlab.scout.ai import ShownEvidence, bind_hypotheses, source_urls, validate_selection
from quantlab.scout.demo import make_demo_market
from quantlab.scout.market import add_sectors, latest_completed_session, scan_market
from quantlab.scout.models import SHANGHAI, Evidence, admit_evidence, fingerprint
from quantlab.scout.pipeline import DEFAULT_CONFIG, read_config, run_scout
from quantlab.scout.sources import parse_feed
from quantlab.scout.tracking import observe_run


@pytest.fixture
def market(tmp_path):
    root = tmp_path / "canonical"
    day = make_demo_market(root)
    return root, day


def evidence(**kwargs):
    values = dict(
        source="test",
        title="headline",
        body="body",
        url="https://example.org/news",
        published_at="2026-01-10T10:00:00+08:00",
        retrieved_at="2026-01-10T11:00:00+08:00",
    )
    return Evidence(**(values | kwargs))


def test_evidence_future_stale_duplicate_and_unknown():
    now = datetime(2026, 1, 10, 12, tzinfo=SHANGHAI)
    items = [
        evidence(),
        evidence(source="syndicated"),
        evidence(title="future", published_at="2026-01-11T10:00:00+08:00"),
        evidence(title="old", published_at="2026-01-01T10:00:00+08:00"),
        evidence(title="unknown", published_at=None),
    ]
    admitted, counts = admit_evidence(items, now, 72)
    assert len(admitted) == 2
    assert counts == {"future": 1, "stale": 1, "duplicate": 1, "undated": 1}
    assert admitted[-1].published_at is None


def test_naive_timestamp_and_unsafe_link_rejected():
    with pytest.raises(ValueError):
        evidence(published_at="2026-01-10T10:00:00")
    with pytest.raises(ValueError):
        evidence(url="file:///etc/passwd")


def test_rss_and_atom_are_explicit_about_missing_dates():
    now = datetime.now(SHANGHAI)
    raw = (
        b"<rss><channel><item><title>x</title><link>https://example.org/x</link>"
        b"<pubDate>Sat, 10 Jan 2026 10:00:00 +0800</pubDate></item></channel></rss>"
    )
    assert parse_feed(raw, "rss", now)[0].published_at.endswith("+08:00")
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>x</title>'
        b'<link href="https://example.org/x"/></entry></feed>'
    )
    assert parse_feed(atom, "atom", now)[0].published_at is None
    with pytest.raises(ValueError):
        parse_feed(b'<!DOCTYPE rss [<!ENTITY x "a">]><rss/>', "rss", now)


def test_missing_partition_is_not_silently_skipped(market):
    root, day = market
    ParquetStorage(root).adj_factor_path(day).unlink()
    with pytest.raises(ValueError, match="Missing daily/adj_factor"):
        scan_market(root, day)


def test_split_adjustment_does_not_create_fake_crash(market):
    root, day = market
    storage = ParquetStorage(root)
    before, _ = scan_market(root, day)
    bars = storage.load_daily_bars_by_date(day)
    stock = bars[0].instrument_id
    storage.save_daily_bars_by_date(
        [
            replace(b, open=b.open / 2, high=b.high / 2, low=b.low / 2, close=b.close / 2)
            if b.instrument_id == stock
            else b
            for b in bars
        ],
        day,
    )
    storage.save_adj_factors_by_date(
        [
            replace(x, adj_factor=2) if x.instrument_id == stock else x
            for x in storage.load_adj_factors_by_date(day)
        ],
        day,
    )
    after, _ = scan_market(root, day)
    assert after[stock].metrics["return_5d"] == before[stock].metrics["return_5d"]


def test_sector_route_and_unknown_turnover(market):
    root, day = market
    universe, _ = scan_market(root, day)
    assert all(x.metrics["turnover_rate_pct"] is None for x in universe.values())
    assert all(x.metrics["up_limit"] is None for x in universe.values())
    codes = add_sectors(universe, {code: "synthetic" for code in universe})
    assert codes
    assert all("板块关联:synthetic" in universe[x].routes for x in codes)


def test_canonical_optional_fields_keep_original_units(market):
    root, day = market
    storage = ParquetStorage(root)
    code = storage.load_daily_bars_by_date(day)[0].instrument_id
    storage.save_daily_basic_by_date([DailyBasic(code, day, 0.052, 1e9, 8e8)], day)
    limit = DailyPriceLimit(code, day, 10.0, 11.0, 9.0, "SSE", "test", "fixture-limit")
    storage.save_daily_price_limits_by_date([limit], day)
    assert storage.load_daily_price_limits_by_date(day) == [limit]
    universe, _ = scan_market(root, day)
    assert universe[code].metrics["turnover_rate_pct"] == pytest.approx(5.2)
    assert universe[code].metrics["up_limit"] == 11.0


def test_stale_calendar_blocks_live(market):
    root, day = market
    with pytest.raises(ValueError, match="calendar missing/stale"):
        latest_completed_session(
            ParquetStorage(root),
            datetime.combine(day + timedelta(days=10), datetime.min.time(), SHANGHAI),
        )


def test_daily_cutoff_before_18_uses_previous_session(market):
    root, day = market
    now = datetime.combine(day, datetime.min.time().replace(hour=14), SHANGHAI)
    assert latest_completed_session(ParquetStorage(root), now) < day
    assert latest_completed_session(ParquetStorage(root), now.replace(hour=19)) == day


def test_hallucinated_urls_or_codes_cannot_enter_pool():
    ev = evidence()
    hypotheses = {
        "hypotheses": [
            {
                "summary": "x",
                "instrument_ids": ["600001.SH", "INVALID"],
                "relation": "theme",
                "source_urls": [ev.url],
                "counterargument": "unknown",
            },
            {
                "summary": "x",
                "instrument_ids": ["600001.SH"],
                "relation": "theme",
                "source_urls": ["https://invented.test/"],
                "counterargument": "unknown",
            },
        ]
    }
    bound = bind_hypotheses(hypotheses, [ev], {"600001.SH"})
    assert len(bound) == 1
    assert bound[0]["instrument_ids"] == ["600001.SH"]
    assert bound[0]["evidence_ids"] == [ev.evidence_id]


def selection(code="600001.SH", ids=None):
    return {
        "market_view": "test",
        "selected": [
            {
                "instrument_id": code,
                "status": "focus",
                "thesis": "test",
                "risk": "test",
                "invalidation": "test",
                "evidence_ids": ids or [f"market:{code}"],
            }
        ],
    }


@pytest.mark.parametrize(
    "result",
    [selection("INVALID"), selection(ids=["invented"]), selection(ids=["market:600002.SH"])],
)
def test_final_selection_rejects_unknown_evidence(result):
    with pytest.raises(ValueError):
        validate_selection(result, [{"instrument_id": "600001.SH"}], [])


def test_final_selection_rejects_other_stocks_or_unshown_evidence():
    own = evidence(title="own", instrument_ids=("600001.SH",))
    other = evidence(title="other", instrument_ids=("600002.SH",))
    candidates = [
        {"instrument_id": "600001.SH", "evidence_ids": [own.evidence_id]},
        {"instrument_id": "600002.SH", "evidence_ids": [other.evidence_id]},
    ]
    with pytest.raises(ValueError, match="not shown or bound"):
        validate_selection(
            selection(ids=["market:600001.SH", other.evidence_id]),
            candidates,
            [own, other],
        )
    with pytest.raises(ValueError, match="not shown or bound"):
        validate_selection(
            selection(ids=["market:600001.SH", own.evidence_id]),
            candidates,
            [other],
        )
    assert (
        validate_selection(
            selection(ids=["market:600001.SH", own.evidence_id]),
            candidates,
            [own, other],
        )["selected"][0]["instrument_id"]
        == "600001.SH"
    )


def test_selection_accepts_cited_numbers_but_rejects_unsourced_precision():
    code = "600001.SH"
    source = evidence(
        source="tushare:limit_list_d",
        title="历史涨停结构",
        body="历史开板次数2次，交易日20260930；不是未来成交保证。",
        instrument_ids=(code,),
    )
    candidate = {
        "instrument_id": code,
        "metrics": {"return_1d": 0.0997, "one_price_session": False},
        "evidence_ids": [source.evidence_id],
    }
    result = selection(ids=[f"market:{code}", source.evidence_id])
    result["selected"][0]["thesis"] = "历史开板2次，日涨幅9.97%；后续能否延续仍未知"
    assert validate_selection(result, [candidate], [source]) == result
    result["selected"][0]["thesis"] = "历史开板7次，日涨幅9.97%；后续能否延续仍未知"
    with pytest.raises(ValueError, match="numeric"):
        validate_selection(result, [candidate], [source])
    result["selected"][0]["thesis"] = "历史开板930次；后续能否延续仍未知"
    with pytest.raises(ValueError, match="numeric"):
        validate_selection(result, [candidate], [source])


def test_market_view_allows_rounding_of_supplied_market_breadth():
    result = selection()
    result["market_view"] = "正收益比例0.5545，中位涨幅0.3856%"
    candidate = {"instrument_id": "600001.SH", "metrics": {"one_price_session": False}}
    market = {"positive_fraction": 0.55446194, "median_return_1d": 0.0038560475}
    assert validate_selection(result, [candidate], [], market) == result
    result["market_view"] = "正收益比例0.9555，中位涨幅0.3856%"
    with pytest.raises(ValueError, match="market view"):
        validate_selection(result, [candidate], [], market)


def test_numeric_claim_must_appear_in_actual_prompt_excerpt():
    code = "600001.SH"
    item = evidence(body="公告完整正文中后段金额123万元", instrument_ids=(code,))
    prompt_excerpt = ShownEvidence.from_packet(
        {**item.to_dict(), "body": "公告正文节选，金额段未进入提示词"}
    )
    candidate = {"instrument_id": code, "metrics": {}, "evidence_ids": [item.evidence_id]}
    result = selection(ids=[f"market:{code}", item.evidence_id])
    result["selected"][0]["thesis"] = "公告金额123万元，后续仍待观察"
    with pytest.raises(ValueError, match="numeric"):
        validate_selection(result, [candidate], [prompt_excerpt])


def test_chinese_date_range_is_not_treated_as_financial_number():
    code = "600001.SH"
    candidate = {"instrument_id": code, "metrics": {"one_price_session": False}}
    result = selection(ids=[f"market:{code}"])
    result["selected"][0]["thesis"] = "9月28—30日有连续行情样本，后续待观察"
    assert validate_selection(result, [candidate], []) == result


def test_named_comparator_can_use_its_program_metric_only():
    code = "600001.SH"
    own = {"instrument_id": code, "name": "甲公司", "metrics": {}}
    peer = {"instrument_id": "600002.SH", "name": "乙公司", "metrics": {"return_1d": 0.0198}}
    result = selection(ids=[f"market:{code}"])
    result["selected"][0]["thesis"] = "相较乙公司1日涨幅1.98%，本股仍待观察"
    assert validate_selection(result, [own, peer], []) == result
    result["selected"][0]["thesis"] = "相较其他公司1日涨幅1.98%，本股仍待观察"
    with pytest.raises(ValueError, match="numeric"):
        validate_selection(result, [own, peer], [])


def test_sampled_notice_cannot_be_called_the_only_company_disclosure():
    code = "600001.SH"
    result = selection(ids=[f"market:{code}"])
    result["selected"][0]["thesis"] = "这是近期唯一正式公司级披露，后续仍待观察"
    with pytest.raises(ValueError, match="exhaustive disclosure"):
        validate_selection(result, [{"instrument_id": code, "metrics": {}}], [])


def test_final_selection_rejects_wrong_one_price_and_sell_only_claims():
    candidate = {"instrument_id": "600001.SH", "metrics": {"one_price_session": False}}
    result = selection()
    result["selected"][0]["thesis"] = "一价收盘"
    with pytest.raises(ValueError, match="non-one-price"):
        validate_selection(result, [candidate], [])

    seat = evidence(
        source="disclosure:top_inst",
        kind="trading_disclosure",
        instrument_ids=("600001.SH",),
        body=json.dumps(
            {
                "records": [
                    {"trade_date": "2026-01-10", "net_buy": -2},
                    {"trade_date": "2026-01-10", "net_buy": 3},
                ]
            }
        ),
    )
    result["selected"][0]["thesis"] = "明细全部只出现在卖出席位"
    result["selected"][0]["evidence_ids"] = ["market:600001.SH", seat.evidence_id]
    candidate["evidence_ids"] = [seat.evidence_id]
    with pytest.raises(ValueError, match="mixed seat records"):
        validate_selection(result, [candidate], [seat])


@pytest.mark.parametrize(
    ("claim", "caution"),
    [
        ("一字涨停并封至收盘", "日线不能证明一字封板"),
        ("实际可买性极低", "实际可买性未知"),
        ("流动性与可成交性更好", "日成交额不等于次日可成交性更好"),
        ("缩量涨停说明分歧小", "缩量不能证明分歧小"),
        ("筹码更轻且弹性更高", "成交额不能推断筹码更轻且弹性更高"),
        ("资金推动逻辑明确", "成交额不能推断资金推动机制"),
    ],
)
def test_selection_rejects_execution_and_causal_claims_without_order_book(claim, caution):
    result = selection()
    result["selected"][0]["risk"] = claim
    with pytest.raises(ValueError, match="order-book, execution or causal"):
        validate_selection(result, [{"instrument_id": "600001.SH"}], [])
    result["selected"][0]["risk"] = caution
    assert validate_selection(result, [{"instrument_id": "600001.SH"}], []) == result


def test_search_sources_are_extracted_from_real_tool_metadata():
    raw = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [{"url": "https://example.org/x"}, {"url": "file:///etc/passwd"}]
                },
            }
        ]
    }
    assert source_urls(raw) == {"https://example.org/x": "https://example.org/x"}


def test_offline_is_network_free_immutable_and_no_ai_recommendation(market, tmp_path):
    root, day = market
    with (
        patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")),
        patch("quantlab.scout.pipeline.OpenAIResearch", side_effect=AssertionError("no AI")),
    ):
        path, report = run_scout(root, tmp_path / "runs", DEFAULT_CONFIG, session=day)
        second, _ = run_scout(root, tmp_path / "runs", DEFAULT_CONFIG, session=day)
    assert path != second
    assert report["selection"]["selected"] == []
    assert report["candidates"]
    assert "不是推荐" in report["selection"]["market_view"]
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["report_sha256"] == fingerprint(report)
    assert manifest["html_sha256"] == sha256((path / "report.html").read_bytes()).hexdigest()
    assert (path / "report.md").is_file()
    assert (path / "report.html").is_file()


def test_live_missing_key_stops_before_sources(market, tmp_path, monkeypatch):
    root, day = market
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.pipeline.collect_sources", side_effect=AssertionError("no source")),
    ):
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            run_scout(root, tmp_path / "runs", DEFAULT_CONFIG, online=True)


def test_live_stage_failure_preserves_report_but_never_recommendation(
    market, tmp_path, monkeypatch
):
    root, day = market
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-never-persist")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = DEFAULT_CONFIG | {"model": "test-model", "tushare_news_sources": []}
    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.OpenAIResearch.ask", side_effect=ValueError("bad response")),
    ):
        path, report = run_scout(root, tmp_path / "runs", config, online=True)
    assert report["status"] == "incomplete"
    assert not report["selection"]["selected"]
    assert "test-secret" not in (path / "report.json").read_text()


def test_output_cannot_modify_canonical(market):
    root, day = market
    with pytest.raises(ValueError, match="canonical"):
        run_scout(root, root / "reports", DEFAULT_CONFIG, session=day)


def test_tracker_rejects_changed_report_and_demo(market, tmp_path):
    root, day = market
    path, report = run_scout(root, tmp_path / "runs", DEFAULT_CONFIG, session=day, demo=True)
    with pytest.raises(ValueError, match="Synthetic"):
        observe_run(path, root, tmp_path / "marks")
    report["status"] = "live_research_unvalidated"
    (path / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="integrity"):
        observe_run(path, root, tmp_path / "marks")


def test_configuration_rejects_unbounded_spend(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"max_tool_calls": 100000}))
    with pytest.raises(ValueError, match="max_tool_calls"):
        read_config(path)


def test_selection_watch_limit_is_enforced():
    codes = [f"60000{i}.SH" for i in range(6)]
    rows = [selection(code)["selected"][0] | {"status": "watch"} for code in codes]
    with pytest.raises(ValueError, match="limit"):
        validate_selection(
            {"market_view": "test", "selected": rows},
            [{"instrument_id": code} for code in codes],
            [],
        )


def test_three_stage_live_flow_archives_evidence(market, tmp_path, monkeypatch):
    root, day = market
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = DEFAULT_CONFIG | {"model": "test-model", "tushare_news_sources": []}
    raw = {
        "output": [
            {
                "type": "web_search_call",
                "action": {"sources": [{"url": "https://example.org/announcement"}]},
            }
        ]
    }
    stages = []

    def ask(client, prompt, schema, search=False):
        stages.append(search)
        client.calls.append({"search": search, "status": "completed"})
        if search:
            code = "600001.SH"
            return {
                "hypotheses": [
                    {
                        "summary": "source-linked finding",
                        "instrument_ids": [code],
                        "relation": "theme",
                        "source_urls": ["https://example.org/announcement"],
                        "counterargument": "unverified",
                    }
                ]
            }, raw
        packet = json.loads(prompt.split("\n", 1)[1])
        code = packet["candidates"][0]["instrument_id"]
        ref = packet["evidence"][0]["evidence_id"]
        return selection(code, [f"market:{code}", ref]), {"status": "completed"}

    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.OpenAIResearch.ask", new=ask),
    ):
        path, report = run_scout(root, tmp_path / "runs", config, online=True)
    assert stages == [True, True, False]
    assert report["status"] == "live_research_unvalidated"
    assert len(report["selection"]["selected"]) == 1
    assert len(report["evidence"]) == 1
    assert report["evidence"][0]["published_at"] is None
    assert len(report["market_universe"]) >= len(report["candidates"])
    assert len(json.loads((path / "ai_responses.json").read_text())) == 3


def test_tracking_starts_strictly_after_publication(market, tmp_path):
    root, _ = market
    storage = ParquetStorage(root)
    days = sorted({x.trade_date for x in storage.load_trading_calendar() if x.is_open})
    code = storage.load_daily_bars_by_date(days[0])[0].instrument_id
    report = {
        "run_id": "test-historical-publication",
        "status": "live_research_unvalidated",
        "finished_at": f"{days[10]}T19:00:00+08:00",
        "baseline": [{"instrument_id": code}],
        "selection": {
            "selected": [
                {
                    "instrument_id": code,
                    "status": "focus",
                    "screening_status": "hold_for_official_notice_review",
                }
            ]
        },
    }
    run = tmp_path / "original"
    run.mkdir()
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    path = observe_run(run, root, tmp_path / "marks")
    observation = json.loads(path.read_text())
    first = observation["rows"][0]
    assert observation["groups"]["ai_focus"] == [code]
    assert observation["groups"]["ai_focus_without_notice_hold"] == []
    assert observation["groups"]["official_notice_hold"] == [code]
    assert first["anchor_session"] == days[11].isoformat()
    assert first["target_session"] == days[12].isoformat()
    assert first["status"] == "observed"
    anchor = storage.load_daily_bars_by_date(days[11])[0].close
    target = storage.load_daily_bars_by_date(days[12])[0].close
    assert first["adjusted_close_return"] == pytest.approx(target / anchor - 1)


def test_tracking_distinguishes_future_and_missing_prices(market, tmp_path):
    root, _ = market
    storage = ParquetStorage(root)
    days = sorted({x.trade_date for x in storage.load_trading_calendar() if x.is_open})
    code = storage.load_daily_bars_by_date(days[0])[0].instrument_id
    report = {
        "run_id": "test-future-observation",
        "status": "live_research_unvalidated",
        "finished_at": f"{days[10]}T19:00:00+08:00",
        "baseline": [{"instrument_id": code}],
        "selection": {"selected": []},
    }
    run = tmp_path / "source"
    run.mkdir()
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))

    def observe_at(day, hour):
        fixed = datetime.fromisoformat(f"{day}T{hour:02d}:00:00+08:00")

        class FrozenClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        with patch("quantlab.scout.tracking.datetime", FrozenClock):
            path = observe_run(run, root, tmp_path / "marks")
        return json.loads(path.read_text())["rows"][0]

    before_anchor = observe_at(days[11], 12)
    assert before_anchor["status"] == "anchor_not_yet_due"
    assert before_anchor["adjusted_close_return"] is None
    after_anchor = observe_at(days[11], 19)
    assert after_anchor["status"] == "target_not_yet_due"
    assert after_anchor["anchor_price_status"] == "available"
    assert after_anchor["target_price_status"] == "not_yet_due"
    storage.adj_factor_path(days[11]).unlink()
    missing_anchor = observe_at(days[12], 19)
    assert missing_anchor["status"] == "anchor_price_or_factor_missing"
    assert missing_anchor["adjusted_close_return"] is None


def test_ai_schema_payload_and_citation_validation(monkeypatch):
    from quantlab.scout.ai import DISCOVERY_SCHEMA, OpenAIResearch

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    raw = {
        "status": "completed",
        "usage": {"total_tokens": 10},
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://example.org"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": '{"hypotheses": []}'}]},
        ],
    }
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return json.dumps(raw).encode()

    def fetch(request, **kwargs):
        captured.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    client = OpenAIResearch("test-model")
    result, _ = client.ask("input", DISCOVERY_SCHEMA, search=True)
    assert result == {"hypotheses": []}
    assert captured[0]["store"] is False
    assert captured[0]["tool_choice"] == "required"
    assert captured[0]["max_tool_calls"] == 5
    raw["status"] = "incomplete"
    with pytest.raises(ValueError, match="incomplete"):
        client.ask("input", DISCOVERY_SCHEMA)
