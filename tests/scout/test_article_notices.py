from copy import deepcopy
from dataclasses import asdict

import pytest

from quantlab.scout.article_notices import (
    NOTICE_CONFIG,
    classify_notice_risk,
    collect_article_notices,
)
from quantlab.scout.models import Coverage, Evidence

CUTOFF = "2026-10-08T08:45:00+08:00"
SEEN = "2026-10-08T08:00:00+08:00"
LATE = "2026-10-08T09:00:00+08:00"
CODE = "600001.SH"


def index(code=CODE, number="10001", title="股东减持计划公告", seen=SEEN, day="2026-10-07"):
    return Evidence(
        source="cninfo:official_index",
        title=title,
        body="索引仅提供标题和PDF链接，正文未读取。",
        url=f"https://static.cninfo.com.cn/finalpage/{day}/{number}.PDF",
        published_at=None,
        retrieved_at=seen,
        kind="official_announcement_index_unverified",
        instrument_ids=(code,),
        event_dates=(day,),
    )


def body(item, *, seen=SEEN, text=None):
    return Evidence(
        source="cninfo:official_pdf_text",
        title=item.title + "（机器提取正文）",
        body=text
        or ("公告正文由机器提取，未人工核实；股东减持计划尚需结合主体、期间与条件审查。" * 5),
        url=item.url,
        published_at=None,
        retrieved_at=seen,
        kind="official_pdf_text_unverified",
        instrument_ids=item.instrument_ids,
        event_dates=item.event_dates,
    )


def callbacks(items, *, bodies=None, index_status="targeted_only", pdf_status="targeted_only"):
    calls = {"index": [], "pdf": []}

    def collector(config, cutoff, online, codes, *, max_targets):
        calls["index"].append((deepcopy(config), cutoff, online, list(codes), max_targets))
        return items, Coverage("official_index", index_status, len(items))

    def reader(config, cutoff, online, selected, *, max_stocks):
        calls["pdf"].append((deepcopy(config), cutoff, online, list(selected), max_stocks))
        result = [body(item) for item in selected] if bodies is None else bodies
        return result, Coverage("official_pdf", pdf_status, len(result))

    return collector, reader, calls


def collect(tmp_path, items, *, codes=None, **kwargs):
    collector, reader, calls = callbacks(items, **kwargs)
    result = collect_article_notices(
        tmp_path, codes or [CODE], CUTOFF, collector=collector, reader=reader
    )
    return result, calls


def test_targeted_pdf_preserves_title_ids_and_machine_uncertainty(tmp_path):
    original = index()
    pack, calls = collect(tmp_path, [original])
    row = pack["records"]["announcements"][0]
    assert row["title"] == original.title
    assert row["body_extracted"] is True and row["index_only"] is False
    assert row["date_uncertainty"] is True
    assert row["machine_text_not_independently_verified"] is True
    assert row["novelty_status"] == "not_inferred_from_first_download"
    assert original.evidence_id in row["source_ids"]
    assert row["_source_id"] != original.evidence_id
    assert row["risk_flags"][0]["action"] == "watch_only"
    assert row["risk_flags"][0]["confirmed_severity"] is False
    assert row["risk_flags"][0]["does_not_prove_all_future_plans_covered"] is True
    assert pack["coverage"][0]["status"] == "available"
    assert pack["coverage"][0]["complete_risk_universe"] is False
    assert pack["costs"]["model_calls"] == 0
    assert pack["costs"]["actual_network_request_count"] is None
    assert "股票交易风险正文补查" in calls["pdf"][0][3][0].title
    assert calls["index"][0][0]["_source_cache_root"] == str(tmp_path / "article_notice_cache")
    assert NOTICE_CONFIG["max_index_pages_per_stock"] == 2
    assert NOTICE_CONFIG["max_pdf_pages"] == 12


def test_only_one_pdf_per_stock_and_unread_important_index_is_specific_gap(tmp_path):
    older = index(number="1", day="2026-10-06", title="监管问询函")
    newer = index(number="2", title="解除限售股份上市流通公告")
    pack, calls = collect(tmp_path, [older, newer])
    selected = calls["pdf"][0][3]
    assert len(selected) == 1 and selected[0].url == newer.url
    rows = {row["url"]: row for row in pack["records"]["announcements"]}
    assert rows[older.url]["index_only"] is True
    assert rows[older.url]["risk_flags"][0]["action"] == "review_required"
    assert rows[newer.url]["body_extracted"] is True
    assert pack["coverage"][0]["status"] == "partial"
    assert pack["coverage"][0]["reason_code"] == "risk_body_missing"
    assert pack["coverage"][0]["important_index_without_body"] == [older.evidence_id]


def test_risk_index_has_priority_over_more_recent_unrelated_title(tmp_path):
    older = index(number="1", day="2026-10-06", title="关于收到调查通知书的公告")
    newer = index(number="2", title="关于参加投资者交流会议的公告")
    _, calls = collect(tmp_path, [newer, older])
    assert calls["pdf"][0][3][0].url == older.url


