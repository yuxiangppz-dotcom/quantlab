"""Typed external cash-flow facts for manual account tracking.

These facts describe money entering or leaving a user's account from outside the
strategy. They are deliberately not execution events, broker fills, or orders.

`occurred_at` remains the internal ordering field for compatibility, but its
meaning is now explicit: it is the economic effective time of the cash movement.
`reported_at` records when the fact was observed. Legacy journal rows that only
carried one timestamp remain replayable but are never performance-grade evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from quantlab.execution.models import require_aware, require_identifier, require_int


class CashFlowDirection(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


class CashFlowTimingQuality(StrEnum):
    """Evidence quality for the economic timing of an external cash flow."""

    EXACT_EFFECTIVE_TIME = "exact_effective_time"
    LEGACY_REPORTED_AS_EFFECTIVE_UNVERIFIED = "legacy_reported_time_used_as_effective_unverified"


@dataclass(frozen=True)
class ExternalCashFlow:
    """One user-reported external deposit or withdrawal.

    ``occurred_at`` is the economic effective time used for deterministic replay.
    ``reported_at`` is provenance only. Old v2 journal rows omit ``reported_at``;
    callers decoding those rows must leave the timing quality at the legacy value.
    """

    event_id: str
    flow_id: str
    occurred_at: datetime
    account_id: str
    direction: CashFlowDirection
    amount_fen: int
    source_sha256: str
    source_row_sha256: str
    reported_at: datetime | None = None
    timing_quality: CashFlowTimingQuality = (
        CashFlowTimingQuality.LEGACY_REPORTED_AS_EFFECTIVE_UNVERIFIED
    )

    def __post_init__(self) -> None:
        for value, field in (
            (self.event_id, "event_id"),
            (self.flow_id, "flow_id"),
            (self.account_id, "account_id"),
        ):
            require_identifier(value, field)
        require_aware(self.occurred_at, "effective_at")
        if self.reported_at is not None:
            require_aware(self.reported_at, "reported_at")
            if self.reported_at < self.occurred_at:
                raise ValueError("reported_at cannot precede cash-flow effective_at")
        if not isinstance(self.direction, CashFlowDirection):
            raise ValueError("direction must be a CashFlowDirection")
        if not isinstance(self.timing_quality, CashFlowTimingQuality):
            raise ValueError("timing_quality must be a CashFlowTimingQuality")
        if self.timing_quality is CashFlowTimingQuality.EXACT_EFFECTIVE_TIME:
            if self.reported_at is None:
                raise ValueError("exact effective-time cash flow requires reported_at provenance")
        elif self.reported_at is not None and self.reported_at != self.occurred_at:
            raise ValueError(
                "legacy cash-flow timing cannot carry a distinct reported_at; "
                "use exact_effective_time"
            )
        require_int(self.amount_fen, "amount_fen", minimum=1)
        for digest, field in (
            (self.source_sha256, "source_sha256"),
            (self.source_row_sha256, "source_row_sha256"),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{field} must be lowercase SHA-256")

    @property
    def effective_at(self) -> datetime:
        """Explicit alias for the economic ordering timestamp."""

        return self.occurred_at

    @property
    def is_performance_timing_eligible(self) -> bool:
        return self.timing_quality is CashFlowTimingQuality.EXACT_EFFECTIVE_TIME

    @property
    def signed_amount_fen(self) -> int:
        return self.amount_fen if self.direction is CashFlowDirection.DEPOSIT else -self.amount_fen
