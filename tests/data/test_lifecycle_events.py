from datetime import date

from quantlab.data.lifecycle_events import (
    VERIFICATION_REVIEW_REQUIRED,
    classify_announcement,
    events_available_as_of,
    events_to_facts,
    golden_event_audit,
    normalize_lifecycle_events,
    termination_decisions_available_as_of,
)
from quantlab.data.models import RawLifecycleAnnouncement
from quantlab.data.storage import ParquetStorage


def _announcement(**overrides) -> RawLifecycleAnnouncement:
    values = {
        "source": "tushare.anns_d",
        "source_record_id": "row-1",
        "instrument_id": "002509.SZ",
        "announcement_date": date(2020, 5, 15),
        "announcement_time": "15:30:00",
        "title": "关于公司股票终止上市决定的公告",
        "source_url": "https://example.test/a",
        "raw_payload": '{"title":"x"}',
        "content_fingerprint": "a" * 64,
    }
    values.update(overrides)
    return RawLifecycleAnnouncement(**values)


def _calendar():
    return [
        (date(2020, 5, 15), True),
        (date(2020, 5, 16), False),
        (date(2020, 5, 17), False),
        (date(2020, 5, 18), True),
    ]


def test_title_classifier_trusts_only_clear_formal_decision() -> None:
    event = normalize_lifecycle_events([_announcement()], _calendar())[0]
    assert event.event_type == "termination_decision"
    assert event.verification_status == "trusted"
    assert event.available_from == date(2020, 5, 18)
    assert "next_open_session" in event.classification_reason


def test_ambiguous_chinese_title_requires_review_and_never_triggers() -> None:
    raw = _announcement(title="关于公司股票可能终止上市的风险提示公告")
    classified = classify_announcement(raw)
    assert classified.verification_status == VERIFICATION_REVIEW_REQUIRED
    assert normalize_lifecycle_events([raw], _calendar()) == []


def test_event_id_and_pit_query_are_deterministic() -> None:
    first = normalize_lifecycle_events([_announcement()], _calendar())
    second = normalize_lifecycle_events([_announcement()], _calendar())
    assert first == second
    assert events_available_as_of(first, date(2020, 5, 15)) == []
    assert termination_decisions_available_as_of(first, date(2020, 5, 18))["002509.SZ"] == first[0]


def test_legacy_risk_adapter_preserves_available_from() -> None:
    event = normalize_lifecycle_events([_announcement()], _calendar())[0]
    facts = events_to_facts([event])
    fact = facts["002509.SZ"]["facts"][0]
    assert fact["available_from"] == "2020-05-18"
    assert fact["fact_type"] == "termination_decision"


def test_golden_audit_reports_missing_without_special_casing() -> None:
    manual = {
        "002509.SZ": {
            "facts": [{
                "fact_type": "termination_decision",
                "fact_id": "m",
                "document_date": "2020-05-15",
            }]
        }
    }
    assert golden_event_audit(manual, []) == [
        {
            "instrument_id": "002509.SZ", "manual_fact_id": "m", "systematic_event_id": None,
            "status": "missing", "manual_date": "2020-05-15", "systematic_date": None,
            "available_from": None, "classification": None, "source_url": None,
        }
    ]


def test_raw_and_canonical_event_storage_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    announcement = _announcement()
    storage.save_lifecycle_announcements_by_date([announcement], announcement.announcement_date)
    assert storage.load_lifecycle_announcements_by_date(
        announcement.announcement_date
    ) == [announcement]
    event = normalize_lifecycle_events([announcement], _calendar())[0]
    storage.save_lifecycle_events([event])
    assert storage.load_lifecycle_events() == [event]
