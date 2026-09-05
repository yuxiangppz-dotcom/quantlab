"""Research backtest."""

from quantlab.backtest.engine import (
    MISSING_PRICE_POLICY,
    run_backtest,
    weekly_signal_dates,
)
from quantlab.backtest.lifecycle import LifecycleMonitor
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
    LifecycleEvent,
    PositionRecord,
    RebalanceRecord,
    SkippedExecution,
    TradeRecord,
)

__all__ = [
    "AccountingResidual",
    "BacktestConfig",
    "BacktestResult",
    "BookSnapshot",
    "DailyBacktestRecord",
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
    "compute_metrics",
    "run_backtest",
    "weekly_signal_dates",
]
