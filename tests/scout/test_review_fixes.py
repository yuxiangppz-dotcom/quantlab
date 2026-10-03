"""Regression cases for the independent fdfb54b review, using only synthetic inputs."""

from datetime import date, datetime

import pytest

from quantlab.scout.facts import claim_fact_id, extract_core_claims, program_facts
from quantlab.scout.models import SHANGHAI, Candidate, Evidence
from quantlab.scout.opportunities import event_records, nominal_contract_scale
from quantlab.scout.opportunity_ai import validate_comparisons
from quantlab.scout.opportunity_demo import synthetic_comparisons

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
