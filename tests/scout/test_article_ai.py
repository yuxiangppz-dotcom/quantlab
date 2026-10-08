import copy
import json

import pytest

from quantlab.scout.article_ai import DIMENSIONS, ArticleAI, validate_reviews


def sample():
    mapped = {
        "group_event": "group",
        "setup": "setup",
        "lhb": "lhb",
        "funds": "moneyflow",
        "levels": "levels",
        "intraday": "recent_chart",
        "peers": "metrics",
    }
    facts = [
        {"fact_id": key, "subject": "600001.SH", "dimension": dimension}
        for key, dimension in mapped.items()
    ]
    research = {
        "deep": [{"ts_code": "600001.SH", "peer_codes": [], "fact_ids": list(mapped)}],
        "facts": facts,
        "charts": {
            "600001.SH": {
                "image_ids": [],
                "visible_dates": [],
                "chart_status": "unavailable",
                "images": [],
            }
        },
    }
    review = {
        "ts_code": "600001.SH",
        "decision": "watch",
        "rationale": "等待量价进一步确认",
        "next_day_hypothesis": "观察板块延续和回踩后的承接恢复",
        "strongest_counter": "目前缺少实际分时图证据",
        "peer_codes": [],
        "dimensions": {
            key: {
                "support": "形态事实",
                "counter": "",
                "unknown": "图像缺失",
                "effect": "unknown",
                "fact_ids": [key],
            }
            for key in DIMENSIONS
        },
        "intraday": {"intraday_quality": "unknown", "image_ids": [], "per_day_findings": []},
    }
    return research, {"reviews": [review]}


def test_malformed_mechanism_collects_errors_without_crashing():
    research, result = sample()
    result["reviews"][0]["decision"] = "priority"
    result["reviews"][0]["next_day_hypothesis"] = []
    assert any(
        error["code"] == "specific_next_day_mechanism_required"
        for error in validate_reviews(result, research)
    )


def test_qualified_peer_funds_cannot_be_this_stock_funds():
    research, result = sample()
    research["facts"].append(
        {"fact_id": "peer", "subject": "600999.SH", "dimension": "qualified_peer"}
    )
    research["deep"][0]["fact_ids"].append("peer")
    result["reviews"][0]["dimensions"]["funds"]["fact_ids"] = ["peer"]
    assert any(
        error["code"] == "fact_subject_or_dimension_conflict"
        for error in validate_reviews(result, research)
    )


def test_complete_error_list_and_unknown_images():
    research, result = sample()
    row = result["reviews"][0]
    row["decision"] = "priority"
    row["next_day_hypothesis"] = "待验证"
    row["dimensions"]["funds"]["fact_ids"] = ["wrong"]
    row["intraday"] = {
        "intraday_quality": "good",
        "image_ids": ["wrong"],
        "per_day_findings": [{"date": "2026-10-08"}],
    }
    codes = {error["code"] for error in validate_reviews(result, research)}
    assert {
        "specific_next_day_mechanism_required",
        "unknown_fact",
        "unknown_chart_reference",
        "no_image_no_visual_claim",
        "unseen_chart_date",
    } <= codes


def test_known_global_fact_cannot_be_claimed_as_this_stock():
    research, result = sample()
    research["facts"].append({"fact_id": "another-stock"})
    result["reviews"][0]["dimensions"]["funds"]["fact_ids"] = ["another-stock"]
    assert any(row["code"] == "unknown_fact" for row in validate_reviews(result, research))


def test_budget_precedes_network_and_preserves_intent(tmp_path):
    research, result = sample()
    response = {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
        "usage": {"total_tokens": 100},
    }
    calls = []
    client = ArticleAI(tmp_path, transport=lambda payload: calls.append(payload) or response)
    assert client.review(research)["calls"] == 1
    assert len(calls) == 1
    assert (tmp_path / "call-1.intent.json").is_file()
    with pytest.raises(ValueError):
        ArticleAI(
            tmp_path / "tiny",
            budget={**client.budget, "text_chars": 1},
            transport=lambda _: pytest.fail("must not send"),
        ).ask(research)


def test_single_compact_correction_then_retain_valid_peers(tmp_path):
    research, valid = sample()
    invalid = copy.deepcopy(valid)
    invalid["reviews"][0]["dimensions"].pop("funds")

    def transport(payload):
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(invalid)}}],
            "usage": {"total_tokens": 100},
        }

    result = ArticleAI(tmp_path, transport=transport).review(research)
    assert result["calls"] == 2
    assert result["reviews"] == []
    assert result["errors"]
    assert not (tmp_path / "call-3.intent.json").exists()


def test_uncertain_paid_delivery_not_automatically_retried(tmp_path):
    research, _ = sample()
    calls = []

    def transport(payload):
        calls.append(payload)
        raise TimeoutError()

    client = ArticleAI(tmp_path, transport=transport)
    with pytest.raises(ValueError):
        client.review(research)
    assert len(calls) == 1
    assert (
        json.loads((tmp_path / "call-1.receipt.json").read_text())["status"] == "unknown_or_failed"
    )


def test_actual_images_are_sent_as_native_multimodal_blocks(tmp_path):
    research, result = sample()
    image = {"image_id": "actual-sha", "data_url": "data:image/jpeg;base64,REAL_ORIGINAL_BYTES"}
    research["charts"]["600001.SH"]["images"] = [image]
    calls = []
    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
    client = ArticleAI(tmp_path, transport=lambda payload: calls.append(payload) or response)
    client.ask(research)
    images = [item for item in calls[0]["messages"][1]["content"] if item["type"] == "image_url"]
    assert images[0]["image_url"]["url"] == image["data_url"]