@pytest.mark.parametrize("title", ["", "  ", None])
def test_empty_important_title_is_unknown_not_clearance(tmp_path, title):
    value = asdict(index())
    value["title"] = title
    pack, calls = collect(tmp_path, [value])
    assert pack["records"]["announcements"] == []
    assert pack["invalid_record_count"] == 1
    assert pack["coverage"][0]["status"] == "unknown"
    assert calls["pdf"] == []


def test_index_after_cutoff_is_audit_only_and_does_not_reach_reader(tmp_path):
    pack, calls = collect(tmp_path, [index(seen=LATE)])
    assert pack["records"]["announcements"] == []
    assert len(pack["rejected_after_cutoff"]) == 1
    assert pack["coverage"][0]["status"] == "unknown"
    assert calls["pdf"] == []


def test_body_after_cutoff_leaves_index_gap_and_never_model_body(tmp_path):
    original = index()
    pack, _ = collect(tmp_path, [original], bodies=[body(original, seen=LATE)])
    row = pack["records"]["announcements"][0]
    assert row["body"] == "" and row["index_only"] is True
    assert row["risk_flags"][0]["action"] == "review_required"
    assert pack["coverage"][0]["status"] == "partial"
    assert len(pack["rejected_after_cutoff"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("instrument_ids", ("600999.SH",)),
        ("url", "https://static.cninfo.com.cn/finalpage/2026-10-07/20000.PDF"),
        ("event_dates", ("2026-10-06",)),
        ("source", "unverified:copy"),
        ("published_at", "2026-10-08T08:30:00+08:00"),
    ],
)
def test_body_cannot_borrow_subject_document_date_or_time(tmp_path, field, value):
    original = index()
    extracted = asdict(body(original))
    extracted[field] = value
    pack, _ = collect(tmp_path, [original], bodies=[extracted])
    assert pack["records"]["announcements"][0]["body"] == ""
    assert pack["invalid_record_count"] == 1
    assert pack["coverage"][0]["status"] == "partial"


def test_reader_cannot_expand_chosen_budget_or_overwrite_duplicate_body(tmp_path):
    older = index(number="1", day="2026-10-06")
    newer = index(number="2")
    pack, _ = collect(tmp_path, [older, newer], bodies=[body(older), body(newer), body(newer)])
    rows = {row["url"]: row for row in pack["records"]["announcements"]}
    assert rows[older.url]["body_extracted"] is False
    assert rows[newer.url]["body_extracted"] is True
    assert pack["invalid_record_count"] == 2


def test_exact_duplicate_index_deduplicates_but_contradiction_cannot_override(tmp_path):
    original = index()
    other_subject = index(code="600002.SH")
    pack, _ = collect(tmp_path, [original, original, other_subject], codes=[CODE, "600002.SH"])
    assert len(pack["records"]["announcements"]) == 1
    assert pack["records"]["announcements"][0]["ts_code"] == CODE
    assert pack["invalid_record_count"] == 1


def test_earliest_index_and_body_receipts_survive_later_retrieval(tmp_path):
    first, _ = collect(tmp_path, [index()])
    later = index(seen="2026-10-08T08:30:00+08:00")
    collector, reader, _ = callbacks([later])
    second = collect_article_notices(tmp_path, [CODE], CUTOFF, collector=collector, reader=reader)
    row = second["records"]["announcements"][0]
    assert row["_first_seen_at"] == SEEN
    assert row["body_first_seen_at"] == SEEN
    assert row["_source_id"] == first["records"]["announcements"][0]["_source_id"]
    assert len(list((tmp_path / "article_notice_cache" / "collections").glob("*.json"))) == 1


def test_empty_official_index_does_not_claim_no_risk(tmp_path):
    pack, calls = collect(tmp_path, [], index_status="empty_unconfirmed")
    assert pack["coverage"][0]["status"] == "unknown"
    assert pack["no_complete_future_risk_clearance_claim"] is True
    assert calls["pdf"] == []


