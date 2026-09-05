"""Research backtest models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BacktestConfig:
    """Configuration for the idealized research backtest."""

    initial_nav: float = 1.0
    transaction_cost_bps: float = 0.0
    annualization: int = 252

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
