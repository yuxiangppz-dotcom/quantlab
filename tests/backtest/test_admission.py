from copy import deepcopy
from datetime import date
from pathlib import Path

from quantlab.backtest.admission import shadow_admission
from quantlab.backtest.delisting_facts import (
    load_delisting_facts,
    trusted_facts_available_as_of,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _fact(**overrides) -> dict:
    base = {
        "fact_type": "termination_decision",
        "fact_id": "X:termination_decision:2020-01-02",
        "effective_date": "2020-01-02",
        "content_verified": True,
        "public_time_verified": True,
        "available_from": "2020-01-03",
        "source": "s",
    }
    base.update(overrides)
    return base


def _facts(*facts) -> dict:
    return {"X": {"facts": list(facts)}}


def test_unknown_content_with_available_from_not_trusted() -> None:
    facts = _facts(_fact(content_verified=False))
    assert trusted_facts_available_as_of(facts, "X", date(2020, 1, 5)) == []


def test_content_verified_but_no_public_time_not_trusted() -> None:
    facts = _facts(_fact(public_time_verified=False))
    assert trusted_facts_available_as_of(facts, "X", date(2020, 1, 5)) == []


def test_missing_available_from_not_trusted() -> None:
    facts = _facts(_fact(available_from=None))
    assert trusted_facts_available_as_of(facts, "X", date(2020, 1, 5)) == []


def test_announcement_boundary() -> None:
    facts = _facts(_fact(available_from="2020-01-03"))
    assert trusted_facts_available_as_of(facts, "X", date(2020, 1, 2)) == []
    assert len(trusted_facts_available_as_of(facts, "X", date(2020, 1, 3))) == 1


def test_future_fact_does_not_change_past() -> None:
    facts = _facts(_fact(available_from="2020-02-01"))
    assert trusted_facts_available_as_of(facts, "X", date(2020, 1, 15)) == []


def test_000018_restricted_at_signal() -> None:
    facts = load_delisting_facts(PROJECT_ROOT / "config" / "delisting_facts.json")
    trusted = trusted_facts_available_as_of(facts, "000018.SZ", date(2020, 1, 3))
    decision = shadow_admission("000018.SZ", date(2020, 1, 3), trusted)
    assert decision.status == "restricted"
    assert decision.policy_version == "no_new_exposure_after_termination_decision_v1"
    assert decision.available_from == date(2019, 11, 18)


def test_unknown_when_no_trusted_facts() -> None:
    facts = _facts(_fact(public_time_verified=False))
    trusted = trusted_facts_available_as_of(facts, "X", date(2020, 1, 5))
    decision = shadow_admission("X", date(2020, 1, 5), trusted)
    assert decision.status == "unknown"


def test_shadow_admission_pure() -> None:
    facts = _facts(_fact())
    before = deepcopy(facts)
    trusted = trusted_facts_available_as_of(facts, "X", date(2020, 1, 5))
    shadow_admission("X", date(2020, 1, 5), trusted)
    assert facts == before
