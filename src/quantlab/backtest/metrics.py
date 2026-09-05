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


def _mean_var(values: list[float]) -> tuple[float, float, float]:
    """Return (mean, variance, n). ddof=0 (population)."""
    n = len(values)
    if n == 0:
        return float("nan"), float("nan"), 0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, var, n


def _sharpe(mean: float, var: float, annualization: int) -> float:
    if var is None or var <= 0 or math.isnan(var):
        return float("nan")
    return mean / math.sqrt(var) * math.sqrt(annualization)


def _vol(var: float, annualization: int) -> float:
    if var is None or math.isnan(var):
        return float("nan")
    return math.sqrt(var) * math.sqrt(annualization)


def compute_metrics(
    records: list[DailyBacktestRecord],
    rebalance_log: list[RebalanceRecord],
    config: BacktestConfig,
) -> dict:
    """Compute performance metrics for a backtest run.

    Return-dependent statistics use ``records[1:]`` (``len(records) - 1`` return
    intervals), so the initial placeholder return is never an independent
    period. Suffix-less turnover/exposure/holdings fields describe the net book;
    gross book metrics carry an explicit ``gross_book_`` prefix. Sharpe uses
    rf=0 and population (ddof=0) variance.
    """
    if not records:
        return {}

    annualization = config.annualization
    intervals = len(records) - 1

    nav_gross = [r.nav_gross for r in records]
    nav_net = [r.nav_net for r in records]
    ret_gross = [r.daily_return_gross for r in records[1:]]
    ret_net = [r.daily_return_net for r in records[1:]]

    mean_gross, var_gross, _ = _mean_var(ret_gross)
    mean_net, var_net, _ = _mean_var(ret_net)

    vol_gross = _vol(var_gross, annualization)
    vol_net = _vol(var_net, annualization)
    sharpe_gross = _sharpe(mean_gross, var_gross, annualization)
    sharpe_net = _sharpe(mean_net, var_net, annualization)

    total_return_gross = nav_gross[-1] / nav_gross[0] - 1
    total_return_net = nav_net[-1] / nav_net[0] - 1

    cagr_gross = _cagr(nav_gross[0], nav_gross[-1], intervals, annualization)
    cagr_net = _cagr(nav_net[0], nav_net[-1], intervals, annualization)

    total_turnover = sum(r.turnover for r in records)
    gross_total_turnover = sum(r.gross_book_turnover for r in records)
    total_cost = sum(r.transaction_cost for r in records)
    rebalance_count = len(rebalance_log)

    def _avg_turnover(total: float, denom: float) -> float:
        return total / denom if denom > 0 else float("nan")

    average_daily_turnover = _avg_turnover(total_turnover, intervals)
    average_rebalance_turnover = _avg_turnover(total_turnover, rebalance_count)
    annualized_turnover = (
        total_turnover / (intervals / annualization) if intervals > 0 else float("nan")
    )

    gross_average_daily_turnover = _avg_turnover(gross_total_turnover, intervals)
    gross_average_rebalance_turnover = _avg_turnover(
        gross_total_turnover, rebalance_count
    )
    gross_annualized_turnover = (
        gross_total_turnover / (intervals / annualization)
        if intervals > 0
        else float("nan")
    )

    cumulative_cost_paid_vs_initial_nav = total_cost / config.initial_nav
    terminal_return_cost_drag = total_return_gross - total_return_net
    cagr_cost_drag = cagr_gross - cagr_net
    annualized_traded_notional = 2 * annualized_turnover if intervals > 0 else float("nan")
    implied_annual_cost_rate = annualized_traded_notional * config.cost_rate

    return {
        "n_records": len(records),
        "n_return_intervals": intervals,
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
        "gross_book_total_turnover": gross_total_turnover,
        "gross_book_average_daily_turnover": gross_average_daily_turnover,
        "gross_book_average_rebalance_turnover": gross_average_rebalance_turnover,
        "gross_book_annualized_turnover": gross_annualized_turnover,
        "average_holdings": sum(r.holdings_count for r in records) / len(records),
        "average_gross_exposure": sum(r.gross_exposure for r in records) / len(records),
        "average_cash_weight": sum(r.cash_weight for r in records) / len(records),
        "gross_book_average_holdings": sum(r.gross_book_holdings_count for r in records)
        / len(records),
        "gross_book_average_gross_exposure": sum(
            r.gross_book_gross_exposure for r in records
        )
        / len(records),
        "gross_book_average_cash_weight": sum(
            r.gross_book_cash_weight for r in records
        )
        / len(records),
        "rebalance_count": rebalance_count,
        "unavailable_execution_count": sum(
            r.unavailable_target_count for r in rebalance_log
        ),
    }
