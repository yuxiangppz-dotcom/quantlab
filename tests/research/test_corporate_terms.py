from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.corporate_terms import minimum_terms


def observed(**updates):
    row = dict(
        observation_id="a" * 64,
        content_id="b" * 64,
        response_fingerprint="c" * 64,
        instrument_id="000001.SZ",
        observed_at="2026-09-12T19:00:00+00:00",
        availability_bound_source="receipt_recorded_at",
        normalized_status="实施",
        implementation_candidate=True,
        quality_flags='["share_components_unknown"]',
        candidate_conflict=False,
        possible_event_conflict=False,
        record_date="2020-06-01",
        ex_date="2020-06-02",
        pay_date="2020-06-02",
        div_listdate=None,
        cash_div_tax=0.1,
        stk_div=0.0,
        stk_bo_rate=None,
        stk_co_rate=None,
        cashflow_eligible=False,
        historical_pit_certified=False,
    )
    return {**row, **updates}


def test_known_zero_total_leaves_subcomponent_nulls_unchanged():
    source = observed()
    term = minimum_terms(source)
    assert term.total_stock_ratio == 0
    assert term.bonus_ratio is None and term.conversion_ratio is None
    assert source["stk_bo_rate"] is None and source["stk_co_rate"] is None
    assert term.breakdown_state == "not_required_for_zero_total"
    assert term.quantity_fields_complete and term.cash_fields_complete
    assert term.payload()["numerical_date_bundle_complete"] is True
    assert term.payload()["cash_before_tax"] == "0.1"


def test_positive_partial_breakdown_retains_quantity_but_requires_tax_context():
    term = minimum_terms(observed(stk_div=0.5, stk_co_rate=0.5, div_listdate="2020-06-03"))
    assert term.quantity_fields_complete
    assert term.breakdown_state == "partial_unknown" and term.bonus_ratio is None
    assert "stock_distribution_tax_breakdown" in term.required_context
    assert "fractional_share_allocation_and_sellable_date" in term.required_context


def test_complete_positive_components_do_not_invent_share_allocation():
    term = minimum_terms(
        observed(stk_div=0.3, stk_bo_rate=0.1, stk_co_rate=0.2, div_listdate="2020-06-02")
    )
    assert term.breakdown_state == "complete_observed"
    assert term.total_stock_ratio == Decimal("0.3")
    assert "fractional_share_allocation_and_sellable_date" in term.required_context
    assert "stock_distribution_tax_breakdown" not in term.required_context


@pytest.mark.parametrize(
    "updates,blocker",
    [
        ({"stk_bo_rate": 0.1}, "observed_stock_components_inconsistent"),
        ({"stk_div": 0.3, "stk_co_rate": 0.4}, "observed_stock_components_inconsistent"),
        (
            {"stk_div": 0.3, "stk_co_rate": 0.1, "stk_bo_rate": 0.1},
            "observed_stock_components_inconsistent",
        ),
        ({"stk_div": None}, "total_stock_ratio_unknown"),
        ({"stk_div": 0.3}, "positive_stock_listing_date_unknown"),
        ({"div_listdate": "2020-05-31"}, "listing_before_ex"),
    ],
)
def test_quantity_failures_are_component_specific(updates, blocker):
    term = minimum_terms(observed(**updates))
    assert blocker in term.quantity_blockers
    assert not term.quantity_fields_complete
    assert term.cash_fields_complete


@pytest.mark.parametrize(
    "updates,blocker",
    [
        ({"cash_div_tax": None}, "cash_before_tax_unknown"),
        ({"pay_date": None}, "positive_cash_pay_date_unknown"),
        ({"pay_date": "2020-06-01"}, "payment_before_ex"),
    ],
)
def test_cash_failures_do_not_erase_known_quantity(updates, blocker):
    term = minimum_terms(observed(**updates))
    assert blocker in term.cash_blockers
    assert not term.cash_fields_complete
    assert term.quantity_fields_complete


def test_zero_cash_requires_no_payment_and_differs_from_null():
    zero = minimum_terms(observed(cash_div_tax=0, pay_date=None))
    unknown = minimum_terms(observed(cash_div_tax=None, pay_date=None))
    assert zero.cash_state == "known_zero" and zero.cash_fields_complete
    assert unknown.cash_state == "unknown" and not unknown.cash_fields_complete


@pytest.mark.parametrize(
    "updates,blocker",
    [
        ({"record_date": None}, "record_or_ex_unknown"),
        ({"record_date": "2020-06-02"}, "record_not_before_ex"),
        ({"record_date": "2020-06-03"}, "record_not_before_ex"),
        ({"normalized_status": "预案", "implementation_candidate": False}, "not_implementation"),
        ({"candidate_conflict": True}, "unresolved_source_conflict"),
        ({"possible_event_conflict": True}, "unresolved_source_conflict"),
        ({"quality_flags": '["malformed_cash_div"]'}, "malformed_cash_div"),
    ],
)
def test_common_gaps_block_both_components(updates, blocker):
    term = minimum_terms(observed(**updates))
    assert blocker in term.common_blockers
    assert not term.cash_fields_complete and not term.quantity_fields_complete


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "0.1"])
def test_unparsed_measures_rejected(value):
    with pytest.raises(DataValidationError):
        minimum_terms(observed(cash_div_tax=value))


@pytest.mark.parametrize("value", ["20200601", "2020-99-99", False])
def test_unparsed_dates_rejected(value):
    with pytest.raises(DataValidationError):
        minimum_terms(observed(record_date=value))


def test_duplicates_and_source_identity_are_not_resolved_by_adapter():
    first = minimum_terms(observed())
    repeated = minimum_terms(observed(observation_id="d" * 64))
    assert first.observation_id != repeated.observation_id
    assert first.content_id == repeated.content_id
    assert "unique_economic_event" in first.required_context
    assert first.response_fingerprint == "c" * 64


def test_authority_stays_false_and_terms_immutable():
    term = minimum_terms(observed())
    for key in ("cashflow_eligible", "historical_pit_certified", "execution_authority"):
        assert term.payload()[key] is False
        with pytest.raises(FrozenInstanceError):
            setattr(term, key, True)
    with pytest.raises(DataValidationError):
        minimum_terms(observed(cashflow_eligible=True))
