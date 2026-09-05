"""Target portfolio domain models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from quantlab.data.models import DataValidationError

_WEIGHT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class TargetWeight:
    """Desired weight for a single instrument (not an order or a fill)."""

    instrument_id: str
    target_weight: float


@dataclass(frozen=True)
class TargetPortfolio:
    """Desired holdings on a single ``as_of`` date.

    A TargetPortfolio is an *intention*, not a trade record, actual position, or
    return forecast. ``cash_weight`` is the residual not allocated to positions.
    """

    as_of: date
    positions: tuple[TargetWeight, ...]
    cash_weight: float
    gross_exposure: float = 1.0

    def __post_init__(self) -> None:
        seen: set[str] = set()
        total = 0.0
        for pos in self.positions:
            if pos.instrument_id in seen:
                raise DataValidationError(f"duplicate instrument_id: {pos.instrument_id}")
            seen.add(pos.instrument_id)
            if not math.isfinite(pos.target_weight):
                raise DataValidationError(
                    f"non-finite target_weight for {pos.instrument_id}"
                )
            if pos.target_weight < 0:
                raise DataValidationError(
                    f"negative target_weight for {pos.instrument_id}"
                )
            total += pos.target_weight

        if not math.isfinite(self.cash_weight):
            raise DataValidationError("non-finite cash_weight")
        if self.cash_weight < 0:
            raise DataValidationError("negative cash_weight")

        if abs(total + self.cash_weight - self.gross_exposure) > _WEIGHT_TOLERANCE:
            raise DataValidationError(
                f"weights sum {total} + cash {self.cash_weight} "
                f"!= gross_exposure {self.gross_exposure}"
            )
