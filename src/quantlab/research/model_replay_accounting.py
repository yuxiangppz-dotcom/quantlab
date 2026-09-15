"""Scenario corporate accounting and marks; never manufactures market quotes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd


class ReplayEvidenceError(ValueError):
    """A concrete unresolved input, requiring repair before path completion."""


def fen(value):
    number = Decimal(str(value)) * 100
    if not number.is_finite() or number != number.to_integral_value():
        raise ReplayEvidenceError(f"not exact fen: {value}")
    return int(number)


def rounded(value):
    return int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))


def dividend_tax_rate(acquired: date, disposed: date) -> Decimal:
    # Holding days run from acquisition to the day BEFORE disposal.
    # Calendar offsets preserve month ends, rather than substituting 30/365 days.
    if disposed < acquired:
        raise ValueError("disposal before acquisition")
    start = pd.Timestamp(acquired)
    if pd.Timestamp(disposed) <= start + pd.DateOffset(months=1):
        return Decimal("0.2")
    if pd.Timestamp(disposed) <= start + pd.DateOffset(years=1):
        return Decimal("0.1")
    return Decimal(0)


@dataclass(frozen=True)
class ValuationMark:
    instrument_id: str
    valuation_date: date
    observed_date: date
    price_fen: int
    method: str


def valuation_mark(code, day, raw_price, previous, suspended):
    if raw_price is not None:
        value = fen(raw_price)
        if value <= 0:
            raise ReplayEvidenceError(f"nonpositive quote:{code}:{day}")
        return ValuationMark(code, day, day, value, "observed_raw_close")
    if suspended and previous is not None:
        if previous.observed_date >= day:
            raise ReplayEvidenceError("stale mark observation is not earlier")
        return replace(previous, valuation_date=day, method="suspension_carry_forward")
    raise ReplayEvidenceError(f"unexplained_missing_mark:{code}:{day}")


@dataclass(frozen=True)
class Distribution:
    event_id: str
    instrument_id: str
    record: date
    ex: date
    pay: date | None
    listing: date | None
    cash: Decimal
    stock: Decimal
    taxable_stock: Decimal


@dataclass
class Claim:
    event: Distribution
    lot_id: str
    acquired: date
    original_quantity: int
    remaining_quantity: int
    gross_fen: int
    remaining_tax_basis: Decimal
    sold_tax: Decimal = Decimal(0)
    withheld_fen: int = 0
    activated: bool = False
    paid: bool = False

    def reserve(self, day):
        if not self.activated:
            return 0
        return (
            rounded(
                self.sold_tax + self.remaining_tax_basis * dividend_tax_rate(self.acquired, day)
            )
            - self.withheld_fen
        )


def capture_claims(book, events):
    claims = []
    for event in events:
        for lot in book.lots:
            if lot.instrument_id == event.instrument_id:
                claims.append(
                    Claim(
                        event,
                        lot.lot_id,
                        lot.acquired_on,
                        lot.quantity,
                        lot.quantity,
                        rounded(lot.quantity * event.cash * 100),
                        lot.quantity * (event.cash + event.taxable_stock) * 100,
                    )
                )
    return claims


def open_corporate_day(book, claims, day, fractional_policy="stop"):
    if fractional_policy not in {"stop", "floor"}:
        raise ValueError("unknown fractional share policy")
    cash, lots, trace = book.cash_fen, list(book.lots), []
    allocations = {}
    groups = {}
    for claim in claims:
        if claim.event.ex == day and claim.event.stock and not claim.activated:
            groups.setdefault(claim.event.event_id, []).append(claim)
    for event_id, group in groups.items():
        exact = {c.lot_id: c.original_quantity * c.event.stock for c in group}
        total = sum(exact.values())
        if total != int(total) and fractional_policy == "stop":
            raise ReplayEvidenceError(f"fractional_share_allocation:{event_id}")
        shares = {lot_id: int(value) for lot_id, value in exact.items()}
        remainder = int(total) - sum(shares.values())
        order = sorted(exact, key=lambda lot_id: (-(exact[lot_id] - shares[lot_id]), lot_id))
        for lot_id in order[:remainder]:
            shares[lot_id] += 1
        allocations.update({(event_id, key): value for key, value in shares.items()})
        if total != int(total):
            trace.append(
                {
                    "event": event_id,
                    "kind": "fractional_shares_not_counted",
                    "quantity": str(total - int(total)),
                    "method": "user_authorized_account_level_floor_scenario",
                }
            )
    for claim in claims:
        event = claim.event
        if event.ex == day and not claim.activated:
            claim.activated = True
            if event.stock:
                # Preserve strict unknowns: delayed/fractional allocations need separate evidence.
                if event.listing != day:
                    raise ReplayEvidenceError(
                        f"share_availability_not_same_ex_date:{event.event_id}"
                    )
                extra = allocations[(event.event_id, claim.lot_id)]
                matching = [i for i, lot in enumerate(lots) if lot.lot_id == claim.lot_id]
                if not matching:
                    raise ReplayEvidenceError(f"shares_due_after_record_lot_sold:{event.event_id}")
                i = matching[0]
                lot = lots[i]
                if lot.quantity != claim.original_quantity:
                    raise ReplayEvidenceError(
                        f"record_lot_changed_before_share_listing:{event.event_id}"
                    )
                lots[i] = replace(lot, quantity=lot.quantity + int(extra))
                for other in claims:
                    if other.lot_id == lot.lot_id:
                        other.remaining_quantity += int(extra)
                trace.append(
                    {"event": event.event_id, "kind": "shares_listed", "quantity": int(extra)}
                )
            trace.append(
                {"event": event.event_id, "kind": "ex_entitlement", "gross_fen": claim.gross_fen}
            )
        if claim.activated and not claim.paid and (event.pay is None or event.pay <= day):
            # None is permitted only for explicitly zero cash distributions.
            if event.pay is None and claim.gross_fen:
                raise ReplayEvidenceError(f"cash_pay_date_unknown:{event.event_id}")
            cash += claim.gross_fen
            claim.paid = True
            trace.append(
                {"event": event.event_id, "kind": "cash_paid", "gross_fen": claim.gross_fen}
            )
        if claim.paid:
            tax = rounded(claim.sold_tax) - claim.withheld_fen
            cash -= tax
            claim.withheld_fen += tax
    if cash < 0:
        raise ReplayEvidenceError("cash_insufficient_for_dividend_tax")
    return replace(book, asof_date=day, cash_fen=cash, lots=tuple(lots), capacity_used=()), trace


def settle_disposals(before, after, claims, day):
    remaining = {lot.lot_id: lot.quantity for lot in after.lots}
    sold = {lot.lot_id: lot.quantity - remaining.get(lot.lot_id, 0) for lot in before.lots}
    cash, withheld = after.cash_fen, 0
    for claim in claims:
        quantity = sold.get(claim.lot_id, 0)
        if quantity:
            if quantity > claim.remaining_quantity:
                raise ReplayEvidenceError("disposal exceeds outstanding dividend claim units")
            basis = claim.remaining_tax_basis * quantity / claim.remaining_quantity
            claim.remaining_tax_basis -= basis
            claim.remaining_quantity -= quantity
            claim.sold_tax += basis * dividend_tax_rate(claim.acquired, day)
            if claim.paid:
                tax = rounded(claim.sold_tax) - claim.withheld_fen
                claim.withheld_fen += tax
                cash -= tax
                withheld += tax
    if cash < 0:
        raise ReplayEvidenceError("cash_insufficient_for_dividend_tax")
    return replace(after, cash_fen=cash), withheld


def corporate_nav(claims, day):
    receivable = sum(c.gross_fen for c in claims if c.activated and not c.paid)
    reserve = sum(c.reserve(day) for c in claims)
    return receivable, reserve
