"""Typed user-imported fill facts with explicit execution/report timing.

The execution layer remains authoritative for fill accounting. This personal-layer
fact adds observation provenance and timing quality, then converts to the existing
``ManualFillImported`` ledger event only for deterministic replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from quantlab.execution import ManualFillImported, Side, exchange_date
from quantlab.execution.models import (
    require_aware,
    require_decimal,
    require_identifier,
    require_int,
)


class ManualFillTimingQuality(StrEnum):
    """Evidence quality for a manual fill's intraday execution timestamp."""

    EXACT_EXECUTION_TIME = "exact_execution_time"
    LEGACY_REPORTED_AS_EXECUTION_UNVERIFIED = (
        "legacy_reported_time_used_as_execution_unverified"
    )


@dataclass(frozen=True)
class ManualFillFact:
    """One user-reported broker fill plus timing provenance.

    ``occurred_at`` is the economic execution timestamp used for replay when
    timing quality is exact. For legacy rows it carries the historical report-time
    surrogate so old journals replay exactly as before; the quality flag prevents
    that surrogate from being mistaken for verified execution time.
    """

    event_id: str
    fill_id: str
    occurred_at: datetime
    account_id: str
    instrument_id: str
    side: Side
    trade_date: date
    quantity: int
    price: Decimal
    gross_notional_fen: int
    fee_fen: int
    buy_lot_sellable_from: date | None
    source_sha256: str
    source_row_sha256: str
    reported_at: datetime | None = None
    timing_quality: ManualFillTimingQuality = (
        ManualFillTimingQuality.LEGACY_REPORTED_AS_EXECUTION_UNVERIFIED
    )

    def __post_init__(self) -> None:
        for value, field in (
            (self.event_id, "event_id"),
            (self.fill_id, "fill_id"),
            (self.account_id, "account_id"),
            (self.instrument_id, "instrument_id"),
        ):
            require_identifier(value, field)
        require_aware(self.occurred_at, "executed_at")
        if self.reported_at is not None:
            require_aware(self.reported_at, "reported_at")
            if self.reported_at < self.occurred_at:
                raise ValueError("reported_at cannot precede fill executed_at")
        if not isinstance(self.side, Side):
            raise ValueError("side must be a Side enum")
        if not isinstance(self.timing_quality, ManualFillTimingQuality):
            raise ValueError("timing_quality must be a ManualFillTimingQuality")
        if self.timing_quality is ManualFillTimingQuality.EXACT_EXECUTION_TIME:
            if self.reported_at is None:
                raise ValueError("exact execution-time fill requires reported_at provenance")
            if exchange_date(self.occurred_at) != self.trade_date:
                raise ValueError("trade_date must equal Shanghai date of executed_at")
        elif self.reported_at is not None and self.reported_at != self.occurred_at:
            raise ValueError(
                "legacy fill timing cannot carry a distinct reported_at; "
                "use exact_execution_time"
            )
        require_int(self.quantity, "quantity", minimum=1)
        require_decimal(self.price, "price", positive=True)
        require_int(self.gross_notional_fen, "gross_notional_fen", minimum=1)
        require_int(self.fee_fen, "fee_fen")
        exact_fen = self.price * self.quantity * 100
        if exact_fen != exact_fen.to_integral_value():
            raise ValueError(
                "fill price and quantity produce fractional fen; explicit rounding is required"
            )
        if int(exact_fen) != self.gross_notional_fen:
            raise ValueError("gross_notional_fen disagrees with Decimal price * quantity")
        for digest, field in (
            (self.source_sha256, "source_sha256"),
            (self.source_row_sha256, "source_row_sha256"),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{field} must be lowercase SHA-256")

    @property
    def executed_at(self) -> datetime:
        return self.occurred_at

    @property
    def report_time(self) -> datetime:
        return self.reported_at or self.occurred_at

    @property
    def is_performance_timing_eligible(self) -> bool:
        return self.timing_quality is ManualFillTimingQuality.EXACT_EXECUTION_TIME

    def to_execution_event(self) -> ManualFillImported:
        """Convert into the existing execution-ledger fact without new authority."""

        return ManualFillImported(
            event_id=self.event_id,
            fill_id=self.fill_id,
            occurred_at=self.occurred_at,
            account_id=self.account_id,
            instrument_id=self.instrument_id,
            side=self.side,
            trade_date=self.trade_date,
            quantity=self.quantity,
            price=self.price,
            gross_notional_fen=self.gross_notional_fen,
            fee_fen=self.fee_fen,
            buy_lot_sellable_from=self.buy_lot_sellable_from,
            source_sha256=self.source_sha256,
            source_row_sha256=self.source_row_sha256,
        )
