from datetime import date
from pathlib import Path

from quantlab.backtest.delisting_facts import (
    facts_available_as_of,
    load_delisting_facts,
    source_coverage,
    trusted_facts_available_as_of,
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
