"""Research backtest models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

_MAX_COST_BPS = 10_000.0  # cost_rate = bps / 10_000 must be strictly < 1

RUN_MODE_STRICT = "strict"
RUN_MODE_DIAGNOSTIC = "diagnostic"

STATUS_COMPLETED = "completed"
STATUS_BLOCKED_UNSUPPORTED_EVENT = "blocked_by_unsupported_event"
STATUS_ACCOUNTING_ERROR = "accounting_error"


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
    execution_price: float | None
    price_date: date | None
    price_kind: str  # "current_session_close" | "none"
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
    nonzero_trade_count: int
    unavailable_target_count: int
    frozen_count: int
    restricted_binding_count: int
    gross_book_restricted_binding_count: int
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
class FailedAttempt:
    """A session whose accounting validation failed; kept apart from valid logs."""

    trade_date: date
    reason: str
    trades: tuple[TradeRecord, ...]
    rebalance: RebalanceRecord | None


@dataclass(frozen=True)
class LifecycleEvent:
    """An unsupported instrument lifecycle event affecting a held position."""

    event_id: str
    instrument_id: str
    event_type: str  # "delist" | "code_change" | "conflict"
    event_date: date  # original event date from the source data
    blocking_session: date  # first session it blocks in this run
    book: str  # "gross" | "net"
    position_value: float
    last_mark_date: date | None
    description: str


@dataclass(frozen=True)
class AccountingResidual:
    """Max absolute and relative residual for one final-accounting check.

    ``max_abs`` and ``max_rel`` are tracked independently, so their occurrence
    date / book may differ.
    """

    check: str
    max_abs: float
    max_abs_date: date | None
    max_abs_book: str | None
    max_rel: float
    max_rel_date: date | None
    max_rel_book: str | None


@dataclass(frozen=True)
class BacktestResult:
    """Full output of a backtest run."""

    run_mode: str
    status: str
    requested_period_start: date | None
    requested_period_end: date | None
    simulated_period_start: date | None
    simulated_period_end: date | None
    valid_through: date | None
    diagnostic_from: date | None
    first_blocking_event: LifecycleEvent | None
    records: list[DailyBacktestRecord]
    rebalances: list[RebalanceRecord]
    books: list[BookSnapshot]
    trades: list[TradeRecord]
    skipped_executions: list[SkippedExecution]
    lifecycle_events: list[LifecycleEvent]
    failed_attempts: list[FailedAttempt]
    solver_root_residual: float
    accounting_checks: list[AccountingResidual]
    accounting_error: str | None
    accounting_error_date: date | None
    accounting_error_book: str | None
