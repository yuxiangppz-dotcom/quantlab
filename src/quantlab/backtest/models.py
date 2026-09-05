"""Research backtest models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

_MAX_COST_BPS = 9_999.999  # cost_rate must be strictly < 1


@dataclass(frozen=True)
class BacktestConfig:
    """Configuration for the idealized research backtest."""

    initial_nav: float = 1.0
    transaction_cost_bps: float = 0.0
    annualization: int = 252

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_nav) or self.initial_nav <= 0:
            raise ValueError(f"initial_nav must be finite and > 0, got {self.initial_nav}")
        if (
            not math.isfinite(self.transaction_cost_bps)
            or self.transaction_cost_bps < 0
            or self.transaction_cost_bps >= _MAX_COST_BPS
        ):
            raise ValueError(
                f"transaction_cost_bps must be finite and in [0, {_MAX_COST_BPS}), "
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
class PositionRecord:
    """End-of-day state of a single position within one book."""

    instrument_id: str
    value: float
    weight: float
    last_price: float | None
    last_mark_date: date | None
    missing_price: bool


@dataclass(frozen=True)
class BookSnapshot:
    """End-of-day state of one ledger (gross or net)."""

    trade_date: date
    book: str
    nav: float
    daily_return: float
    cash: float
    market_pnl: float
    fee: float
    gross_exposure: float
    net_exposure: float
    cash_weight: float
    holdings_count: int
    positions: tuple[PositionRecord, ...]


@dataclass(frozen=True)
class DailyBacktestRecord:
    """Combined gross/net daily record for metrics.

    Suffix-less exposure / turnover / cash fields describe the **net** book and
    are kept for backward compatibility; the gross book is exposed with an
    explicit ``gross_book_`` prefix.
    """

    trade_date: date
    nav_gross: float
    nav_net: float
    daily_return_gross: float
    daily_return_net: float
    # net book (compat, no suffix)
    gross_exposure: float
    net_exposure: float
    cash_weight: float
    turnover: float
    traded_notional_ratio: float
    transaction_cost: float
    holdings_count: int
    # gross book
    gross_book_gross_exposure: float
    gross_book_net_exposure: float
    gross_book_cash_weight: float
    gross_book_turnover: float
    gross_book_traded_notional_ratio: float
    gross_book_holdings_count: int


@dataclass(frozen=True)
class TradeRecord:
    """Per-instrument, per-book trade detail at one rebalance."""

    signal_date: date
    execution_date: date
    book: str
    instrument_id: str
    pre_value: float
    post_value: float
    signed_trade_value: float
    target_weight: float
    actual_weight: float
    reason: str


@dataclass(frozen=True)
class RebalanceRecord:
    """Per-rebalance summary across both books.

    Suffix-less fields describe the **net** book; the gross book uses the
    ``gross_book_`` prefix.
    """

    signal_date: date
    execution_date: date
    target_count: int
    filled_target_count: int
    unavailable_target_count: int
    frozen_count: int
    # net book
    buy_notional_ratio: float
    sell_notional_ratio: float
    traded_notional_ratio: float
    turnover: float
    transaction_cost: float
    pre_trade_gross_exposure: float
    post_trade_gross_exposure: float
    allocation_deviation: float
    # gross book
    gross_book_buy_notional_ratio: float
    gross_book_sell_notional_ratio: float
    gross_book_traded_notional_ratio: float
    gross_book_turnover: float
    gross_book_transaction_cost: float
    gross_book_pre_trade_gross_exposure: float
    gross_book_post_trade_gross_exposure: float
    gross_book_allocation_deviation: float


@dataclass(frozen=True)
class SkippedExecution:
    """A signal whose execution fell outside the simulation window."""

    signal_date: date
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    """Full output of a backtest run."""

    records: list[DailyBacktestRecord]
    rebalances: list[RebalanceRecord]
    books: list[BookSnapshot]
    trades: list[TradeRecord]
    skipped_executions: list[SkippedExecution]
    max_conservation_residual: float
