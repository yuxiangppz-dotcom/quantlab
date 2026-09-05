"""Target portfolio domain models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from quantlab.data.models import DataValidationError

_WEIGHT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class TargetWeight:
    """Desired weight for a single instrument, relative to portfolio NAV.

    Signed: positive is long, negative is short.
    """

    instrument_id: str
    target_weight: float


@dataclass(frozen=True)
class TargetPortfolio:
    """Desired holdings on a single ``as_of`` date.

    All weights (including ``cash_weight``) are relative to portfolio NAV and
    sum to 1.0. Exposure is derived, not stored. A TargetPortfolio is an
    *intention*, not an order, fill, or return forecast.
    """

    as_of: date
    positions: tuple[TargetWeight, ...]
    cash_weight: float

    def __post_init__(self) -> None:
        seen: set[str] = set()
        total = 0.0
        for pos in self.positions:
            if not pos.instrument_id:
                raise DataValidationError("empty instrument_id in positions")
            if pos.instrument_id in seen:
                raise DataValidationError(f"duplicate instrument_id: {pos.instrument_id}")
            seen.add(pos.instrument_id)
            if not math.isfinite(pos.target_weight):
                raise DataValidationError(
                    f"non-finite target_weight for {pos.instrument_id}"
                )
            total += pos.target_weight

        if not math.isfinite(self.cash_weight):
            raise DataValidationError("non-finite cash_weight")

        if abs(total + self.cash_weight - 1.0) > _WEIGHT_TOLERANCE:
            raise DataValidationError(
                f"position weights sum {total} + cash {self.cash_weight} != 1.0"
            )

    @property
    def net_exposure(self) -> float:
        """Sum of signed position weights."""
        return sum(pos.target_weight for pos in self.positions)

    @property
    def gross_exposure(self) -> float:
        """Sum of absolute position weights."""
        return sum(abs(pos.target_weight) for pos in self.positions)
