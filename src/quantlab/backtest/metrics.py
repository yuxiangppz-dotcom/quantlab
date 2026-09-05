"""Backtest performance metrics."""

from __future__ import annotations

import math

from quantlab.backtest.models import BacktestConfig, DailyBacktestRecord, RebalanceRecord


def _cagr(start: float, end: float, intervals: int, annualization: int) -> float:
    if start <= 0 or intervals <= 0:
        return float("nan")
    return (end / start) ** (annualization / intervals) - 1


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
    """Compute performance metrics for a backtest run.

    Return-dependent statistics (mean, volatility, Sharpe) use ``records[1:]``,
    i.e. ``len(records) - 1`` return intervals, so the initial placeholder
    return is never treated as an independent investment period.
    """
    if not records:
        return {}

    annualization = config.annualization
    intervals = len(records) - 1

    nav_gross = [r.nav_gross for r in records]
    nav_net = [r.nav_net for r in records]

    ret_gross = [r.daily_return_gross for r in records[1:]]
    ret_net = [r.daily_return_net for r in records[1:]]
    n_obs = len(ret_gross)

    if n_obs > 0:
        mean_gross = sum(ret_gross) / n_obs
        mean_net = sum(ret_net) / n_obs
        var_gross = sum((r - mean_gross) ** 2 for r in ret_gross) / n_obs
        var_net = sum((r - mean_net) ** 2 for r in ret_net) / n_obs
    else:
        mean_gross = mean_net = 0.0
        var_gross = var_net = 0.0

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

    total_return_gross = nav_gross[-1] / nav_gross[0] - 1
    total_return_net = nav_net[-1] / nav_net[0] - 1

    cagr_gross = _cagr(nav_gross[0], nav_gross[-1], intervals, annualization)
    cagr_net = _cagr(nav_net[0], nav_net[-1], intervals, annualization)

    total_turnover = sum(r.turnover for r in records)
    total_cost = sum(r.transaction_cost for r in records)
    rebalance_count = len(rebalance_log)

    average_daily_turnover = total_turnover / intervals if intervals > 0 else float("nan")
    average_rebalance_turnover = (
        total_turnover / rebalance_count if rebalance_count > 0 else float("nan")
    )
    annualized_turnover = (
        total_turnover / (intervals / annualization) if intervals > 0 else float("nan")
    )

    cumulative_cost_paid_vs_initial_nav = total_cost / config.initial_nav
    terminal_return_cost_drag = total_return_gross - total_return_net
    cagr_cost_drag = cagr_gross - cagr_net
    annualized_traded_notional = (
        2 * annualized_turnover if intervals > 0 else float("nan")
    )
    implied_annual_cost_rate = annualized_traded_notional * config.cost_rate

    return {
        "total_return_gross": total_return_gross,
        "total_return_net": total_return_net,
        "cagr_gross": cagr_gross,
        "cagr_net": cagr_net,
        "annualized_volatility_gross": vol_gross,
        "annualized_volatility_net": vol_net,
        "sharpe_gross": sharpe_gross,
        "sharpe_net": sharpe_net,
        "max_drawdown_gross": _max_drawdown(nav_gross),
        "max_drawdown_net": _max_drawdown(nav_net),
        "total_turnover": total_turnover,
        "average_daily_turnover": average_daily_turnover,
        "average_rebalance_turnover": average_rebalance_turnover,
        "annualized_turnover": annualized_turnover,
        "total_transaction_cost": total_cost,
        "cumulative_cost_paid_vs_initial_nav": cumulative_cost_paid_vs_initial_nav,
        "terminal_return_cost_drag": terminal_return_cost_drag,
        "cagr_cost_drag": cagr_cost_drag,
        "annualized_traded_notional": annualized_traded_notional,
        "implied_annual_cost_rate": implied_annual_cost_rate,
        "average_holdings": sum(r.holdings_count for r in records) / len(records),
        "average_gross_exposure": sum(r.gross_exposure for r in records) / len(records),
        "average_cash_weight": sum(r.cash_weight for r in records) / len(records),
        "rebalance_count": rebalance_count,
        "unavailable_execution_count": sum(
            r.unavailable_target_count for r in rebalance_log
        ),
    }
