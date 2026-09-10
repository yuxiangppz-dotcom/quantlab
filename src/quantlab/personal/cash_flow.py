"""Typed external cash-flow facts for manual account tracking.

These facts describe money entering or leaving a user's account from outside the
strategy. They are deliberately not execution events, broker fills, or orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from quantlab.execution.models import require_aware, require_identifier, require_int


class CashFlowDirection(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


@dataclass(frozen=True)
class ExternalCashFlow:
    """One user-reported external deposit or withdrawal."""

    event_id: str
    flow_id: str
    occurred_at: datetime
    account_id: str
    direction: CashFlowDirection
    amount_fen: int
    source_sha256: str
    source_row_sha256: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.event_id, "event_id"),
            (self.flow_id, "flow_id"),
            (self.account_id, "account_id"),
        ):
            require_identifier(value, field)
        require_aware(self.occurred_at, "occurred_at")
        if not isinstance(self.direction, CashFlowDirection):
            raise ValueError("direction must be a CashFlowDirection")
        require_int(self.amount_fen, "amount_fen", minimum=1)
        for digest, field in (
            (self.source_sha256, "source_sha256"),
            (self.source_row_sha256, "source_row_sha256"),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{field} must be lowercase SHA-256")

    @property
    def signed_amount_fen(self) -> int:
        return self.amount_fen if self.direction is CashFlowDirection.DEPOSIT else -self.amount_fen
