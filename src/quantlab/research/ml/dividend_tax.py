"""Declared individual A-share cash-dividend tax, charged on a modeled sale.

This is an accounting component, not evidence that a vendor dividend row is the
cash actually paid to this account. Only an event with an explicitly certified
gross payment and no at-source withholding may create a claim here.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


def _anniversary(acquired: date, months: int) -> date:
    month = acquired.month - 1 + months
    year = acquired.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(acquired.day, monthrange(year, month)[1]))


def individual_dividend_tax_rate(acquired: date, sold: date) -> Decimal:
    """2015/101, with the one-month and one-year endpoints inclusive."""
    if not acquired < sold:
        raise ValueError("dividend tax sale must follow acquisition")
    if sold <= _anniversary(acquired, 1):
        return Decimal("0.20")
    if sold <= _anniversary(acquired, 12):
        return Decimal("0.10")
    return Decimal(0)


def capture_tax_claims(state: dict, book, events) -> dict:
    """Bind record-date entitlements to actual acquisition lots, not pay-day holdings."""
    state = {**state, "tax_claims": list(state.get("tax_claims", []))}
    for event in events:
        if event.record_date != book.asof_date or event.tax_treatment != "individual_a_share_2015":
            continue
        if any(c["event_id"] == event.event_id for c in state["tax_claims"]):
            raise ValueError(f"duplicate tax entitlement:{event.event_id}")
        for lot in book.lots:
            if lot.instrument_id == event.instrument_id:
                state["tax_claims"].append(
                    {
                        "event_id": event.event_id,
                        "lot_id": lot.lot_id,
                        "instrument_id": lot.instrument_id,
                        "acquired_on": lot.acquired_on.isoformat(),
                        "remaining": lot.quantity,
                        "gross_per_share_fen": str(event.gross_cash_per_share_fen),
                    }
                )
    return state


def charge_sale_tax(state: dict, before, after, sold_on: date) -> tuple[dict, int, list[dict]]:
    """Charge only shares actually sold by the quantity kernel; leave claims after pay day."""
    old = {lot.lot_id: lot for lot in before.lots}
    new = {lot.lot_id: lot for lot in after.lots}
    sold = {
        lot_id: lot.quantity - (new[lot_id].quantity if lot_id in new else 0)
        for lot_id, lot in old.items()
    }
    if any(q < 0 for q in sold.values()):
        raise ValueError("sale tax cannot process a share increase")
    claims = [dict(c) for c in state.get("tax_claims", [])]
    movements = []
    total = 0
    for claim in claims:
        count = min(sold.get(claim["lot_id"], 0), claim["remaining"])
        if not count:
            continue
        rate = individual_dividend_tax_rate(date.fromisoformat(claim["acquired_on"]), sold_on)
        amount = int(
            (Decimal(claim["gross_per_share_fen"]) * count * rate).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        claim["remaining"] -= count
        total += amount
        movements.append(
            {
                "event_id": claim["event_id"],
                "lot_id": claim["lot_id"],
                "kind": "realized_individual_dividend_tax",
                "shares": count,
                "rate": str(rate),
                "cash_fen": -amount,
            }
        )
    return {**state, "tax_claims": [c for c in claims if c["remaining"]]}, total, movements
