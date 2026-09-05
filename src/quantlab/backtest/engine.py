"""Idealized research backtest engine."""

from __future__ import annotations

from datetime import date

import pandas as pd

from quantlab.backtest.models import BacktestConfig, DailyBacktestRecord, RebalanceRecord
from quantlab.portfolio.models import TargetPortfolio


def weekly_signal_dates(open_trade_dates: list[date]) -> list[date]:
    """Return the last open market session of each calendar week."""
    dates = sorted(set(open_trade_dates))
    if not dates:
        return []
    result: list[date] = []
    prev_week = None
    for i, d in enumerate(dates):
        week = d.isocalendar()[:2]
        if week != prev_week:
            if prev_week is not None:
                result.append(dates[i - 1])
            prev_week = week
    result.append(dates[-1])
    return result


def _next_session(open_dates: list[date], signal_date: date, lag: int) -> date | None:
    try:
        idx = open_dates.index(signal_date)
    except ValueError:
        return None
    if idx + lag >= len(open_dates):
        return None
    return open_dates[idx + lag]


def run_backtest(
    price_frame: pd.DataFrame,
    open_dates: list[date],
    targets: dict[date, TargetPortfolio],
    config: BacktestConfig,
    execution_lag_sessions: int = 1,
) -> tuple[list[DailyBacktestRecord], list[RebalanceRecord]]:
    """Simulate an idealized long-only portfolio.

    Timing: a signal at session T rebalances at the close of the next open
    session (T+1); the new portfolio starts earning from T+1 close onward. PnL
    is computed from chronological adjusted closes, never from ``future_return``
    labels.

    Suspended held positions are marked with return 0 (portfolio marking, not
    feature fill); a new target with no execution-date bar is not opened and the
    corresponding cash stays uninvested.
    """
    dates = sorted(set(open_dates))
    execution_map: dict[date, tuple[date, TargetPortfolio]] = {}
    for signal_date, target in targets.items():
        exec_date = _next_session(dates, signal_date, execution_lag_sessions)
        if exec_date is not None:
            execution_map[exec_date] = (signal_date, target)

    price = price_frame.pivot(index="trade_date", columns="instrument_id", values="adj_close")

    cash_value = config.initial_nav
    positions_value: dict[str, float] = {}
    prev_nav_gross = config.initial_nav
    prev_nav_net = config.initial_nav
    prev_prices: dict[str, float] = {}

    records: list[DailyBacktestRecord] = []
    rebalance_log: list[RebalanceRecord] = []

    for trade_date in dates:
        current_prices: dict[str, float] = {}
        if trade_date in price.index:
            current_prices = price.loc[trade_date].dropna().to_dict()

        # 1. mark-to-market: held positions earn prev->current close return
        for instr in list(positions_value):
            if instr in prev_prices and instr in current_prices:
                r = current_prices[instr] / prev_prices[instr] - 1
                positions_value[instr] *= 1 + r
            # held but missing either price -> mark unchanged (return 0)

        nav_gross = cash_value + sum(positions_value.values())

        # 2. execution
        turnover = 0.0
        traded_ratio = 0.0
        cost = 0.0
        if trade_date in execution_map:
            signal_date, target = execution_map[trade_date]
            target_weights = {p.instrument_id: p.target_weight for p in target.positions}
            target_cash = target.cash_weight

            pre_gross = sum(abs(v) for v in positions_value.values()) / nav_gross

            all_instr = set(target_weights) | set(positions_value)
            traded_value = sum(
                abs(target_weights.get(i, 0.0) * nav_gross - positions_value.get(i, 0.0))
                for i in all_instr
            )
            traded_ratio = traded_value / nav_gross if nav_gross > 0 else 0.0
            turnover = 0.5 * traded_ratio
            cost = traded_ratio * config.cost_rate * nav_gross

            unavailable = 0
            new_positions: dict[str, float] = {}
            for instr, w in target_weights.items():
                if w == 0.0:
                    continue
                if instr not in current_prices:
                    unavailable += 1
                    target_cash += w
                else:
                    new_positions[instr] = w * nav_gross

            positions_value = new_positions
            cash_value = nav_gross - cost - sum(positions_value.values())

            rebalance_log.append(RebalanceRecord(
                signal_date=signal_date,
                execution_date=trade_date,
                target_count=len(target.positions),
                filled_target_count=len(new_positions),
                unavailable_target_count=unavailable,
                pre_trade_gross_exposure=pre_gross,
                post_trade_gross_exposure=sum(abs(v) for v in new_positions.values()) / nav_gross
                if nav_gross > 0
                else 0.0,
                traded_notional_ratio=traded_ratio,
                turnover=turnover,
                transaction_cost=cost,
            ))

        nav_net = nav_gross - cost

        gross_exp = sum(abs(v) for v in positions_value.values()) / nav_net if nav_net > 0 else 0.0
        net_exp = sum(positions_value.values()) / nav_net if nav_net > 0 else 0.0
        cash_weight = cash_value / nav_net if nav_net > 0 else 0.0

        daily_ret_gross = nav_gross / prev_nav_gross - 1 if prev_nav_gross > 0 else 0.0
        daily_ret_net = nav_net / prev_nav_net - 1 if prev_nav_net > 0 else 0.0

        records.append(DailyBacktestRecord(
            trade_date=trade_date,
            nav_gross=nav_gross,
            nav_net=nav_net,
            daily_return_gross=daily_ret_gross,
            daily_return_net=daily_ret_net,
            gross_exposure=gross_exp,
            net_exposure=net_exp,
            cash_weight=cash_weight,
            turnover=turnover,
            traded_notional_ratio=traded_ratio,
            transaction_cost=cost,
            holdings_count=len(positions_value),
        ))

        prev_nav_gross = nav_gross
        prev_nav_net = nav_net
        prev_prices = current_prices

    return records, rebalance_log
