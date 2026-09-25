"""Explicit corporate-event scenarios, entitlements and dividend receivables.

No provider/tax inference. Cash rates are DECLARED net scenarios; the
applicable holding-period differential dividend tax charged at disposal is a
separate, unimplemented cost. Share distributions are applied only when the
account-level entitlement is an exact whole share. Fractional entitlements
require the issuer/depository's tail-share allocation and stop replay. Rights
issues, mergers and delisting settlements require an explicit adapter and
stop here instead of silently inventing a settlement.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from quantlab.research.quantity_kernel import ResearchLot


@dataclass(frozen=True)
class CorporateEvent:
    event_id: str
    instrument_id: str
    kind: str
    record_date: date
    ex_date: date
    settlement_date: date
    source_id: str
    net_cash_per_share_fen: Decimal | None = None
    share_numerator: int | None = None
    share_denominator: int | None = None

    def __post_init__(self):
        for key in ("event_id", "instrument_id", "source_id"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"corporate {key} required")
        if self.kind not in {"cash_dividend", "bonus_shares", "split"}:
            raise ValueError("unsupported corporate action; explicit adapter required")
        if any(
            type(getattr(self, k)) is not date
            for k in ("record_date", "ex_date", "settlement_date")
        ):
            raise ValueError("corporate dates must be explicit dates")
        if not self.record_date < self.ex_date <= self.settlement_date:
            raise ValueError("corporate record/ex/settlement chronology invalid")
        if self.kind == "cash_dividend":
            rate = self.net_cash_per_share_fen
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0:
                raise ValueError("explicit nonnegative NET scenario cash rate required")
            if self.share_numerator is not None or self.share_denominator is not None:
                raise ValueError("cash event cannot modify shares")
        else:
            if self.net_cash_per_share_fen is not None:
                raise ValueError("share event cannot infer cash")
            for key in ("share_numerator", "share_denominator"):
                if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                    raise ValueError("positive integer share ratio required")
            if self.kind == "split" and self.settlement_date != self.ex_date:
                raise ValueError("split must be effective on ex-date")


def decode_event(raw):
    raw = dict(raw)
    for key in ("record_date", "ex_date", "settlement_date"):
        raw[key] = date.fromisoformat(raw[key])
    if raw.get("net_cash_per_share_fen") is not None:
        raw["net_cash_per_share_fen"] = Decimal(str(raw["net_cash_per_share_fen"]))
    return CorporateEvent(**raw)


def new_state():
    return {"processed": [], "receivables": [], "entitlements": {}}


def capture_entitlements(state, book, events):
    state = {**state, "entitlements": dict(state["entitlements"])}
    for event in events:
        if event.record_date == book.asof_date:
            state["entitlements"][event.event_id] = sum(
                lot.quantity for lot in book.lots if lot.instrument_id == event.instrument_id
            )
    return state


def receivable_value(state):
    return sum(item["cash_fen"] for item in state["receivables"])


def apply_events(book, day, events, state, inception):
    """Pure morning transition. Caller commits it only with the completed day."""
    if len({e.event_id for e in events}) != len(events):
        raise ValueError("duplicate corporate event id")
    state = {
        "processed": list(state["processed"]),
        "receivables": list(state["receivables"]),
        "entitlements": dict(state["entitlements"]),
    }
    movements = []
    for event in sorted(events, key=lambda e: (e.ex_date, e.event_id)):
        if event.ex_date != day:
            continue
        if event.event_id in state["processed"]:
            raise ValueError("corporate event already processed")
        entitled = state["entitlements"].get(event.event_id)
        if entitled is None:
            if event.record_date <= inception:  # the account was explicitly flat at inception
                entitled = 0
            else:
                raise ValueError(f"corporate entitlement snapshot missing:{event.event_id}")
        if event.kind == "cash_dividend":
            cash = int(
                (entitled * event.net_cash_per_share_fen).to_integral_value(rounding=ROUND_HALF_UP)
            )
            state["receivables"].append(
                {
                    "event_id": event.event_id,
                    "cash_fen": cash,
                    "settlement_date": str(event.settlement_date),
                }
            )
            movements.append(
                {"event_id": event.event_id, "kind": "dividend_receivable", "cash_fen": cash}
            )
        elif event.kind == "bonus_shares":
            count, remainder = divmod(entitled * event.share_numerator, event.share_denominator)
            if remainder:
                raise ValueError(
                    f"fractional bonus shares require explicit settlement:{event.event_id}"
                )
            if count:
                lot = ResearchLot(
                    f"corporate:{event.event_id}",
                    event.instrument_id,
                    count,
                    event.record_date,
                    event.settlement_date,
                )
                book = replace(book, lots=(*book.lots, lot))
            movements.append({"event_id": event.event_id, "kind": event.kind, "shares": count})
        else:
            lots = []
            for lot in book.lots:
                if lot.instrument_id == event.instrument_id:
                    count, remainder = divmod(
                        lot.quantity * event.share_numerator, event.share_denominator
                    )
                    if remainder or count <= 0:
                        raise ValueError("fractional split requires explicit settlement")
                    lot = replace(lot, quantity=count)
                lots.append(lot)
            book = replace(book, lots=tuple(lots))
            movements.append({"event_id": event.event_id, "kind": event.kind})
        state["processed"].append(event.event_id)
    pending = []
    for receipt in state["receivables"]:
        if date.fromisoformat(receipt["settlement_date"]) <= day:
            book = replace(book, cash_fen=book.cash_fen + receipt["cash_fen"])
            movements.append(
                {
                    "event_id": receipt["event_id"],
                    "kind": "dividend_cash_paid",
                    "cash_fen": receipt["cash_fen"],
                }
            )
        else:
            pending.append(receipt)
    state["receivables"] = pending
    return book, state, movements
