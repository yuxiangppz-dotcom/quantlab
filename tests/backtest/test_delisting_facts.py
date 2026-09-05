from datetime import date
from pathlib import Path

from quantlab.backtest.delisting_facts import (
    facts_available_as_of,
    load_delisting_facts,
    source_coverage,
    trusted_facts_available_as_of,
    validate_facts,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_available_from_filters_future() -> None:
    facts = {
        "X": {
            "facts": [
                {"fact_type": "a", "content_verified": True,
                 "public_time_verified": True, "available_from": "2020-01-08"},
                {"fact_type": "b", "content_verified": True,
                 "public_time_verified": True, "available_from": None},
            ]
        }
    }
    assert facts_available_as_of(facts, "X", date(2020, 1, 7)) == []
    avail = facts_available_as_of(facts, "X", date(2020, 1, 8))
    assert [f["fact_type"] for f in avail] == ["a"]


def test_unknown_date_not_derived() -> None:
    facts = {"X": {"facts": [{"fact_type": "b", "available_from": None}]}}
    assert facts_available_as_of(facts, "X", date(2030, 1, 1)) == []


def test_source_coverage_requires_content_verified() -> None:
    facts = {
        "verified": {"facts": [{"content_verified": True}]},
        "unverified": {"facts": [{"content_verified": False}]},
        "empty": {"facts": []},
    }
    assert source_coverage(facts, "verified") == "verified"
    assert source_coverage(facts, "unverified") == "unknown"
    assert source_coverage(facts, "empty") == "unknown"
    assert source_coverage(facts, "absent") == "unknown"


def test_real_fixture_fact_types() -> None:
    facts = load_delisting_facts(PROJECT_ROOT / "config" / "delisting_facts.json")
    types = {f["fact_type"] for f in facts["000018.SZ"]["facts"]}
    assert "expected_last_trading_day" in types
    assert "confirmed_last_trading_day" in types
    assert "delisting" in types


def test_trusted_facts_exclude_unverified_time() -> None:
    facts = load_delisting_facts(PROJECT_ROOT / "config" / "delisting_facts.json")
    trusted = trusted_facts_available_as_of(facts, "000018.SZ", date(2020, 1, 3))
    types = {f["fact_type"] for f in trusted}
    # termination + arrangement have verified public time (SZSE page 2019-11-15)
    assert "termination_decision" in types
    assert "delisting_arrangement_start" in types
    # expected/confirmed/delisting have no verified public time -> excluded
    assert "expected_last_trading_day" not in types
    assert "confirmed_last_trading_day" not in types
    assert "delisting" not in types


def test_late_published_fact_not_available_early() -> None:
    facts = {
        "X": {
            "facts": [
                {"fact_type": "delisting", "content_verified": True,
                 "public_time_verified": True, "available_from": "2020-01-08"},
            ]
        }
    }
    assert facts_available_as_of(facts, "X", date(2020, 1, 3)) == []
    assert facts_available_as_of(facts, "X", date(2020, 1, 8)) != []


def _fact(content_verified=True, public_time_verified=True,
          publication_date="2026-01-09", available_from=None) -> dict:
    return {
        "fact_type": "t",
        "content_verified": content_verified,
        "public_time_verified": public_time_verified,
        "publication_date": publication_date,
        "available_from": available_from,
    }


def _calendar(*days) -> list:
    return [(d, True) for d in days]


def test_bool_types_validated() -> None:
    facts = {"X": {"facts": [{"fact_type": "t", "content_verified": "true",
                              "public_time_verified": True}]}}
    errors = validate_facts(facts, _calendar(date(2020, 1, 2), date(2020, 1, 3)))
    assert any("must be bool" in e for e in errors)


def _complete_calendar(start: date, end: date) -> list:
    from datetime import timedelta

    out = []
    d = start
    while d <= end:
        out.append((d, d.weekday() < 5))  # weekdays open, weekends closed
        d += timedelta(days=1)
    return out


def test_contradictory_available_from_rejected() -> None:
    facts = {
        "X": {"facts": [_fact(publication_date="2020-02-01",
                              available_from="2020-01-01")]}
    }
    cal = _complete_calendar(date(2020, 1, 1), date(2020, 2, 3))
    errors = validate_facts(facts, cal)
    assert any("!=" in e or "not after" in e for e in errors)


def test_weekend_available_from_calendar() -> None:
    # Friday 2026-01-09 open, Sat/Sun closed, Monday 01-12 open
    cal = [
        (date(2026, 1, 9), True),
        (date(2026, 1, 10), False),
        (date(2026, 1, 11), False),
        (date(2026, 1, 12), True),
        (date(2026, 1, 13), True),
    ]
    facts = {"X": {"facts": [_fact(publication_date="2026-01-09")]}}
    errors = validate_facts(facts, cal)
    assert errors == []
    assert facts["X"]["facts"][0]["available_from"] == "2026-01-12"


def test_announcement_before_left_boundary() -> None:
    facts = {"X": {"facts": [_fact(publication_date="2019-11-15")]}}
    cal = _calendar(date(2020, 1, 2), date(2020, 1, 3))
    errors = validate_facts(facts, cal)
    assert any("before calendar left boundary" in e for e in errors)


def test_announcement_after_right_boundary() -> None:
    facts = {"X": {"facts": [_fact(publication_date="2026-02-01")]}}
    cal = _calendar(date(2026, 1, 2), date(2026, 1, 3))
    errors = validate_facts(facts, cal)
    assert any("after calendar right boundary" in e for e in errors)


def test_missing_middle_calendar_record() -> None:
    # 01-02 and 01-04 present but 01-03 missing -> incomplete coverage
    cal = [(date(2026, 1, 2), True), (date(2026, 1, 4), True)]
    facts = {"X": {"facts": [_fact(publication_date="2026-01-02")]}}
    errors = validate_facts(facts, cal)
    assert any("coverage incomplete" in e for e in errors)


def test_insufficient_calendar_after_publication() -> None:
    cal = _calendar(date(2026, 1, 8), date(2026, 1, 9))
    facts = {"X": {"facts": [_fact(publication_date="2026-01-09")]}}
    errors = validate_facts(facts, cal)
    assert any("insufficient calendar coverage after" in e for e in errors)
