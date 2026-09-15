from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from quantlab.research.model_replay_accounting import (
    Distribution,
    ReplayEvidenceError,
    capture_claims,
    corporate_nav,
    dividend_tax_rate,
    fen,
    open_corporate_day,
    settle_disposals,
    valuation_mark,
)
from quantlab.research.model_replay_data import canonical_amount_fen
from quantlab.research.quantity_kernel import ResearchBook, ResearchLot


def d(value):
    return date.fromisoformat(value)


def test_exact_price_and_float_amount_recovery_are_separate():
    assert canonical_amount_fen(132550852.00000001) == 13255085200
    assert canonical_amount_fen(264146474.99999997) == 26414647500
    with pytest.raises(ReplayEvidenceError):
        fen(1.001)
    with pytest.raises(ReplayEvidenceError):
        canonical_amount_fen(1.001)


def test_suspension_keeps_observation_date_and_never_invents_execution_quote():
    prior = valuation_mark("A", d("2023-01-03"), 10.12, None, False)
    mark = valuation_mark("A", d("2023-01-04"), None, prior, True)
    assert mark.observed_date == d("2023-01-03")
    assert mark.price_fen == 1012
    assert mark.method == "suspension_carry_forward"
    with pytest.raises(ReplayEvidenceError):
        valuation_mark("A", d("2023-01-04"), None, prior, False)


def test_calendar_month_and_year_boundaries():
    assert dividend_tax_rate(d("2023-01-15"), d("2023-02-15")) == Decimal(".2")
    assert dividend_tax_rate(d("2023-01-15"), d("2023-02-16")) == Decimal(".1")
    assert dividend_tax_rate(d("2023-01-15"), d("2024-01-15")) == Decimal(".1")
    assert dividend_tax_rate(d("2023-01-15"), d("2024-01-16")) == 0


def example():
    lot = ResearchLot("buy", "A", 100, d("2023-01-03"), d("2023-01-04"))
    book = ResearchBook(d("2023-01-05"), 100000, (lot,))
    event = Distribution(
        "A:div",
        "A",
        d("2023-01-05"),
        d("2023-01-06"),
        d("2023-01-09"),
        None,
        Decimal(".5"),
        Decimal(0),
        Decimal(0),
    )
    return book, capture_claims(book, [event])


def test_record_ex_pay_and_partial_sale_tax_conserve_equity():
    book, claims = example()
    assert corporate_nav(claims, d("2023-01-05")) == (0, 0)
    book, _ = open_corporate_day(book, claims, d("2023-01-06"))
    assert book.cash_fen == 100000
    assert corporate_nav(claims, d("2023-01-06")) == (5000, 1000)
    # A sale before payment accrues tax, not a duplicate withdrawal.
    after = replace(book, lots=(replace(book.lots[0], quantity=60),))
    after, tax = settle_disposals(book, after, claims, d("2023-01-06"))
    assert tax == 0 and after.cash_fen == 100000
    after, _ = open_corporate_day(after, claims, d("2023-01-09"))
    assert after.cash_fen == 104600
    assert corporate_nav(claims, d("2023-01-09")) == (0, 600)
    sold, tax = settle_disposals(after, replace(after, lots=()), claims, d("2023-01-10"))
    assert tax == 600 and sold.cash_fen == 104000
    assert corporate_nav(claims, d("2023-01-10")) == (0, 0)


def test_same_day_share_listing_changes_quantity_not_total_dividend_basis():
    book, claims = example()
    claims[0].event = replace(claims[0].event, stock=Decimal(".4"), listing=d("2023-01-06"))
    book, _ = open_corporate_day(book, claims, d("2023-01-06"))
    assert book.lots[0].quantity == 140
    assert claims[0].remaining_quantity == 140
    assert claims[0].remaining_tax_basis == 5000
    assert corporate_nav(claims, d("2023-01-06")) == (5000, 1000)


def test_fractional_share_and_unknown_availability_stop():
    book, claims = example()
    claims[0].event = replace(claims[0].event, stock=Decimal(".333"), listing=d("2023-01-06"))
    with pytest.raises(ReplayEvidenceError, match="fractional"):
        open_corporate_day(book, claims, d("2023-01-06"))


def test_later_sale_tax_uses_actual_lot_age_and_receipts_are_not_paid_twice():
    book, claims = example()
    book, _ = open_corporate_day(book, claims, d("2023-01-06"))
    book, _ = open_corporate_day(book, claims, d("2023-01-09"))
    book, _ = open_corporate_day(book, claims, d("2023-01-10"))
    assert book.cash_fen == 105000
    assert corporate_nav(claims, d("2023-02-05")) == (0, 500)
    sold, tax = settle_disposals(book, replace(book, lots=()), claims, d("2023-02-05"))
    assert tax == 500 and sold.cash_fen == 104500


def test_record_claims_use_filled_inventory_only():
    book, claims = example()
    assert capture_claims(replace(book, lots=()), [claims[0].event]) == []
