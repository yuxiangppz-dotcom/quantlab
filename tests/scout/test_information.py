"""Disclosure windows, source failures, comment sampling, and evidence integration."""

import json
from datetime import date, datetime
from unittest.mock import patch

import pandas as pd
import pytest

from quantlab.scout.ai import validate_selection
from quantlab.scout.demo import make_demo_market, make_demo_sources
from quantlab.scout.disclosures import (
    collect_disclosures,
    disclosure_context,
    normalize_snapshot,
    normalized_rows,
)
from quantlab.scout.discussion import load_comments
from quantlab.scout.market import scan_market
from quantlab.scout.models import SHANGHAI, Evidence
from quantlab.scout.pipeline import DEFAULT_CONFIG, evidence_packet, read_config, run_scout

DAY = date(2026, 1, 9)
NOW = datetime(2026, 1, 10, 12, tzinfo=SHANGHAI)
CODE = "600001.SH"


def seat(**overrides):
    return {
        "ts_code": CODE,
        "trade_date": "20260109",
        "reason": "单日偏离值",
        "exalter": "机构专用",
        "side": "0",
        "buy": 2000000,
        "sell": 1000000,
        "net_buy": 1000000,
        **overrides,
    }


def snapshot(dataset="top_inst", rows=None, **overrides):
    raw = {
        "dataset": dataset,
        "trade_date": DAY.isoformat(),
        "retrieved_at": NOW.isoformat(),
        "published_at": None,
        "rows": [seat()] if rows is None else rows,
        **overrides,
    }
    return raw


def normalized(raw):
    return normalize_snapshot(raw, [DAY], NOW) | {"origin": "user_import_unverified"}


def comment_document(**overrides):
    return {
        "schema_version": 1,
        "source": "用户导出的同花顺样本",
        "sampling_method": "manual_export",
        "retrieved_at": NOW.isoformat(),
        "window_start": "2026-01-10T09:00:00+08:00",
        "window_end": NOW.isoformat(),
        "comments": [
            {
                "comment_id": "1",
                "instrument_id": CODE,
                "text": "待核实的一项合作",
                "published_at": "2026-01-10T10:00:00+08:00",
                "author_id": "private-handle-a",
            },
            {
                "comment_id": "2",
                "instrument_id": CODE,
                "text": "待核实的一项合作",
                "published_at": "2026-01-10T11:00:00+08:00",
            },
        ],
        **overrides,
    }


def write_json(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def test_rank_sides_merge_but_different_windows_do_not_sum():
    raw = snapshot(rows=[seat(), seat(side="1"), seat(reason="连续三个交易日累计偏离值")])
    evidence, contexts = disclosure_context([normalized(raw)], {CODE: None}, {})
    records = contexts[CODE]["top_inst"]["records"]
    assert len(records) == 2
    assert records[0]["rank_sides"] == ["0", "1"]
    assert records[0]["buy"] == 2000000
    assert records[1]["reason"] == "连续三个交易日累计偏离值"
    assert evidence[0].published_at is None
    assert evidence[0].event_dates == (DAY.isoformat(),)
    assert contexts[CODE]["top_inst"]["duplicate_or_indistinguishable_rows"] == 1


def test_identical_block_rows_are_not_assumed_duplicate_transactions():
    row = {
        "ts_code": CODE,
        "trade_date": "20260109",
        "price": 9,
        "vol": 10,
        "amount": 90,
        "buyer": "席位甲",
        "seller": "席位乙",
    }
    snap = normalized(snapshot("block_trade", [row, row]))
    _, contexts = disclosure_context([snap], {CODE: None}, {(CODE, DAY.isoformat()): 10})
    records = contexts[CODE]["block_trade"]["records"]
    assert len(records) == 2
    assert records[0]["volume_shares"] == 100000
    assert records[0]["derived_notional_cny"] == 900000
    assert records[0]["premium_to_close_pct"] == pytest.approx(-10)
    assert records[0]["amount"] == 90  # Unknown provider amount unit stays raw.
    _, missing = disclosure_context([snap], {CODE: None}, {})
    assert missing[CODE]["block_trade"]["records"][0]["premium_to_close_pct"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"retrieved_at": "2026-01-11T12:00:00+08:00"},
        {"retrieved_at": "2026-01-08T12:00:00+08:00"},
        {"published_at": "2026-01-11T12:00:00+08:00"},
        {"trade_date": "2026-01-08"},
        {"retrieved_at": "2026-01-10T12:00:00"},
    ],
)
def test_disclosure_rejects_inconsistent_timing(change):
    with pytest.raises(ValueError):
        normalize_snapshot(snapshot(**change), [DAY], NOW)


