"""Idealized research backtest engine with dual independent ledgers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from quantlab.backtest.models import (
    BacktestConfig,
    BacktestResult,
    BookSnapshot,
    DailyBacktestRecord,
    PositionRecord,
    RebalanceRecord,
    SkippedExecution,
    TradeRecord,
)
from quantlab.portfolio.models import TargetPortfolio

MISSING_PRICE_POLICY = "freeze_held_no_price"

_TOL = 1e-12
_TRADE_TOL = 1e-9
_ROOT_MAX_ITER = 200


@dataclass
class _Position:
    value: float
    last_price: float | None
    last_mark_date: date | None


class _Book:
    """A single independent ledger (gross or net)."""

    def __init__(self, initial_nav: float) -> None:
        self.cash = initial_nav
        self.positions: dict[str, _Position] = {}
        self.prev_nav = initial_nav

    def nav(self) -> float:
        return self.cash + sum(p.value for p in self.positions.values())

    def mark_to_market(self, current_prices: dict[str, float], trade_date: date) -> float:
        """Value held positions at current prices; return market pnl."""
        nav_before = self.nav()
        for instr, pos in self.positions.items():
            price = current_prices.get(instr)
            if price is None:
                continue
            if pos.last_price is None or pos.last_price <= 0:
                raise ValueError(
                    f"position {instr} has no trusted initial mark on {trade_date}"
                )
            pos.value *= price / pos.last_price
            pos.last_price = price
            pos.last_mark_date = trade_date
        return self.nav() - nav_before


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


def _validate_price_frame(price_frame: pd.DataFrame) -> pd.DataFrame:
    required = {"instrument_id", "trade_date", "adj_close"}
    missing = required - set(price_frame.columns)
    if missing:
        raise ValueError(f"price_frame missing columns: {sorted(missing)}")
    frame = price_frame[["instrument_id", "trade_date", "adj_close"]].copy()

    if frame["instrument_id"].isna().any():
        raise ValueError("price_frame has null instrument_id")
    if frame["trade_date"].isna().any():
        raise ValueError("price_frame has null trade_date")

    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date

    dup = frame.duplicated(subset=["instrument_id", "trade_date"], keep=False)
    if dup.any():
        sample = (
            frame.loc[dup, ["instrument_id", "trade_date"]].drop_duplicates().to_dict("records")
        )
        raise ValueError(f"price_frame has duplicate instrument/date rows: {sample}")

    prices = frame["adj_close"]
    non_null = prices[prices.notna()]
    if not non_null.map(math.isfinite).all():
        raise ValueError("price_frame adj_close must be finite when present")
    if (non_null <= 0).any():
        raise ValueError("price_frame adj_close must be > 0 when present")

    return frame


def _validate_targets(targets: dict[date, TargetPortfolio]) -> None:
    for signal_date, target in targets.items():
        if signal_date != target.as_of:
            raise ValueError(
                f"targets key {signal_date} does not match target.as_of {target.as_of}"
            )
        for pos in target.positions:
            if pos.target_weight < 0:
                raise ValueError(
                    f"backtest is long-only; negative weight for {pos.instrument_id}"
                )
        if target.cash_weight < 0:
            raise ValueError("target cash_weight must be >= 0 in long-only backtest")
        if target.gross_exposure > 1.0 + 1e-9:
            raise ValueError(f"target gross exposure {target.gross_exposure} exceeds 1.0")


def _solve_self_financing(
    v_minus: float,
    cost_rate: float,
    frozen_value: float,
    traded_value_fn,
) -> tuple[float, float]:
    """Solve ``v + cost_rate * traded_value(v) = v_minus``.

    Returns ``(v, residual)``. In the long-only, ``0 <= cost_rate < 1`` domain
    the left-hand side is monotonic increasing (derivative ``>= 1 - cost_rate``),
    so bisection on ``[frozen_value, v_minus]`` has a unique root.
    """
    if cost_rate == 0.0:
        return v_minus, 0.0

    lo = frozen_value
    hi = v_minus
    for _ in range(_ROOT_MAX_ITER):
        mid = 0.5 * (lo + hi)
        g = mid + cost_rate * traded_value_fn(mid) - v_minus
        if g > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo <= _TOL * max(1.0, abs(v_minus)):
            break
    v = 0.5 * (lo + hi)
    residual = v + cost_rate * traded_value_fn(v) - v_minus
    return v, abs(residual)


def _rebalance(
    book: _Book,
    target: TargetPortfolio,
    current_prices: dict[str, float],
    cost_rate: float,
    signal_date: date,
    execution_date: date,
) -> tuple[dict, float, float]:
    """Apply a target to one book using the freeze-first allocation rule."""
    v_minus = book.nav()
    positions_before = {i: p.value for i, p in book.positions.items()}

    target_weights = {p.instrument_id: p.target_weight for p in target.positions}

    frozen: set[str] = set()
    unavailable_new: dict[str, float] = {}
    tradable: dict[str, float] = {}
    for instr, w in target_weights.items():
        if w == 0.0:
            continue
        if instr in current_prices:
            tradable[instr] = w
        elif instr in book.positions:
            frozen.add(instr)
        else:
            unavailable_new[instr] = w
    for instr in book.positions:
        if instr not in current_prices:
            frozen.add(instr)

    frozen_value = sum(positions_before[i] for i in frozen)
    q = target.cash_weight + sum(unavailable_new.values())
    s = sum(tradable.values())

    involved = set(positions_before) | set(tradable) | set(unavailable_new)

    def final_value(v: float, instr: str) -> float:
        if instr in frozen:
            return positions_before[instr]
        if instr in tradable:
            if s > 0 and v > 0:
                budget = max(0.0, (1.0 - q) * v - frozen_value)
                lam = min(1.0, budget / (s * v))
                return lam * tradable[instr] * v
            return 0.0
        return 0.0

    def traded_value(v: float) -> float:
        return sum(
            abs(final_value(v, i) - positions_before.get(i, 0.0)) for i in involved
        )

    v, residual = _solve_self_financing(
        v_minus=v_minus,
        cost_rate=cost_rate,
        frozen_value=frozen_value,
        traded_value_fn=traded_value,
    )
    cost = v_minus - v
    if abs(cost) <= _TRADE_TOL:
        cost = 0.0
        v = v_minus

    final_values: dict[str, float] = {}
    for instr in involved:
        xp = final_value(v, instr)
        x_minus = positions_before.get(instr, 0.0)
        if abs(xp - x_minus) <= _TRADE_TOL:
            xp = x_minus
        final_values[instr] = xp

    old_positions = book.positions
    new_positions: dict[str, _Position] = {}
    for instr, xp in final_values.items():
        if xp <= 0.0:
            continue
        if instr in old_positions:
            pos = old_positions[instr]
            pos.value = xp
            new_positions[instr] = pos
        else:
            new_positions[instr] = _Position(
                value=xp,
                last_price=current_prices[instr],
                last_mark_date=execution_date,
            )
    book.positions = new_positions
    book.cash = v - sum(p.value for p in new_positions.values())

    buy_value = 0.0
    sell_value = 0.0
    nonzero_trades = 0
    trade_details: list[tuple[str, float, float, float, float, float, str]] = []
    for instr in involved:
        x_minus = positions_before.get(instr, 0.0)
        x_plus = final_values[instr]
        signed = x_plus - x_minus
        if signed > _TRADE_TOL:
            buy_value += signed
        elif signed < -_TRADE_TOL:
            sell_value += -signed
        if abs(signed) > _TRADE_TOL:
            nonzero_trades += 1
        if instr in frozen:
            reason = "frozen_held_no_price"
        elif instr in unavailable_new:
            reason = "new_target_no_price"
        elif abs(signed) <= _TOL:
            reason = "no_change"
        elif signed < 0:
            reason = "sold"
        else:
            reason = "traded"
        actual_weight = x_plus / v if v > 0 else 0.0
        trade_details.append(
            (
                instr,
                x_minus,
                x_plus,
                signed,
                target_weights.get(instr, 0.0),
                actual_weight,
                reason,
            )
        )

    buy_ratio = buy_value / v_minus if v_minus > 0 else 0.0
    sell_ratio = sell_value / v_minus if v_minus > 0 else 0.0
    traded_ratio = buy_ratio + sell_ratio
    turnover = 0.5 * traded_ratio

    post_gross_exposure = (
        sum(abs(p.value) for p in new_positions.values()) / v if v > 0 else 0.0
    )
    pre_gross_exposure = (
        sum(abs(pv) for pv in positions_before.values()) / v_minus if v_minus > 0 else 0.0
    )

    actual_weights = {i: p.value / v for i, p in new_positions.items()} if v > 0 else {}
    deviation = 0.0
    for instr in set(target_weights) | set(actual_weights):
        deviation += abs(actual_weights.get(instr, 0.0) - target_weights.get(instr, 0.0))
    deviation += abs((book.cash / v if v > 0 else 0.0) - target.cash_weight)

    summary = {
        "buy_ratio": buy_ratio,
        "sell_ratio": sell_ratio,
        "traded_ratio": traded_ratio,
        "turnover": turnover,
        "pre_gross_exposure": pre_gross_exposure,
        "post_gross_exposure": post_gross_exposure,
        "deviation": deviation,
        "frozen_count": len(frozen),
        "unavailable_count": len(unavailable_new),
        "nonzero_trades": nonzero_trades,
        "trade_details": trade_details,
    }
    return summary, cost, residual


def run_backtest(
    price_frame: pd.DataFrame,
    open_dates: list[date],
    targets: dict[date, TargetPortfolio],
    config: BacktestConfig,
    execution_lag_sessions: int = 1,
) -> BacktestResult:
    """Simulate dual gross/net ledgers with self-financing cost.

    Both books share the same market inputs, target sequence and valuation
    logic; the gross book runs with ``cost_rate = 0`` and the net book with
    ``config.cost_rate``. Positions are simulated as adjusted-close value
    amounts (not share counts). Held positions with no current price are frozen
    and cannot trade (``freeze_held_no_price``).
    """
    if (
        isinstance(execution_lag_sessions, bool)
        or not isinstance(execution_lag_sessions, int)
        or execution_lag_sessions <= 0
    ):
        raise ValueError(
            f"execution_lag_sessions must be a positive integer, "
            f"got {execution_lag_sessions!r}"
        )

    frame = _validate_price_frame(price_frame)
    _validate_targets(targets)

    dates = sorted(set(open_dates))
    if not dates:
        raise ValueError("open_dates must not be empty")

    price = frame.pivot(index="trade_date", columns="instrument_id", values="adj_close")

    execution_map: dict[date, tuple[date, TargetPortfolio]] = {}
    skipped: list[SkippedExecution] = []
    for signal_date, target in targets.items():
        if signal_date not in dates:
            raise ValueError(f"signal date {signal_date} is not a simulation session")
        exec_date = _next_session(dates, signal_date, execution_lag_sessions)
        if exec_date is None:
            skipped.append(
                SkippedExecution(
                    signal_date=signal_date,
                    reason="execution_beyond_simulation_end",
                )
            )
        else:
            execution_map[exec_date] = (signal_date, target)

    gross_book = _Book(config.initial_nav)
    net_book = _Book(config.initial_nav)

    records: list[DailyBacktestRecord] = []
    rebalances: list[RebalanceRecord] = []
    books: list[BookSnapshot] = []
    trades: list[TradeRecord] = []
    max_residual = 0.0

    for trade_date in dates:
        current_prices: dict[str, float] = {}
        if trade_date in price.index:
            current_prices = price.loc[trade_date].dropna().to_dict()

        gross_market_pnl = gross_book.mark_to_market(current_prices, trade_date)
        net_market_pnl = net_book.mark_to_market(current_prices, trade_date)

        gross_cost = 0.0
        net_cost = 0.0
        gross_turnover = 0.0
        net_turnover = 0.0
        gross_traded_ratio = 0.0
        net_traded_ratio = 0.0

        if trade_date in execution_map:
            signal_date, target = execution_map[trade_date]

            gross_summary, gross_cost, _ = _rebalance(
                gross_book, target, current_prices, 0.0, signal_date, trade_date
            )
            net_summary, net_cost, residual = _rebalance(
                net_book, target, current_prices, config.cost_rate, signal_date, trade_date
            )
            max_residual = max(max_residual, residual)

            gross_turnover = gross_summary["turnover"]
            gross_traded_ratio = gross_summary["traded_ratio"]
            net_turnover = net_summary["turnover"]
            net_traded_ratio = net_summary["traded_ratio"]

            for book_name, summary in (("net", net_summary), ("gross", gross_summary)):
                for instr, x_minus, x_plus, signed, tw, aw, reason in summary[
                    "trade_details"
                ]:
                    trades.append(
                        TradeRecord(
                            signal_date=signal_date,
                            execution_date=trade_date,
                            book=book_name,
                            instrument_id=instr,
                            pre_value=x_minus,
                            post_value=x_plus,
                            signed_trade_value=signed,
                            target_weight=tw,
                            actual_weight=aw,
                            reason=reason,
                        )
                    )

            rebalances.append(
                RebalanceRecord(
                    signal_date=signal_date,
                    execution_date=trade_date,
                    target_count=len(target.positions),
                    filled_target_count=net_summary["nonzero_trades"],
                    unavailable_target_count=net_summary["unavailable_count"],
                    frozen_count=net_summary["frozen_count"],
                    buy_notional_ratio=net_summary["buy_ratio"],
                    sell_notional_ratio=net_summary["sell_ratio"],
                    traded_notional_ratio=net_summary["traded_ratio"],
                    turnover=net_summary["turnover"],
                    transaction_cost=net_cost,
                    pre_trade_gross_exposure=net_summary["pre_gross_exposure"],
                    post_trade_gross_exposure=net_summary["post_gross_exposure"],
                    allocation_deviation=net_summary["deviation"],
                    gross_book_buy_notional_ratio=gross_summary["buy_ratio"],
                    gross_book_sell_notional_ratio=gross_summary["sell_ratio"],
                    gross_book_traded_notional_ratio=gross_summary["traded_ratio"],
                    gross_book_turnover=gross_summary["turnover"],
                    gross_book_transaction_cost=gross_cost,
                    gross_book_pre_trade_gross_exposure=gross_summary[
                        "pre_gross_exposure"
                    ],
                    gross_book_post_trade_gross_exposure=gross_summary[
                        "post_gross_exposure"
                    ],
                    gross_book_allocation_deviation=gross_summary["deviation"],
                )
            )

        gross_nav = gross_book.nav()
        net_nav = net_book.nav()
        gross_return = (
            gross_nav / gross_book.prev_nav - 1 if gross_book.prev_nav > 0 else 0.0
        )
        net_return = net_nav / net_book.prev_nav - 1 if net_book.prev_nav > 0 else 0.0

        gross_snapshot, net_snapshot = _snapshots(
            trade_date,
            gross_book,
            net_book,
            gross_return,
            net_return,
            gross_market_pnl,
            net_market_pnl,
            gross_cost,
            net_cost,
            current_prices,
        )
        books.append(gross_snapshot)
        books.append(net_snapshot)

        records.append(
            DailyBacktestRecord(
                trade_date=trade_date,
                nav_gross=gross_nav,
                nav_net=net_nav,
                daily_return_gross=gross_return,
                daily_return_net=net_return,
                gross_exposure=net_snapshot.gross_exposure,
                net_exposure=net_snapshot.net_exposure,
                cash_weight=net_snapshot.cash_weight,
                turnover=net_turnover,
                traded_notional_ratio=net_traded_ratio,
                transaction_cost=net_cost,
                holdings_count=net_snapshot.holdings_count,
                gross_book_gross_exposure=gross_snapshot.gross_exposure,
                gross_book_net_exposure=gross_snapshot.net_exposure,
                gross_book_cash_weight=gross_snapshot.cash_weight,
                gross_book_turnover=gross_turnover,
                gross_book_traded_notional_ratio=gross_traded_ratio,
                gross_book_holdings_count=gross_snapshot.holdings_count,
            )
        )

        gross_book.prev_nav = gross_nav
        net_book.prev_nav = net_nav

    return BacktestResult(
        records=records,
        rebalances=rebalances,
        books=books,
        trades=trades,
        skipped_executions=skipped,
        max_conservation_residual=max_residual,
    )


def _snapshots(
    trade_date: date,
    gross_book: _Book,
    net_book: _Book,
    gross_return: float,
    net_return: float,
    gross_market_pnl: float,
    net_market_pnl: float,
    gross_cost: float,
    net_cost: float,
    current_prices: dict[str, float],
) -> tuple[BookSnapshot, BookSnapshot]:
    def make(
        book: _Book, name: str, daily_return: float, market_pnl: float, fee: float
    ) -> BookSnapshot:
        nav = book.nav()
        positions = tuple(
            PositionRecord(
                instrument_id=instr,
                value=pos.value,
                weight=pos.value / nav if nav > 0 else 0.0,
                last_price=pos.last_price,
                last_mark_date=pos.last_mark_date,
                missing_price=instr not in current_prices,
            )
            for instr, pos in sorted(book.positions.items())
        )
        gross_exposure = (
            sum(abs(p.value) for p in book.positions.values()) / nav if nav > 0 else 0.0
        )
        net_exposure = (
            sum(p.value for p in book.positions.values()) / nav if nav > 0 else 0.0
        )
        cash_weight = book.cash / nav if nav > 0 else 0.0
        return BookSnapshot(
            trade_date=trade_date,
            book=name,
            nav=nav,
            daily_return=daily_return,
            cash=book.cash,
            market_pnl=market_pnl,
            fee=fee,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            cash_weight=cash_weight,
            holdings_count=len(positions),
            positions=positions,
        )

    return (
        make(gross_book, "gross", gross_return, gross_market_pnl, gross_cost),
        make(net_book, "net", net_return, net_market_pnl, net_cost),
    )
