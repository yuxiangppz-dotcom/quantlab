from copy import deepcopy
from datetime import datetime

import pytest

from quantlab.scout.article_review import (
    DIMENSIONS,
    MODE,
    bind_review,
    research_hash,
    validate_review,
)
from quantlab.scout.models import SHANGHAI


def sample():
    research = {
        "signal_date": "2026-10-08",
        "deep": [
            {"ts_code": "600642.SH", "peer_codes": [], "fact_ids": ["own-fact"]},
        ],
        "facts": [],
    }
    row = {
        "ts_code": "600642.SH",
        "decision": "watch",
        "peer_codes": [],
        "rationale": "仅作形态观察。",
        "next_day_hypothesis": "等待突破位回踩后的日线确认。",
        "strongest_counter": "方向持续性尚未积累。",
        "dimensions": {
            key: {
                "support": "",
                "counter": "",
                "unknown": "待核查",
                "effect": "unknown",
                "fact_ids": [],
            }
            for key in DIMENSIONS
        },
        "daily_review": {
            "source_url": "https://www.xueqiu.com/S/SH600642",
            "observed_code": "600642.SH",
            "view": "daily",
            "status": "partial",
            "visible_dates": ["2026-10-08"],
            "observed_at": "2026-10-08T20:00:00+08:00",
        },
    }
    document = {
        "review_mode": MODE,
        "reviewer": "main_agent",
        "research_hash": research_hash(research),
        "reviews": [row],
    }
    return research, document


def test_partial_daily_can_be_frozen_as_unknown_without_external_model():
    research, doc = sample()
    result = bind_review(doc, research)
    assert result["external_model_calls"] == result["calls"] == result["tokens"] == 0


def test_complete_error_list_catches_wrong_subject_peer_fact_and_future_daily():
    research, doc = sample()
    row = doc["reviews"][0]
    row["peer_codes"] = ["000001.SZ"]
    row["dimensions"]["funds"]["fact_ids"] = ["other-stock-fact"]
    row["daily_review"]["observed_code"] = "000001.SZ"
    row["daily_review"]["visible_dates"] = ["2026-10-09"]
    errors = validate_review(doc, research, now=datetime(2026, 10, 8, 21, tzinfo=SHANGHAI))
    assert {r["code"] for r in errors} >= {
        "unqualified_peer",
        "fact_scope",
        "daily_subject_or_view",
        "daily_future_or_invalid_date",
    }


def test_incomplete_daily_does_not_count_as_support():
    research, doc = sample()
    doc["reviews"][0]["dimensions"]["daily_visual"]["effect"] = "retain"
    assert "incomplete_daily_not_support" in {r["code"] for r in validate_review(doc, research)}


def test_review_cannot_be_reused_on_changed_facts():
    research, doc = sample()
    changed = deepcopy(research)
    changed["facts"] = [
        {"fact_id": "new-fact", "value": "new", "subject": "600642.SH", "dimension": "metrics"}
    ]
    with pytest.raises(ValueError, match="research_changed"):
        bind_review(doc, changed)


def test_peer_funds_cannot_be_borrowed_for_this_stock():
    research, doc = sample()
    research["facts"] = [
        {"fact_id": "own-fact", "subject": "000001.SZ", "dimension": "moneyflow", "value": {}}
    ]
    doc["reviews"][0]["dimensions"]["funds"]["fact_ids"] = ["own-fact"]
    doc["research_hash"] = research_hash(research)
    assert "fact_subject" in {r["code"] for r in validate_review(doc, research)}


def test_unreadable_daily_cannot_supply_positive_support_or_quality():
    research, doc = sample()
    row = doc["reviews"][0]
    row["daily_review"].update(status="unavailable", quality="good")
    row["dimensions"]["daily_visual"]["support"] = "走势良好"
    assert "incomplete_daily_not_support" in {r["code"] for r in validate_review(doc, research)}


def test_old_observation_and_empty_date_are_both_errors():
    research, doc = sample()
    research["cutoff_at"] = "2026-10-08T19:00:00+08:00"
    doc["research_hash"] = research_hash(research)
    doc["reviews"][0]["daily_review"].update(
        status="complete", visible_dates=[""], observed_at="2020-01-01T00:00:00+08:00"
    )
    assert {r["code"] for r in validate_review(doc, research)} >= {
        "daily_future_or_invalid_date",
        "daily_dates_unknown",
        "daily_observation_time",
    }


def test_malformed_visual_dimension_returns_errors_instead_of_crashing():
    research, doc = sample()
    doc["reviews"][0]["dimensions"]["daily_visual"] = []
    assert "dimension_missing" in {r["code"] for r in validate_review(doc, research)}


@pytest.mark.parametrize("value", [None, [], "bad", {"reviews": None}])
def test_malformed_document_is_bounded(value):
    research, _ = sample()
    assert validate_review(value, research)