@pytest.mark.parametrize(
    "row", [seat(trade_date="20260108"), seat(side="9"), seat(buy=-1), seat(buy=float("inf"))]
)
def test_invalid_rows_fail_entire_snapshot(row):
    with pytest.raises(ValueError):
        normalized_rows("top_inst", [seat(), row], DAY)


def test_provider_failure_empty_and_truncation_are_distinct(monkeypatch):
    class API:
        def top_list(self, **kwargs):
            assert kwargs == {"trade_date": "20260109"}
            raise RuntimeError("secret-token-never-echo")

        def top_inst(self, **kwargs):
            return pd.DataFrame([seat(), seat(side="1")])

        def block_trade(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setenv("TUSHARE_TOKEN", "test-only")
    monkeypatch.setattr("tushare.pro_api", lambda *args, **kwargs: API())
    monkeypatch.setattr("quantlab.scout.disclosures.ROW_LIMIT", 1)
    snapshots, coverage = collect_disclosures([DAY], online=True, enabled=True)
    assert {x.status for x in coverage} == {"failed", "possibly_truncated", "empty_unconfirmed"}
    assert len(snapshots) == 2
    assert "secret-token" not in repr(coverage)


def test_import_never_connects_and_omissions_stay_unknown(tmp_path):
    path = write_json(
        tmp_path, "disclosures.json", {"schema_version": 1, "snapshots": [snapshot()]}
    )
    with patch("tushare.pro_api", side_effect=AssertionError("network forbidden")):
        snapshots, coverage = collect_disclosures([DAY], True, True, path)
    assert snapshots[0]["origin"] == "user_import_unverified"
    assert sum(x.status == "not_provided" for x in coverage) == 2
    duplicate = write_json(
        tmp_path, "duplicate.json", {"schema_version": 1, "snapshots": [snapshot(), snapshot()]}
    )
    with pytest.raises(ValueError, match="Duplicate"):
        collect_disclosures([DAY], False, True, duplicate)


def test_provider_initialization_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-only")
    with patch("tushare.pro_api", side_effect=RuntimeError("secret-value")):
        snapshots, coverage = collect_disclosures([DAY], True, True)
    assert not snapshots
    assert coverage[0].status == "failed"
    assert "secret-value" not in repr(coverage)


def test_disabled_sources_never_initialize_provider(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-only")
    with patch("tushare.pro_api", side_effect=AssertionError("must not connect")):
        assert collect_disclosures([DAY], False, True)[1][0].status == "disabled"
        assert collect_disclosures([DAY], True, False)[1][0].status == "disabled"


def test_comment_sample_deduplication_and_author_redaction(tmp_path):
    data = comment_document()
    data["comments"].append(dict(data["comments"][0]))
    path = write_json(tmp_path, "comments.json", data)
    evidence, context, audit = load_comments(path, NOW, {CODE})
    assert len(evidence) == 1
    assert context[CODE]["sample_count"] == 2
    assert context[CODE]["repeated_text_fraction"] == 0.5
    assert context[CODE]["identified_author_count"] == 1
    assert context[CODE]["unknown_author_comments"] == 1
    assert context[CODE]["heat_growth"] is None
    assert context[CODE]["sentiment_score"] is None
    assert audit["duplicate_ids"] == 1
    assert "private-handle-a" not in json.dumps(audit)


def test_comment_outside_window_or_conflicting_id_rejected(tmp_path):
    data = comment_document()
    data["comments"][0]["published_at"] = "2026-01-10T13:00:00+08:00"
    path = write_json(tmp_path, "comments.json", data)
    with pytest.raises(ValueError, match="window"):
        load_comments(path, NOW, {CODE})
    data = comment_document()
    data["comments"][1]["comment_id"] = "1"
    path = write_json(tmp_path, "comments.json", data)
    with pytest.raises(ValueError, match="Conflicting"):
        load_comments(path, NOW, {CODE})


def test_discussion_only_focus_rejected():
    row = {
        "instrument_id": CODE,
        "status": "focus",
        "thesis": "未经核实的评论",
        "risk": "未知",
        "invalidation": "尚未核实",
        "evidence_ids": [f"market:{CODE}"],
    }
    result = {"market_view": "test", "selected": [row]}
    candidates = [{"instrument_id": CODE, "routes": ["信息关联:public_discussion"]}]
    with pytest.raises(ValueError, match="discussion-only"):
        validate_selection(result, candidates, [])
    row["status"] = "watch"
    assert validate_selection(result, candidates, []) == result


def test_model_evidence_budget_and_stock_scope():
    def item(code):
        return Evidence(
            "test", code, "x" * 5000, None, None, NOW.isoformat(), instrument_ids=(code,)
        )

    packet = evidence_packet([item(CODE), item("OTHER")], {CODE}, 4000)
    assert len(packet) == 1 and packet[0]["body_truncated"]
    assert len(json.dumps(packet[0], ensure_ascii=False)) <= 4000


def test_compact_disclosure_prompt_counts_both_sides_of_seat_sample():
    records = [
        {"trade_date": DAY.isoformat(), "reason": "累计偏离", "exalter": f"seat{i}", "net_buy": net}
        for i, net in enumerate([-7, -2, 3, 9])
    ]
    item = Evidence(
        "disclosure:top_inst",
        "seat sample",
        json.dumps({"dataset": "top_inst", "records": records}),
        None,
        None,
        NOW.isoformat(),
        kind="trading_disclosure",
        instrument_ids=(CODE,),
    )
    packet = evidence_packet([item], {CODE}, max_chars=4000, max_body_chars=1400)
    summary = json.loads(packet[0]["body"])
    assert packet[0]["body_compacted"]
    assert summary["record_count"] == 4
    assert summary["groups"][0]["positive_net_rows"] == 2
    assert summary["groups"][0]["negative_net_rows"] == 2
    assert {row["net_buy"] for row in summary["groups"][0]["largest_positive"]} == {3, 9}


def test_offline_supplements_reach_candidate_report_without_canonical_writes(tmp_path):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    disclosures, comments = make_demo_sources(tmp_path, day)
    before = {str(p): p.read_bytes() for p in canonical.rglob("*.parquet")}
    with patch("tushare.pro_api", side_effect=AssertionError("network forbidden")):
        run, report = run_scout(
            canonical,
            tmp_path / "runs",
            DEFAULT_CONFIG,
            session=day,
            disclosures_path=disclosures,
            comments_path=comments,
        )
    candidate = next(x for x in report["candidates"] if x["instrument_id"] == "600007.SH")
    assert set(candidate["context"]) == {"top_list", "top_inst", "block_trade", "discussion"}
    assert report["source_comparison"]["definition"].startswith("candidate discovery")
    assert report["selection"]["selected"] == []
    assert report["disclosure_snapshots"]
    assert "评论样本" in (run / "report.md").read_text()
    assert before == {str(p): p.read_bytes() for p in canonical.rglob("*.parquet")}
    universe, _ = scan_market(canonical, day)
    assert [x["score"] for x in report["baseline"]] == sorted(
        [x.score for x in universe.values()], reverse=True
    )[:3]


def test_live_mock_reads_new_evidence_and_keeps_calls_bounded(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    disclosures, comments = make_demo_sources(tmp_path, day)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = DEFAULT_CONFIG | {"model": "test-model", "tushare_news_sources": []}
    stages = []

    def ask(client, prompt, schema, search=False):
        client.calls.append({"search": search, "status": "completed"})
        stages.append(search)
        if search:
            if len(stages) == 2:
                assert '"trading_disclosure"' in prompt
                assert '"public_comment_unverified"' in prompt
            return {"hypotheses": []}, {
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": "https://example.org/source"}]},
                    }
                ]
            }
        packet = json.loads(prompt.split("\n", 1)[1])
        row = next(x for x in packet["candidates"] if x["instrument_id"] == "600007.SH")
        ref = row["context"]["top_inst"]["evidence_id"]
        return {
            "market_view": "样本存在分歧",
            "selected": [
                {
                    "instrument_id": row["instrument_id"],
                    "status": "watch",
                    "thesis": "披露待核实",
                    "risk": "样本有限",
                    "invalidation": "业务关系不成立",
                    "evidence_ids": [f"market:{row['instrument_id']}", ref],
                }
            ],
        }, {"status": "completed"}

    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.OpenAIResearch.ask", new=ask),
    ):
        _, report = run_scout(
            canonical,
            tmp_path / "runs",
            config,
            online=True,
            disclosures_path=disclosures,
            comments_path=comments,
        )
    assert stages == [True, True, False]
    assert report["status"] == "live_research_unvalidated"
    assert report["selection"]["selected"][0]["status"] == "watch"


def test_source_request_budget_rejects_large_history(tmp_path):
    path = write_json(tmp_path, "config.json", {"disclosure_sessions": 100})
    with pytest.raises(ValueError, match="disclosure_sessions"):
        read_config(path)