def test_failed_collector_is_counted_bounded_and_does_not_call_reader(tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("This fixture never makes a network call")

    pack = collect_article_notices(tmp_path, [CODE], CUTOFF, collector=broken, reader=broken)
    assert pack["coverage"][0]["reason_code"] == "official_index_failed"
    assert pack["coverage"][0]["error_type"] == "RuntimeError"
    assert pack["costs"]["index_collector_calls"] == 1
    assert pack["costs"]["pdf_reader_calls"] == 0


def test_reader_failure_preserves_actual_index_and_gap(tmp_path):
    collector, _, _ = callbacks([index()])

    def broken(*args, **kwargs):
        raise RuntimeError("Not a provider call")

    pack = collect_article_notices(tmp_path, [CODE], CUTOFF, collector=collector, reader=broken)
    assert pack["records"]["announcements"][0]["index_only"] is True
    assert pack["coverage"][0]["status"] == "partial"
    assert pack["coverage"][0]["pdf_status"] == "failed"
    assert pack["costs"]["pdf_reader_calls"] == 1


def test_fixed_stock_budget_has_explicit_overflow_and_network_ceiling(tmp_path):
    root = tmp_path / "scout_article_offline_bounded_test"
    root.mkdir()
    (root / ".scout-article").write_text("quantlab-scout-article-v1")
    codes = [f"{600000 + number:06}.SH" for number in range(25)]
    items = [index(code=code, number=str(number + 1)) for number, code in enumerate(codes[:24])]
    collector, reader, calls = callbacks(items)
    pack = collect_article_notices(
        root, codes, CUTOFF, online=True, collector=collector, reader=reader
    )
    assert len(calls["index"][0][3]) == len(calls["pdf"][0][3]) == 24
    assert pack["coverage"][0]["ts_code"] == codes[-1]
    assert pack["coverage"][0]["status"] == "unknown"
    assert pack["coverage"][0]["reason_code"] == "frozen_notice_stock_budget_exhausted"
    assert pack["costs"]["index_network_requests_upper_bound"] == 49
    assert pack["costs"]["pdf_network_requests_upper_bound"] == 24
    assert pack["costs"]["actual_network_request_count"] is None


@pytest.mark.parametrize(
    "config",
    [
        {"_source_cache_root": "/unsafe/shared/path"},
        {"max_pdf_pages": 13},
        {"lookback_hours": 169},
        {"cninfo_announcements": "true"},
        {"_source_window_start": LATE},
    ],
)
def test_configuration_rejects_budget_or_cache_change(tmp_path, config):
    with pytest.raises(ValueError):
        collect_article_notices(tmp_path, [CODE], CUTOFF, config=config)


def test_online_requires_isolated_root_before_callback(tmp_path):
    collector, reader, calls = callbacks([index()])
    with pytest.raises(ValueError, match="isolated"):
        collect_article_notices(
            tmp_path, [CODE], CUTOFF, online=True, collector=collector, reader=reader
        )
    assert calls["index"] == []


def test_nested_receipt_symlink_cannot_write_outside_isolated_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "isolated"
    cache = root / "article_notice_cache"
    cache.mkdir(parents=True)
    (cache / "first_seen").symlink_to(outside, target_is_directory=True)
    pack, _ = collect(root, [index()])
    assert pack["invalid_record_count"] == 1
    assert pack["coverage"][0]["status"] == "unknown"
    assert list(outside.iterdir()) == []


def test_title_or_machine_text_alone_never_hard_excludes():
    for title in ["业绩预亏公告", "收到问询函", "立案通知", "减持计划", "股份解禁", "股票停牌公告"]:
        assert all(
            flag["action"] != "exclude" for flag in classify_notice_risk(title, "", index_only=True)
        )
        assert all(
            flag["action"] != "exclude"
            for flag in classify_notice_risk(title, title * 30, index_only=False)
        )


def confirmed_receipt():
    return {
        "confirmed_risk": True,
        "subject_verified": True,
        "scope_verified": True,
        "effective_period_verified": True,
        "source_verified": True,
        "source_id": "official-exchange-separate-verified-receipt",
        "ts_code": CODE,
        "scope": {"ts_code": CODE, "qualification": "suspended"},
        "effective_period": {"start": "2026-10-08", "end": "2026-10-09"},
        "qualification_constraint": "suspended",
    }


def test_confirmed_qualification_requires_actual_subject_scope_and_effective_period():
    receipt = confirmed_receipt()
    flags = classify_notice_risk(
        "停牌公告",
        "",
        index_only=True,
        confirmation=receipt,
        expected_code=CODE,
        effective_date="2026-10-08",
    )
    assert flags[-1]["action"] == "exclude"
    for changes in [
        {"scope_verified": False},
        {"scope": None},
        {"effective_period": None},
        {"ts_code": "600999.SH"},
        {"source_id": ""},
        {"scope": {"ts_code": CODE, "qualification": "unclear"}},
        {"qualification_constraint": []},
        {"effective_period": {"start": "2026-10-09", "end": "2026-10-10"}},
        {"effective_period": {"start": "2026-10-01", "end": "2026-10-07"}},
    ]:
        flags = classify_notice_risk(
            "停牌公告",
            "",
            index_only=True,
            confirmation={**receipt, **changes},
            expected_code=CODE,
            effective_date="2026-10-08",
        )
        assert flags[-1]["action"] == "review_required"
        assert flags[-1]["confirmed_severity"] is False


@pytest.mark.parametrize("confirmation", ["verified", ["source_id"], 1])
def test_malformed_qualification_is_bounded_unknown_not_an_exception(confirmation):
    flags = classify_notice_risk(
        "停牌公告",
        "",
        index_only=True,
        confirmation=confirmation,
        expected_code=CODE,
        effective_date="2026-10-08",
    )
    assert flags[-1]["action"] == "review_required"
    assert flags[-1]["reason_code"] == "qualification_receipt_malformed"


def test_default_offline_path_has_no_provider_evidence(tmp_path):
    pack = collect_article_notices(tmp_path, [CODE], CUTOFF)
    assert pack["records"]["announcements"] == []
    assert pack["coverage"][0]["status"] == "unknown"
    assert pack["coverage"][0]["index_status"] == "disabled"
    assert pack["costs"]["index_network_requests_upper_bound"] == 0
