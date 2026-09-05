from datetime import date
from pathlib import Path

from quantlab.backtest.delisting_facts import (
    facts_available_as_of,
    load_delisting_facts,
    source_coverage,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_available_from_filters_future() -> None:
    facts = {
        "X": {
            "facts": [
                {"fact_type": "a", "available_from": "2020-01-08",
                 "verification_status": "verified"},
                {"fact_type": "b", "available_from": None,
                 "verification_status": "verified"},
            ]
        }
    }
    assert facts_available_as_of(facts, "X", date(2020, 1, 7)) == []
    avail = facts_available_as_of(facts, "X", date(2020, 1, 8))
    assert [f["fact_type"] for f in avail] == ["a"]


def test_unknown_date_not_derived() -> None:
    facts = {"X": {"facts": [{"fact_type": "b", "available_from": None}]}}
    assert facts_available_as_of(facts, "X", date(2030, 1, 1)) == []


def test_source_coverage_requires_verified() -> None:
    facts = {
        "verified": {"facts": [{"verification_status": "verified"}]},
        "unverified": {"facts": [{"verification_status": "unknown"}]},
        "empty": {"facts": []},
    }
    assert source_coverage(facts, "verified") == "verified"
    assert source_coverage(facts, "unverified") == "unknown"
    assert source_coverage(facts, "empty") == "unknown"
    assert source_coverage(facts, "absent") == "unknown"


def test_real_fixture_expected_vs_confirmed_separated() -> None:
    facts = load_delisting_facts(PROJECT_ROOT / "config" / "delisting_facts.json")
    entry = facts["000018.SZ"]
    types = {f["fact_type"] for f in entry["facts"]}
    assert "expected_last_trading_day" in types
    assert "confirmed_last_trading_day" in types
    # at the 2020-01-03 signal the delisting fact was not yet available
    avail = facts_available_as_of(facts, "000018.SZ", date(2020, 1, 3))
    avail_types = {f["fact_type"] for f in avail}
    assert "expected_last_trading_day" in avail_types
    assert "confirmed_last_trading_day" not in avail_types
    assert "delisting" not in avail_types


def test_late_published_fact_not_available_early() -> None:
    facts = {
        "X": {
            "facts": [
                {"fact_type": "delisting", "available_from": "2020-01-08",
                 "verification_status": "verified"},
            ]
        }
    }
    assert facts_available_as_of(facts, "X", date(2020, 1, 3)) == []
    assert facts_available_as_of(facts, "X", date(2020, 1, 8)) != []
