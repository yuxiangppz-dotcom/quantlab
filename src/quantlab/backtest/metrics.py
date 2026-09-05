"""Backtest performance metrics."""

from __future__ import annotations

import math

from quantlab.backtest.models import BacktestConfig, DailyBacktestRecord, RebalanceRecord


def _cagr(start: float, end: float, periods: int, annualization: int) -> float:
    if start <= 0 or periods <= 0:
        return float("nan")
    return (end / start) ** (annualization / periods) - 1


def _max_drawdown(navs: list[float]) -> float:
    peak = navs[0]
    max_dd = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_dd = min(max_dd, nav / peak - 1)
    return max_dd


def compute_metrics(
    records: list[DailyBacktestRecord],
    rebalance_log: list[RebalanceRecord],
    config: BacktestConfig,
) -> dict:
    """Compute performance metrics for a backtest run."""
    if not records:
        return {}

    annualization = config.annualization
    periods = len(records)

    nav_gross = [r.nav_gross for r in records]
    nav_net = [r.nav_net for r in records]
    ret_gross = [r.daily_return_gross for r in records]
    ret_net = [r.daily_return_net for r in records]

    mean_gross = sum(ret_gross) / periods
    mean_net = sum(ret_net) / periods
    var_gross = sum((r - mean_gross) ** 2 for r in ret_gross) / periods
    var_net = sum((r - mean_net) ** 2 for r in ret_net) / periods
    vol_gross = math.sqrt(var_gross) * math.sqrt(annualization)
    vol_net = math.sqrt(var_net) * math.sqrt(annualization)

    if var_gross > 0:
        sharpe_gross = mean_gross / math.sqrt(var_gross) * math.sqrt(annualization)
    else:
        sharpe_gross = float("nan")
    if var_net > 0:
        sharpe_net = mean_net / math.sqrt(var_net) * math.sqrt(annualization)
    else:
        sharpe_net = float("nan")

    total_cost = sum(r.transaction_cost for r in records)
    total_turnover = sum(r.turnover for r in records)

    return {
        "total_return_gross": nav_gross[-1] / nav_gross[0] - 1,
        "total_return_net": nav_net[-1] / nav_net[0] - 1,
        "cagr_gross": _cagr(nav_gross[0], nav_gross[-1], periods, annualization),
        "cagr_net": _cagr(nav_net[0], nav_net[-1], periods, annualization),
        "annualized_volatility_gross": vol_gross,
        "annualized_volatility_net": vol_net,
        "sharpe_gross": sharpe_gross,
        "sharpe_net": sharpe_net,
        "max_drawdown_gross": _max_drawdown(nav_gross),
        "max_drawdown_net": _max_drawdown(nav_net),
        "average_turnover": total_turnover / periods,
        "annualized_turnover": total_turnover * annualization / periods,
        "total_transaction_cost": total_cost,
        "cost_drag": total_cost / config.initial_nav,
        "average_holdings": sum(r.holdings_count for r in records) / periods,
        "average_gross_exposure": sum(r.gross_exposure for r in records) / periods,
        "average_cash_weight": sum(r.cash_weight for r in records) / periods,
        "rebalance_count": len(rebalance_log),
        "unavailable_execution_count": sum(
            r.unavailable_target_count for r in rebalance_log
        ),
    }
