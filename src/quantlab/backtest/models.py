"""Research backtest models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BacktestConfig:
    """Configuration for the idealized research backtest."""

    initial_nav: float = 1.0
    transaction_cost_bps: float = 0.0
    annualization: int = 252

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_nav) or self.initial_nav <= 0:
            raise ValueError(f"initial_nav must be finite and > 0, got {self.initial_nav}")
        if not math.isfinite(self.transaction_cost_bps) or self.transaction_cost_bps < 0:
            raise ValueError(
                f"transaction_cost_bps must be finite and >= 0, "
                f"got {self.transaction_cost_bps}"
            )
        if (
            isinstance(self.annualization, bool)
            or not isinstance(self.annualization, int)
            or self.annualization <= 0
        ):
            raise ValueError(
                f"annualization must be a positive integer, got {self.annualization}"
            )

    @property
    def cost_rate(self) -> float:
        return self.transaction_cost_bps / 10_000.0


@dataclass(frozen=True)
class DailyBacktestRecord:
    trade_date: date
    nav_gross: float
    nav_net: float
    daily_return_gross: float
    daily_return_net: float
    gross_exposure: float
    net_exposure: float
    cash_weight: float
    turnover: float
    traded_notional_ratio: float
    transaction_cost: float
    holdings_count: int


@dataclass(frozen=True)
class RebalanceRecord:
    signal_date: date
    execution_date: date
    target_count: int
    filled_target_count: int
    unavailable_target_count: int
    pre_trade_gross_exposure: float
    post_trade_gross_exposure: float
    traded_notional_ratio: float
    turnover: float
    transaction_cost: float
