"""Research backtest."""

from quantlab.backtest.engine import (
    MISSING_PRICE_POLICY,
    run_backtest,
    weekly_signal_dates,
)
from quantlab.backtest.lifecycle import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    LEGACY_DELIST_DATE_INCLUSIVE,
    LifecycleMonitor,
)
from quantlab.backtest.metrics import compute_metrics
from quantlab.backtest.models import (
    RUN_MODE_DIAGNOSTIC,
    RUN_MODE_STRICT,
    STATUS_ACCOUNTING_ERROR,
    STATUS_BLOCKED_UNSUPPORTED_EVENT,
    STATUS_COMPLETED,
    AccountingResidual,
    BacktestConfig,
    BacktestResult,
    BookSnapshot,
    DailyBacktestRecord,
    FailedAttempt,
    LifecycleEvent,
    PositionRecord,
    RebalanceRecord,
    SkippedExecution,
    TradeRecord,
)
from quantlab.backtest.report import build_report

__all__ = [
    "AccountingResidual",
    "BacktestConfig",
    "BacktestResult",
    "BookSnapshot",
    "DELIST_DATE_IS_FIRST_INVALID_V1",
    "DailyBacktestRecord",
    "FailedAttempt",
    "LEGACY_DELIST_DATE_INCLUSIVE",
    "LifecycleEvent",
    "LifecycleMonitor",
    "MISSING_PRICE_POLICY",
    "PositionRecord",
    "RebalanceRecord",
    "RUN_MODE_DIAGNOSTIC",
    "RUN_MODE_STRICT",
    "SkippedExecution",
    "STATUS_ACCOUNTING_ERROR",
    "STATUS_BLOCKED_UNSUPPORTED_EVENT",
    "STATUS_COMPLETED",
    "TradeRecord",
    "build_report",
    "compute_metrics",
    "run_backtest",
    "weekly_signal_dates",
]
