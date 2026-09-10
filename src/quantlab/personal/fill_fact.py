"""Typed user-imported fill facts with explicit execution/report timing.

The execution layer remains authoritative for fill accounting. This personal-layer
subtype adds observation provenance and timing quality while remaining a normal
``ManualFillImported`` fact for the existing deterministic execution ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from quantlab.execution import ManualFillImported, exchange_date
from quantlab.execution.models import require_aware


class ManualFillTimingQuality(StrEnum):
    """Evidence quality for a manual fill's intraday execution timestamp."""

    EXACT_EXECUTION_TIME = "exact_execution_time"
    LEGACY_REPORTED_AS_EXECUTION_UNVERIFIED = (
        "legacy_reported_time_used_as_execution_unverified"
    )


@dataclass(frozen=True)
class ManualFillFact(ManualFillImported):
    """Manual fill plus explicit economic execution/report timing provenance.

    ``occurred_at`` remains the ledger ordering field. For exact facts it is the
    explicitly supplied execution timestamp. For legacy rows it remains the old report-time
    surrogate so existing journals replay identically; the timing-quality flag
    prevents that surrogate from being mistaken for exact execution time. Exact
    means explicit and internally consistent, not independently broker-verified.
    """

    reported_at: datetime | None = None
    timing_quality: ManualFillTimingQuality = (
        ManualFillTimingQuality.LEGACY_REPORTED_AS_EXECUTION_UNVERIFIED
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.reported_at is not None:
            require_aware(self.reported_at, "reported_at")
            if self.reported_at < self.occurred_at:
                raise ValueError("reported_at cannot precede fill executed_at")
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

    @property
    def executed_at(self) -> datetime | None:
        return self.occurred_at if self.is_performance_timing_eligible else None

    @property
    def report_time(self) -> datetime:
        return self.reported_at or self.occurred_at

    @property
    def is_performance_timing_eligible(self) -> bool:
        return self.timing_quality is ManualFillTimingQuality.EXACT_EXECUTION_TIME
