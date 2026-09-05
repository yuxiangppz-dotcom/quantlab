"""Idealized research backtest engine."""

from __future__ import annotations

from datetime import date

import pandas as pd

from quantlab.backtest.models import BacktestConfig, DailyBacktestRecord, RebalanceRecord
from quantlab.portfolio.models import TargetPortfolio

HELD_MISSING_BAR_REBALANCE_POLICY = "stale_mark_idealized"


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
    """Simulate an idealized portfolio with independent gross and net NAV.

    State is weight-based: a single set of portfolio weights (``current_weights``
    plus ``cash_weight``) drifts naturally with asset returns, while two NAV
    scalars (``gross_nav`` and ``net_nav``) evolve independently:

    - market returns multiply both NAVs equally;
    - transaction cost permanently reduces only ``net_nav``.

    Historical cost therefore never re-enters gross NAV.

    Timing: a signal at session T rebalances at the close of the next open
    session (T+1); the new portfolio starts earning from T+1 close onward. PnL
    is computed from chronological adjusted closes, never from ``future_return``
    labels.

    Held positions with a missing bar are marked with return 0 using their last
    available price, which is preserved across suspension gaps
    (``held_missing_bar_rebalance_policy = "stale_mark_idealized"``). A new
    target with no execution-date bar is not opened and its allocation stays in
    cash without incurring cost.
    """
    dates = sorted(set(open_dates))
    execution_map: dict[date, tuple[date, TargetPortfolio]] = {}
    for signal_date, target in targets.items():
        exec_date = _next_session(dates, signal_date, execution_lag_sessions)
        if exec_date is not None:
            execution_map[exec_date] = (signal_date, target)

    price = price_frame.pivot(index="trade_date", columns="instrument_id", values="adj_close")

    gross_nav = config.initial_nav
    net_nav = config.initial_nav
    weights: dict[str, float] = {}
    cash_weight = 1.0
    last_price: dict[str, float] = {}

    records: list[DailyBacktestRecord] = []
    rebalance_log: list[RebalanceRecord] = []

    prev_gross_nav = gross_nav
    prev_net_nav = net_nav

    for trade_date in dates:
        current_prices: dict[str, float] = {}
        if trade_date in price.index:
            current_prices = price.loc[trade_date].dropna().to_dict()

        # 1. mark-to-market drift: held positions earn last_available -> current
        asset_returns: dict[str, float] = {}
        for instr in list(weights):
            if instr in current_prices:
                lp = last_price.get(instr)
                r = current_prices[instr] / lp - 1 if lp is not None and lp > 0 else 0.0
                asset_returns[instr] = r
                last_price[instr] = current_prices[instr]
            else:
                asset_returns[instr] = 0.0

        portfolio_return = sum(w * asset_returns.get(i, 0.0) for i, w in weights.items())

        gross_nav *= 1 + portfolio_return
        net_nav *= 1 + portfolio_return

        denom = 1 + portfolio_return
        if denom != 0.0:
            weights = {
                i: w * (1 + asset_returns.get(i, 0.0)) / denom for i, w in weights.items()
            }
            cash_weight = cash_weight / denom

        # 2. execution
        turnover = 0.0
        traded_ratio = 0.0
        cost = 0.0
        if trade_date in execution_map:
            signal_date, target = execution_map[trade_date]
            pre_trade_gross = sum(abs(w) for w in weights.values())

            target_weights = {p.instrument_id: p.target_weight for p in target.positions}
            effective: dict[str, float] = {}
            unavailable = 0
            moved_to_cash = 0.0
            for instr, w in target_weights.items():
                if w == 0.0:
                    continue
                if instr not in current_prices and instr not in weights:
                    unavailable += 1
                    moved_to_cash += w
                else:
                    effective[instr] = w
                    if instr not in weights:
                        last_price[instr] = current_prices[instr]

            effective_cash = target.cash_weight + moved_to_cash

            all_risky = set(weights) | set(effective)
            traded_ratio = sum(
                abs(effective.get(i, 0.0) - weights.get(i, 0.0)) for i in all_risky
            )
            turnover = 0.5 * traded_ratio
            cost_fraction = traded_ratio * config.cost_rate
            cost = net_nav * cost_fraction
            net_nav *= 1 - cost_fraction

            weights = effective
            cash_weight = effective_cash

            for instr in list(last_price):
                if instr not in weights:
                    del last_price[instr]

            post_trade_gross = sum(abs(w) for w in weights.values())

            rebalance_log.append(
                RebalanceRecord(
                    signal_date=signal_date,
                    execution_date=trade_date,
                    target_count=len(target.positions),
                    filled_target_count=len(effective),
                    unavailable_target_count=unavailable,
                    pre_trade_gross_exposure=pre_trade_gross,
                    post_trade_gross_exposure=post_trade_gross,
                    traded_notional_ratio=traded_ratio,
                    turnover=turnover,
                    transaction_cost=cost,
                )
            )

        gross_exposure = sum(abs(w) for w in weights.values())
        net_exposure = sum(weights.values())

        daily_return_gross = gross_nav / prev_gross_nav - 1 if prev_gross_nav > 0 else 0.0
        daily_return_net = net_nav / prev_net_nav - 1 if prev_net_nav > 0 else 0.0

        records.append(
            DailyBacktestRecord(
                trade_date=trade_date,
                nav_gross=gross_nav,
                nav_net=net_nav,
                daily_return_gross=daily_return_gross,
                daily_return_net=daily_return_net,
                gross_exposure=gross_exposure,
                net_exposure=net_exposure,
                cash_weight=cash_weight,
                turnover=turnover,
                traded_notional_ratio=traded_ratio,
                transaction_cost=cost,
                holdings_count=len(weights),
            )
        )

        prev_gross_nav = gross_nav
        prev_net_nav = net_nav

    return records, rebalance_log
