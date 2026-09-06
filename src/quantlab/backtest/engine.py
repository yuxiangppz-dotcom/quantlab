"""Idealized research backtest engine with dual independent ledgers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from quantlab.backtest.lifecycle import LifecycleMonitor
from quantlab.backtest.models import (
    RUN_MODE_DIAGNOSTIC,
    RUN_MODE_STRICT,
    STATUS_ACCOUNTING_ERROR,
    STATUS_BLOCKED_UNSUPPORTED_EVENT,
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITH_SETTLEMENT,
    AccountingResidual,
    BacktestConfig,
    BacktestResult,
    BookSnapshot,
    DailyBacktestRecord,
    FailedAttempt,
    LifecycleEvent,
    PositionRecord,
    RebalanceRecord,
    RiskPolicyAuditRecord,
    SettlementRecord,
    SkippedExecution,
    TradeRecord,
)
from quantlab.backtest.risk_policy import (
    EXIT_POLICY_ID,
    termination_decisions_available_as_of,
)
from quantlab.portfolio.models import TargetPortfolio

MISSING_PRICE_POLICY = "freeze_held_no_price"

_SOLVER_TOL = 1e-15
_ROOT_MAX_ITER = 100
_NEG_TOL = 1e-9
_REL_TOL = 1e-9


def _ulp_zero_scale_tolerance(scale_watermark: float) -> float:
    """Residual bound for identities evaluated at exactly zero scale.

    When every participant of an accounting identity is zero (portfolio fully
    settled at the zero-recovery bound), the residual can only be floating
    point dust left over from the largest magnitudes the session touched.
    Float subtraction error is bounded by a few ULP of its operands, so a few
    ULP of the session's largest magnitude is a mathematically justified,
    fully scale-invariant bound (no absolute money tolerance involved).
    """
    if scale_watermark <= 0:
        return 0.0
    return 4.0 * math.ulp(scale_watermark)


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
        # largest magnitude any earlier session's identities touched; float
        # dust produced at a large scale survives later zero-scale sessions,
        # so zero-scale residual bounds must reference this, not the current
        # session's (possibly dust-sized) participants
        self.scale_watermark = abs(initial_nav)

    def nav(self) -> float:
        return self.cash + math.fsum(p.value for p in self.positions.values())

    def mark_to_market(
        self,
        current_prices: dict[str, float],
        trade_date: date,
        skip: frozenset[str] = frozenset(),
    ) -> float:
        nav_before = self.nav()
        for instr, pos in self.positions.items():
            if instr in skip:
                continue
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


@dataclass
class _ResidualAccumulator:
    _abs: dict[str, tuple[float, date | None, str | None]] = field(default_factory=dict)
    _rel: dict[str, tuple[float, date | None, str | None]] = field(default_factory=dict)

    def add(
        self,
        check: str,
        abs_r: float | None,
        rel_r: float | None,
        trade_date: date,
        book: str,
    ) -> None:
        if abs_r is not None:
            if check not in self._abs or abs_r > self._abs[check][0]:
                self._abs[check] = (abs_r, trade_date, book)
        if rel_r is not None:
            if check not in self._rel or rel_r > self._rel[check][0]:
                self._rel[check] = (rel_r, trade_date, book)

    def results(self) -> list[AccountingResidual]:
        out = []
        for check in sorted(self._abs):
            a = self._abs.get(check, (0.0, None, None))
            r = self._rel.get(check, (0.0, None, None))
            out.append(
                AccountingResidual(
                    check=check,
                    max_abs=a[0],
                    max_abs_date=a[1],
                    max_abs_book=a[2],
                    max_rel=r[0],
                    max_rel_date=r[1],
                    max_rel_book=r[2],
                )
            )
        return out


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


def _solve_normalized(f: float, cost_rate: float, traded_ratio_fn) -> tuple[float, float]:
    """Solve ``u + cost_rate * traded_ratio(u) = 1`` for ``u`` in ``[f, 1]``."""
    if not math.isfinite(f) or not (0.0 <= f <= 1.0):
        raise ValueError(f"invalid normalized frozen value f={f}")
    if not math.isfinite(cost_rate) or not (0.0 <= cost_rate < 1.0):
        raise ValueError(f"invalid cost_rate {cost_rate}")

    if cost_rate == 0.0:
        return 1.0, 0.0

    lo = f
    hi = 1.0
    g_lo = lo + cost_rate * traded_ratio_fn(lo) - 1.0
    g_hi = hi + cost_rate * traded_ratio_fn(hi) - 1.0
    if not (math.isfinite(g_lo) and math.isfinite(g_hi)):
        raise ValueError("self-financing root endpoints are not finite")
    if g_lo > 0 or g_hi < 0:
        raise ValueError(
            f"self-financing root bracket does not contain a root: "
            f"g({lo})={g_lo}, g({hi})={g_hi}"
        )

    converged = False
    for _ in range(_ROOT_MAX_ITER):
        mid = 0.5 * (lo + hi)
        g = mid + cost_rate * traded_ratio_fn(mid) - 1.0
        if g > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo <= _SOLVER_TOL:
            converged = True
            break

    u = 0.5 * (lo + hi)
    residual = u + cost_rate * traded_ratio_fn(u) - 1.0
    if not math.isfinite(residual):
        raise ValueError(f"self-financing root residual is not finite: {residual}")
    if not converged and hi - lo > _SOLVER_TOL * 10:
        raise ValueError(
            f"self-financing root did not converge: bracket [{lo}, {hi}], "
            f"residual {residual}"
        )
    return u, abs(residual)


def _rebalance(
    book: _Book,
    target: TargetPortfolio,
    current_prices: dict[str, float],
    cost_rate: float,
    signal_date: date,
    execution_date: date,
    blocked: frozenset[str] = frozenset(),
    restricted: frozenset[str] = frozenset(),
) -> dict:
    """Apply a target to one book using the freeze-first allocation rule."""
    v_minus = book.nav()
    if not math.isfinite(v_minus) or v_minus <= 0:
        raise ValueError(f"non-positive pre-trade NAV: {v_minus}")

    cash_before = book.cash
    positions_before = {i: p.value for i, p in book.positions.items()}
    weights_before = {i: val / v_minus for i, val in positions_before.items()}

    target_weights = {p.instrument_id: p.target_weight for p in target.positions}

    frozen: set[str] = set()
    unavailable_new: dict[str, float] = {}
    tradable: dict[str, float] = {}
    for instr, w in target_weights.items():
        if w == 0.0:
            continue
        if instr in blocked:
            if instr in book.positions:
                frozen.add(instr)
            else:
                unavailable_new[instr] = w
        elif instr in current_prices:
            tradable[instr] = w
        elif instr in book.positions:
            frozen.add(instr)
        else:
            unavailable_new[instr] = w
    for instr in book.positions:
        if instr not in current_prices or instr in blocked:
            frozen.add(instr)

    frozen_value = math.fsum(positions_before[i] for i in frozen)
    f = max(0.0, min(1.0, frozen_value / v_minus))
    q = target.cash_weight + math.fsum(unavailable_new.values())
    s = math.fsum(tradable.values())

    involved = set(positions_before) | set(tradable) | set(unavailable_new)

    def final_weight(u: float, instr: str) -> float:
        if instr in frozen:
            return weights_before[instr]
        if instr in tradable:
            if s > 0 and u > 0:
                budget = max(0.0, (1.0 - q) * u - f)
                lam = min(1.0, budget / (s * u))
                w = lam * tradable[instr] * u
                if instr in restricted:
                    # cap new exposure at the pre-rebalance (post-mark) amount
                    w = min(w, weights_before.get(instr, 0.0))
                return w
            return 0.0
        return 0.0

    def traded_ratio(u: float) -> float:
        return math.fsum(
            abs(final_weight(u, i) - weights_before.get(i, 0.0))
            for i in sorted(involved)
        )

    u, solver_residual = _solve_normalized(f, cost_rate, traded_ratio)
    v = u * v_minus

    final_values: dict[str, float] = {}
    for instr in sorted(involved):
        if instr in frozen:
            final_values[instr] = positions_before[instr]
        else:
            final_values[instr] = final_weight(u, instr) * v_minus

    buy_cap_binding: list[str] = []
    for instr in sorted(restricted & set(tradable)):
        if s > 0 and u > 0:
            budget = max(0.0, (1.0 - q) * u - f)
            lam = min(1.0, budget / (s * u))
            candidate = lam * tradable[instr] * u * v_minus
        else:
            candidate = 0.0
        cap = positions_before.get(instr, 0.0)
        if candidate > cap + 1e-12:
            buy_cap_binding.append(instr)

    signed = {
        instr: final_values[instr] - positions_before.get(instr, 0.0)
        for instr in sorted(involved)
    }
    actual_traded = math.fsum(abs(x) for x in signed.values())
    fee = cost_rate * actual_traded
    cash_after = cash_before - math.fsum(signed.values()) - fee

    old_positions = book.positions
    new_positions: dict[str, _Position] = {}
    for instr, xp in sorted(final_values.items()):
        if xp == 0.0:
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
    book.cash = cash_after
    nav_after = book.nav()

    buy_value = 0.0
    sell_value = 0.0
    nonzero_trades = 0
    trade_details = []
    for instr in sorted(involved):
        x_minus = positions_before.get(instr, 0.0)
        x_plus = final_values[instr]
        signed_value = signed[instr]
        if signed_value > 0:
            buy_value += signed_value
        elif signed_value < 0:
            sell_value += -signed_value
        if signed_value != 0.0:
            nonzero_trades += 1
        if instr in blocked:
            reason = "lifecycle_blocked"
        elif instr in restricted and instr in tradable and signed_value < 0:
            reason = "sold"  # allowed sell per original target
        elif instr in restricted and instr in tradable:
            reason = "restricted_no_new_exposure"
        elif instr in frozen:
            reason = "frozen_held_no_price"
        elif instr in unavailable_new:
            reason = "new_target_no_price"
        elif signed_value == 0.0:
            reason = "no_change"
        elif signed_value < 0:
            reason = "sold"
        else:
            reason = "traded"
        if signed_value != 0.0:
            execution_price = current_prices[instr]
            price_date = execution_date
            price_kind = "current_session_close"
        else:
            execution_price = None
            price_date = None
            price_kind = "none"
        actual_weight = x_plus / nav_after if nav_after > 0 else 0.0
        trade_details.append(
            (
                instr, x_minus, x_plus, signed_value,
                target_weights.get(instr, 0.0), actual_weight,
                execution_price, price_date, price_kind, reason,
            )
        )

    buy_ratio = buy_value / v_minus if v_minus > 0 else 0.0
    sell_ratio = sell_value / v_minus if v_minus > 0 else 0.0
    traded_ratio = buy_ratio + sell_ratio
    turnover = 0.5 * traded_ratio

    post_gross_exposure = (
        math.fsum(abs(p.value) for p in new_positions.values()) / nav_after
        if nav_after > 0
        else 0.0
    )
    pre_gross_exposure = (
        math.fsum(abs(pv) for pv in positions_before.values()) / v_minus
        if v_minus > 0
        else 0.0
    )

    actual_weights = (
        {i: p.value / nav_after for i, p in new_positions.items()}
        if nav_after > 0
        else {}
    )
    deviation = 0.0
    for instr in sorted(set(target_weights) | set(actual_weights)):
        deviation += abs(actual_weights.get(instr, 0.0) - target_weights.get(instr, 0.0))
    deviation += abs((book.cash / nav_after if nav_after > 0 else 0.0) - target.cash_weight)

    return {
        "cash_before": cash_before,
        "v_minus": v_minus,
        "v": v,
        "fee": fee,
        "actual_traded": actual_traded,
        "solver_residual": solver_residual,
        "pre_values": positions_before,
        "post_values": final_values,
        "signed": signed,
        "frozen_pre": {i: positions_before[i] for i in frozen},
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
        "restricted_count": len(restricted & set(tradable)),
        "restricted_instruments": sorted(restricted & set(tradable)),
        "buy_cap_binding": buy_cap_binding,
        "buy_cap_binding_count": len(buy_cap_binding),
        "trade_details": trade_details,
    }


def _capture_session_start(book: _Book) -> dict:
    """Capture the post-mark, pre-trade ledger used by session reconciliation."""
    return {
        "cash_before": book.cash,
        "v_minus": book.nav(),
        "pre_values": {i: p.value for i, p in book.positions.items()},
    }


def _force_exit(
    book: _Book,
    instrument_id: str,
    cost_rate: float,
) -> tuple[float, float]:
    """Sell one currently marked position completely at current-session close."""
    position = book.positions.pop(instrument_id)
    sell_value = position.value
    fee = cost_rate * sell_value
    book.cash += sell_value - fee
    return sell_value, fee


def _session_accounting_summary(
    book: _Book,
    start: dict,
    segment_summaries: list[dict],
    frozen_instruments: set[str],
    settlements: dict[str, dict] | None = None,
) -> dict | None:
    """Combine forced-exit, settlement and rebalance legs for validation."""
    settlements = settlements or {}
    if not segment_summaries and not frozen_instruments and not settlements:
        return None
    signed: dict[str, float] = {}
    for summary in segment_summaries:
        for instrument_id, value in summary["signed"].items():
            signed[instrument_id] = signed.get(instrument_id, 0.0) + value
    return {
        "cash_before": start["cash_before"],
        "v_minus": start["v_minus"],
        "pre_values": start["pre_values"],
        "signed": signed,
        "settlements": settlements,
        "frozen_pre": {
            instrument_id: start["pre_values"][instrument_id]
            for instrument_id in sorted(frozen_instruments)
            if instrument_id in start["pre_values"]
        },
    }


def _forced_exit_summary(
    start: dict,
    instrument_id: str,
    sell_value: float,
) -> dict:
    """Return the trade leg shape consumed by session reconciliation."""
    return {
        "signed": {instrument_id: -sell_value},
        "pre_values": start["pre_values"],
        "frozen_pre": {},
    }


def _session_trade_ratios(summary: dict | None) -> tuple[float, float, float, float]:
    """Return buy, sell, traded and turnover ratios from all session legs.

    Settlement disposals are not market trades and are excluded from turnover.
    """
    if summary is None:
        return 0.0, 0.0, 0.0, 0.0
    scale = summary["v_minus"]
    settlements = summary.get("settlements", {})
    recovered = math.fsum(v["recovered"] for v in settlements.values())
    buy = math.fsum(max(value, 0.0) for value in summary["signed"].values()) / scale
    sell = (
        math.fsum(max(-value, 0.0) for value in summary["signed"].values()) - recovered
    ) / scale
    traded = buy + sell
    return buy, sell, traded, 0.5 * traded


def run_backtest(
    price_frame: pd.DataFrame,
    open_dates: list[date],
    targets: dict[date, TargetPortfolio],
    config: BacktestConfig,
    execution_lag_sessions: int = 1,
    mode: str = RUN_MODE_STRICT,
    lifecycle: LifecycleMonitor | None = None,
    requested_period_start: date | None = None,
    requested_period_end: date | None = None,
    restricted_by_signal: dict[date, frozenset[str]] | None = None,
    risk_facts: dict | None = None,
    risk_policy: str | None = None,
) -> BacktestResult:
    """Simulate dual gross/net ledgers with self-financing cost.

    ``mode`` is ``"strict"`` (stop before the first unsupported lifecycle event)
    or ``"diagnostic"`` (continue past events with ``diagnostic_only`` marking).

    When ``config.delisting_settlement`` is set, held positions whose
    lifecycle event_type is ``delist`` and that have no valid exit price are
    settled as cash at ``last_mark_value * recovery_rate`` (net book minus
    ``settlement_fee_bps``; gross book never pays the fee) and the run
    completes as ``completed_with_settlement_assumptions`` instead of
    blocking. This is an explicit accounting assumption, not a verified
    delisting fact. ``code_change`` and ``conflict`` events are NOT covered:
    held positions keep the ``blocked_by_unsupported_event`` path. Available
    trusted-fact risk instructions keep priority: settlement is only the
    residual fallback for positions the risk policy could not exit.
    """
    if mode not in (RUN_MODE_STRICT, RUN_MODE_DIAGNOSTIC):
        raise ValueError(f"invalid mode {mode!r}")
    if (
        isinstance(execution_lag_sessions, bool)
        or not isinstance(execution_lag_sessions, int)
        or execution_lag_sessions <= 0
    ):
        raise ValueError(
            f"execution_lag_sessions must be a positive integer, "
            f"got {execution_lag_sessions!r}"
        )
    if risk_policy not in (None, EXIT_POLICY_ID):
        raise ValueError(f"invalid risk_policy {risk_policy!r}")
    if risk_policy is not None and risk_facts is None:
        raise ValueError("risk_facts are required when risk_policy is enabled")
    if risk_policy is None and risk_facts is not None:
        raise ValueError("risk_policy is required when risk_facts are supplied")

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
    lifecycle_events: list[LifecycleEvent] = []
    failed_attempts: list[FailedAttempt] = []
    risk_policy_audit: list[RiskPolicyAuditRecord] = []

    acc = _ResidualAccumulator()
    solver_residual = 0.0
    seen_event_keys: set[tuple[str, str]] = set()
    exited_by_book = {"gross": set(), "net": set()}
    settlement_cfg = config.delisting_settlement
    settlement_events: list[SettlementRecord] = []
    settled_by_book = {"gross": set(), "net": set()}

    status = STATUS_COMPLETED
    first_blocking_event: LifecycleEvent | None = None
    valid_through: date | None = None
    diagnostic_from: date | None = None
    accounting_error: str | None = None
    accounting_error_date: date | None = None
    accounting_error_book: str | None = None

    for trade_date in dates:
        current_prices: dict[str, float] = {}
        if trade_date in price.index:
            current_prices = price.loc[trade_date].dropna().to_dict()

        target = execution_map.get(trade_date)
        risk_decisions = (
            termination_decisions_available_as_of(risk_facts, trade_date)
            if risk_policy is not None
            else {}
        )

        # ---- lifecycle check before valuation / trading ----
        session_blocked: set[str] = set()
        new_events: list[LifecycleEvent] = []
        if lifecycle is not None:
            for book_name, book in (("gross", gross_book), ("net", net_book)):
                for instr, pos in book.positions.items():
                    spec = lifecycle.event_for(instr, trade_date)
                    if spec is None:
                        continue
                    session_blocked.add(instr)
                    event_id = f"{instr}:{spec.event_type}:{spec.event_date.isoformat()}"
                    key = (event_id, book_name)
                    if key in seen_event_keys:
                        continue
                    seen_event_keys.add(key)
                    new_events.append(
                        LifecycleEvent(
                            event_id=event_id,
                            instrument_id=instr,
                            event_type=spec.event_type,
                            event_date=spec.event_date,
                            blocking_session=trade_date,
                            book=book_name,
                            position_value=pos.value,
                            last_mark_date=pos.last_mark_date,
                            description=spec.description,
                        )
                    )
            if target is not None:
                _, target_portfolio = target
                held = set(gross_book.positions) | set(net_book.positions)
                for pos in target_portfolio.positions:
                    if pos.target_weight <= 0 or pos.instrument_id in held:
                        continue
                    if pos.instrument_id in risk_decisions:
                        # The zero-exposure risk overlay rejects this target.
                        continue
                    spec = lifecycle.event_for(pos.instrument_id, trade_date)
                    if spec is None:
                        continue
                    session_blocked.add(pos.instrument_id)
                    event_id = (
                        f"{pos.instrument_id}:{spec.event_type}:"
                        f"{spec.event_date.isoformat()}"
                    )
                    key = (event_id, "target")
                    if key in seen_event_keys:
                        continue
                    seen_event_keys.add(key)
                    new_events.append(
                        LifecycleEvent(
                            event_id=event_id,
                            instrument_id=pos.instrument_id,
                            event_type=spec.event_type,
                            event_date=spec.event_date,
                            blocking_session=trade_date,
                            book="target",
                            position_value=0.0,
                            last_mark_date=None,
                            description=(
                                f"target attempts to open invalid instrument: "
                                f"{spec.description}"
                            ),
                        )
                    )

        blocked_instruments = frozenset(session_blocked)
        target_weights_today = (
            {p.instrument_id: p.target_weight for p in target[1].positions}
            if target is not None
            else {}
        )

        # A lifecycle-invalid held position blocks before valuation or trading.
        # Record whether an already-available risk instruction failed to exit it.
        for book_name, book in (("gross", gross_book), ("net", net_book)):
            for instr in sorted(blocked_instruments & set(book.positions)):
                fact = risk_decisions.get(instr)
                if fact is None:
                    continue
                pos = book.positions[instr]
                available_from = date.fromisoformat(fact["available_from"])
                risk_policy_audit.append(
                    RiskPolicyAuditRecord(
                        instrument_id=instr,
                        book=book_name,
                        fact_id=fact.get("fact_id") or "unknown_fact_id",
                        policy_version=risk_policy,
                        available_from=available_from,
                        decision_date=trade_date,
                        risk_state="blocked_before_exit",
                        held=True,
                        pre_position_value=pos.value,
                        current_price_available=instr in current_prices,
                        execution_price=None,
                        forced_sell_value=0.0,
                        fee=0.0,
                        resulting_position_value=pos.value,
                        target_weight=target_weights_today.get(instr, 0.0),
                        prevented_new_entry=False,
                        prevented_refill=target_weights_today.get(instr, 0.0) > 0,
                        reason="instrument lifecycle-invalid before risk exit completed",
                    )
                )

        if new_events:
            new_events.sort(
                key=lambda e: (e.blocking_session, e.book, e.instrument_id)
            )
            lifecycle_events.extend(new_events)
            held_events = [e for e in new_events if e.book in ("gross", "net")]
            unsupported_held = [e for e in held_events if e.event_type != "delist"]
            if settlement_cfg is None:
                status = STATUS_BLOCKED_UNSUPPORTED_EVENT
                if first_blocking_event is None:
                    first_blocking_event = new_events[0]
                if mode == RUN_MODE_STRICT:
                    break
                if diagnostic_from is None:
                    diagnostic_from = trade_date
            elif unsupported_held:
                # The delisting settlement assumption only covers held positions
                # whose event_type is "delist". Code changes and static
                # conflicts are not delistings; cashing them out with a
                # recovery assumption would fabricate an economic fact, so
                # they keep the strict blocked path.
                status = STATUS_BLOCKED_UNSUPPORTED_EVENT
                if first_blocking_event is None:
                    first_blocking_event = unsupported_held[0]
                if mode == RUN_MODE_STRICT:
                    break
                if diagnostic_from is None:
                    diagnostic_from = trade_date
            else:
                # Delist-only session: held positions are resolved by the
                # explicit settlement assumption below; target-only events
                # (prevented new entries) stay audit-only and non-blocking.
                if held_events and first_blocking_event is None:
                    first_blocking_event = held_events[0]

        # ---- valuation ----
        gross_market_pnl = gross_book.mark_to_market(
            current_prices, trade_date, skip=blocked_instruments
        )
        net_market_pnl = net_book.mark_to_market(
            current_prices, trade_date, skip=blocked_instruments
        )

        gross_start = _capture_session_start(gross_book)
        net_start = _capture_session_start(net_book)
        gross_cost = 0.0
        net_cost = 0.0
        gross_segments: list[dict] = []
        net_segments: list[dict] = []
        gross_settlements: dict[str, dict] = {}
        net_settlements: dict[str, dict] = {}
        gross_frozen: set[str] = set()
        net_frozen: set[str] = set()
        gross_rebalance_summary: dict | None = None
        net_rebalance_summary: dict | None = None

        day_trades: list[TradeRecord] = []
        day_risk_audit: list[RiskPolicyAuditRecord] = []
        day_rebalance: RebalanceRecord | None = None

        # ---- persistent PIT risk overlay and forced exits ----
        for book_name, book, cost_rate, start, segments, frozen in (
            ("gross", gross_book, 0.0, gross_start, gross_segments, gross_frozen),
            ("net", net_book, config.cost_rate, net_start, net_segments, net_frozen),
        ):
            for instr, fact in sorted(risk_decisions.items()):
                if instr in blocked_instruments and instr in book.positions:
                    continue
                held = instr in book.positions
                pre_value = book.positions[instr].value if held else 0.0
                target_weight = target_weights_today.get(instr, 0.0)
                prevented_new_entry = target_weight > 0 and not held
                prevented_refill = target_weight > 0 and held
                execution_price = None
                sell_value = 0.0
                fee = 0.0

                if held and instr in current_prices:
                    execution_price = current_prices[instr]
                    sell_value, fee = _force_exit(book, instr, cost_rate)
                    segments.append(_forced_exit_summary(start, instr, sell_value))
                    exited_by_book[book_name].add(instr)
                    state = "exited"
                    reason = "forced exit at current-session close"
                    day_trades.append(
                        TradeRecord(
                            signal_date=trade_date,
                            execution_date=trade_date,
                            book=book_name,
                            instrument_id=instr,
                            pre_value=pre_value,
                            post_value=0.0,
                            signed_trade_value=-sell_value,
                            target_weight=0.0,
                            actual_weight=0.0,
                            execution_price=execution_price,
                            price_date=trade_date,
                            price_kind="current_session_close",
                            reason="risk_forced_exit",
                        )
                    )
                elif held:
                    frozen.add(instr)
                    state = "pending_no_price"
                    reason = "exit pending; no current-session execution price"
                elif instr in exited_by_book[book_name]:
                    state = "exited"
                    reason = "position already exited; re-entry remains forbidden"
                elif instr in settled_by_book[book_name]:
                    state = "exited"
                    reason = (
                        "position settled under delisting settlement assumption; "
                        "re-entry remains forbidden"
                    )
                else:
                    state = "exit_required"
                    reason = "trusted termination decision available; no position held"

                if prevented_new_entry:
                    reason += "; prevented new entry"
                if prevented_refill:
                    reason += "; prevented refill"
                day_risk_audit.append(
                    RiskPolicyAuditRecord(
                        instrument_id=instr,
                        book=book_name,
                        fact_id=fact.get("fact_id") or "unknown_fact_id",
                        policy_version=risk_policy,
                        available_from=date.fromisoformat(fact["available_from"]),
                        decision_date=trade_date,
                        risk_state=state,
                        held=held,
                        pre_position_value=pre_value,
                        current_price_available=instr in current_prices,
                        execution_price=execution_price,
                        forced_sell_value=sell_value,
                        fee=fee,
                        resulting_position_value=(
                            book.positions[instr].value if instr in book.positions else 0.0
                        ),
                        target_weight=target_weight,
                        prevented_new_entry=prevented_new_entry,
                        prevented_refill=prevented_refill,
                        reason=reason,
                    )
                )
                if book_name == "gross":
                    gross_cost += fee
                else:
                    net_cost += fee

        # ---- residual lifecycle settlement (explicit assumption) ----
        # Trusted-fact forced exits above retain priority; settlement only
        # resolves lifecycle-invalid positions they could not exit.
        if settlement_cfg is not None:
            session_event_index = {
                (e.instrument_id, e.book): e
                for e in new_events
                if e.book in ("gross", "net")
            }
            for book_name, book, start, segments, settlements_for_book in (
                ("gross", gross_book, gross_start, gross_segments, gross_settlements),
                ("net", net_book, net_start, net_segments, net_settlements),
            ):
                fee_rate = (
                    0.0
                    if book_name == "gross"
                    else settlement_cfg.settlement_fee_rate
                )
                for instr in sorted(blocked_instruments & set(book.positions)):
                    event = session_event_index.get((instr, book_name))
                    if event is None or event.event_type != "delist":
                        # Only a confirmed delisting may be settled. A held
                        # code_change/conflict keeps blocking above and its
                        # position stays frozen at the stale mark.
                        continue
                    pos = book.positions.pop(instr)
                    recovered = pos.value * settlement_cfg.recovery_rate
                    settle_fee = recovered * fee_rate
                    cash_credit = recovered - settle_fee
                    shortfall = pos.value - recovered
                    book.cash += cash_credit
                    settled_by_book[book_name].add(instr)
                    settlements_for_book[instr] = {
                        "pre": pos.value,
                        "recovered": recovered,
                        "fee": settle_fee,
                        "shortfall": shortfall,
                    }
                    # disposal leg: proceeds before the settlement fee, so the
                    # fee reconciles as an explicit cost term, not market cost
                    segments.append(
                        {
                            "signed": {instr: -recovered},
                            "pre_values": start["pre_values"],
                            "frozen_pre": {},
                        }
                    )
                    status = STATUS_COMPLETED_WITH_SETTLEMENT
                    settlement_events.append(
                        SettlementRecord(
                            instrument_id=instr,
                            book=book_name,
                            event_type=event.event_type if event else "unknown",
                            event_date=event.event_date if event else trade_date,
                            blocking_session=trade_date,
                            last_mark_value=pos.value,
                            last_mark_date=pos.last_mark_date,
                            recovery_rate=settlement_cfg.recovery_rate,
                            settlement_fee=settle_fee,
                            settled_value=cash_credit,
                            recovery_shortfall=shortfall,
                            description=(
                                event.description if event
                                else "residual lifecycle settlement"
                            ),
                        )
                    )

        # settled instruments can never be re-entered by a later target
        rebalance_blocked = frozenset(
            set(blocked_instruments)
            | settled_by_book["gross"]
            | settled_by_book["net"]
        )

        # session total cost = market transaction fee + settlement fee
        # (option A: every daily fee field reports the full session cost;
        # the gross book never pays the settlement fee, so its total is 0).
        # Computed on the common path so non-rebalance settlement sessions
        # also report the fee in daily fields.
        gross_settlement_fee_total = math.fsum(
            v["fee"] for v in gross_settlements.values()
        )
        net_settlement_fee_total = math.fsum(
            v["fee"] for v in net_settlements.values()
        )

        if target is not None:
            signal_date, target_portfolio = target
            signal_restricted = frozenset(
                set((restricted_by_signal or {}).get(signal_date, frozenset()))
                | set(risk_decisions)
            )
            gross_rebalance_summary = _rebalance(
                gross_book, target_portfolio, current_prices, 0.0,
                signal_date, trade_date, rebalance_blocked, signal_restricted,
            )
            net_rebalance_summary = _rebalance(
                net_book, target_portfolio, current_prices, config.cost_rate,
                signal_date, trade_date, rebalance_blocked, signal_restricted,
            )
            gross_segments.append(gross_rebalance_summary)
            net_segments.append(net_rebalance_summary)
            gross_frozen.update(gross_rebalance_summary["frozen_pre"])
            net_frozen.update(net_rebalance_summary["frozen_pre"])
            solver_residual = max(
                solver_residual, net_rebalance_summary["solver_residual"]
            )
            gross_cost += gross_rebalance_summary["fee"]
            net_cost += net_rebalance_summary["fee"]

            for book_name, summary in (
                ("net", net_rebalance_summary),
                ("gross", gross_rebalance_summary),
            ):
                for detail in summary["trade_details"]:
                    (
                        instr, x_minus, x_plus, signed_value, tw, aw,
                        exec_price, price_date, price_kind, reason,
                    ) = detail
                    day_trades.append(
                        TradeRecord(
                            signal_date=signal_date,
                            execution_date=trade_date,
                            book=book_name,
                            instrument_id=instr,
                            pre_value=x_minus,
                            post_value=x_plus,
                            signed_trade_value=signed_value,
                            target_weight=tw,
                            actual_weight=aw,
                            execution_price=exec_price,
                            price_date=price_date,
                            price_kind=price_kind,
                            reason=reason,
                        )
                    )

        gross_summary = _session_accounting_summary(
            gross_book, gross_start, gross_segments, gross_frozen, gross_settlements
        )
        net_summary = _session_accounting_summary(
            net_book, net_start, net_segments, net_frozen, net_settlements
        )
        gross_buy_ratio, gross_sell_ratio, gross_traded_ratio, gross_turnover = (
            _session_trade_ratios(gross_summary)
        )
        net_buy_ratio, net_sell_ratio, net_traded_ratio, net_turnover = (
            _session_trade_ratios(net_summary)
        )

        if target is not None:
            signal_date, target_portfolio = target
            assert gross_rebalance_summary is not None
            assert net_rebalance_summary is not None
            gross_pre_exposure = (
                math.fsum(abs(v) for v in gross_start["pre_values"].values())
                / gross_start["v_minus"]
            )
            net_pre_exposure = (
                math.fsum(abs(v) for v in net_start["pre_values"].values())
                / net_start["v_minus"]
            )
            day_rebalance = RebalanceRecord(
                signal_date=signal_date,
                execution_date=trade_date,
                target_count=len(target_portfolio.positions),
                nonzero_trade_count=sum(
                    value != 0.0 for value in net_summary["signed"].values()
                ),
                unavailable_target_count=net_rebalance_summary["unavailable_count"],
                frozen_count=len(net_frozen),
                restricted_binding_count=net_rebalance_summary["buy_cap_binding_count"],
                gross_book_restricted_binding_count=gross_rebalance_summary[
                    "buy_cap_binding_count"
                ],
                buy_notional_ratio=net_buy_ratio,
                sell_notional_ratio=net_sell_ratio,
                traded_notional_ratio=net_traded_ratio,
                turnover=net_turnover,
                transaction_cost=net_cost + net_settlement_fee_total,
                pre_trade_gross_exposure=net_pre_exposure,
                post_trade_gross_exposure=(
                    math.fsum(abs(p.value) for p in net_book.positions.values())
                    / net_book.nav()
                ),
                allocation_deviation=net_rebalance_summary["deviation"],
                gross_book_buy_notional_ratio=gross_buy_ratio,
                gross_book_sell_notional_ratio=gross_sell_ratio,
                gross_book_traded_notional_ratio=gross_traded_ratio,
                gross_book_turnover=gross_turnover,
                gross_book_transaction_cost=gross_cost + gross_settlement_fee_total,
                gross_book_pre_trade_gross_exposure=gross_pre_exposure,
                gross_book_post_trade_gross_exposure=(
                    math.fsum(abs(p.value) for p in gross_book.positions.values())
                    / gross_book.nav()
                ),
                gross_book_allocation_deviation=gross_rebalance_summary["deviation"],
            )

        gross_nav = gross_book.nav()
        net_nav = net_book.nav()
        gross_return = (
            gross_nav / gross_book.prev_nav - 1 if gross_book.prev_nav > 0 else 0.0
        )
        net_return = net_nav / net_book.prev_nav - 1 if net_book.prev_nav > 0 else 0.0

        # ---- accounting checks (before this session is treated as valid) ----
        gross_violations = _accumulate_checks(
            acc, "gross", trade_date, gross_book, gross_book.prev_nav,
            gross_market_pnl, gross_cost, 0.0, gross_summary,
            allow_zero_nav=settlement_cfg is not None,
        )
        net_violations = _accumulate_checks(
            acc, "net", trade_date, net_book, net_book.prev_nav,
            net_market_pnl, net_cost, config.cost_rate, net_summary,
            allow_zero_nav=settlement_cfg is not None,
        )
        all_violations = gross_violations + net_violations
        if all_violations:
            accounting_error = "; ".join(all_violations)
            accounting_error_date = trade_date
            accounting_error_book = "gross" if gross_violations else "net"
            status = STATUS_ACCOUNTING_ERROR
            failed_attempts.append(
                FailedAttempt(
                    trade_date=trade_date,
                    reason=accounting_error,
                    trades=tuple(day_trades),
                    rebalance=day_rebalance,
                )
            )
            break

        # commit this day's trades/rebalance only after both books validate
        trades.extend(day_trades)
        risk_policy_audit.extend(day_risk_audit)
        if day_rebalance is not None:
            rebalances.append(day_rebalance)

        # ---- snapshots ----
        gross_snapshot, net_snapshot = _snapshots(
            trade_date,
            gross_book,
            net_book,
            gross_return,
            net_return,
            gross_market_pnl,
            net_market_pnl,
            gross_cost + gross_settlement_fee_total,
            net_cost + net_settlement_fee_total,
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
                transaction_cost=net_cost + net_settlement_fee_total,
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

        # valid_through advances only for clean, non-diagnostic sessions
        if diagnostic_from is None:
            valid_through = trade_date

    accounting_checks = acc.results()
    simulated_start = records[0].trade_date if records else None
    simulated_end = records[-1].trade_date if records else None

    return BacktestResult(
        run_mode=mode,
        status=status,
        requested_period_start=requested_period_start,
        requested_period_end=requested_period_end,
        simulated_period_start=simulated_start,
        simulated_period_end=simulated_end,
        valid_through=valid_through,
        diagnostic_from=diagnostic_from,
        first_blocking_event=first_blocking_event,
        records=records,
        rebalances=rebalances,
        books=books,
        trades=trades,
        skipped_executions=skipped,
        lifecycle_events=lifecycle_events,
        failed_attempts=failed_attempts,
        solver_root_residual=solver_residual,
        accounting_checks=accounting_checks,
        accounting_error=accounting_error,
        accounting_error_date=accounting_error_date,
        accounting_error_book=accounting_error_book,
        risk_policy_audit=risk_policy_audit,
        settlement_events=settlement_events,
    )


def _record_residual(
    acc: _ResidualAccumulator,
    violations: list[str],
    check: str,
    abs_r: float,
    scale: float,
    trade_date: date,
    book_name: str,
    zero_tolerance: float,
) -> None:
    if not math.isfinite(abs_r):
        violations.append(
            f"{check} residual not finite on {trade_date} {book_name}: {abs_r}"
        )
        return
    if not math.isfinite(scale) or scale < 0:
        violations.append(
            f"{check} invalid scale on {trade_date} {book_name}: {scale}"
        )
        return
    if scale == 0:
        # zero-NAV corner (portfolio settled to zero at the recovery=0 bound):
        # the residual can only be float dust of the session's largest
        # magnitudes, so the bound is a few ULP of that watermark — fully
        # scale-invariant, no absolute money tolerance
        if abs_r > zero_tolerance:
            violations.append(
                f"{check} invalid scale on {trade_date} {book_name}: {scale} "
                f"(residual {abs_r:.6e} at zero scale, ULP bound "
                f"{zero_tolerance:.6e})"
            )
            return
        acc.add(check, abs_r, 0.0, trade_date, book_name)
        return
    rel = abs_r / scale
    acc.add(check, abs_r, rel, trade_date, book_name)
    if rel > _REL_TOL:
        violations.append(
            f"{check} residual {abs_r:.6e} exceeds tolerance "
            f"(rel {rel:.6e}, scale {scale:.6e}) on {trade_date} {book_name}"
        )


def _check_participants_finite(
    violations: list[str],
    book_name: str,
    trade_date: date,
    prev_nav: float,
    market_pnl: float,
    fee: float,
) -> None:
    for name, value in (("prev_nav", prev_nav), ("market_pnl", market_pnl), ("fee", fee)):
        if not math.isfinite(value):
            violations.append(
                f"{book_name} {name} not finite on {trade_date}: {value}"
            )


def _check_summary_finite(
    violations: list[str], book_name: str, trade_date: date, summary: dict
) -> None:
    for name in ("cash_before", "v_minus"):
        value = summary.get(name)
        if not math.isfinite(value):
            violations.append(
                f"{book_name} summary {name} not finite on {trade_date}: {value}"
            )
    for instr, value in summary.get("signed", {}).items():
        if not math.isfinite(value):
            violations.append(
                f"{book_name} summary signed {instr} not finite on {trade_date}: {value}"
            )
    for instr, value in summary.get("pre_values", {}).items():
        if not math.isfinite(value):
            violations.append(
                f"{book_name} summary pre_value {instr} not finite on {trade_date}: {value}"
            )
    for instr, values in summary.get("settlements", {}).items():
        for name, value in values.items():
            if not math.isfinite(value):
                violations.append(
                    f"{book_name} summary settlement {instr}.{name} not finite "
                    f"on {trade_date}: {value}"
                )


def _accumulate_checks(
    acc: _ResidualAccumulator,
    book_name: str,
    trade_date: date,
    book: _Book,
    prev_nav: float,
    market_pnl: float,
    fee: float,
    cost_rate: float,
    summary: dict | None,
    allow_zero_nav: bool = False,
) -> list[str]:
    violations: list[str] = []
    nav = book.nav()

    _check_participants_finite(
        violations, book_name, trade_date, prev_nav, market_pnl, fee
    )

    # Scale watermark for zero-scale residuals: the largest magnitude any
    # participant of this session's identities touched. Float error in the
    # residual expressions is bounded by a few ULP of it.
    session_watermark = max(
        abs(prev_nav), abs(market_pnl), abs(fee), abs(nav), abs(book.cash)
    )
    if summary is not None:
        session_watermark = max(
            session_watermark,
            abs(summary["cash_before"]),
            abs(summary["v_minus"]),
            *(abs(v) for v in summary["signed"].values()),
            *(abs(v) for v in summary["pre_values"].values()),
            *(
                abs(term)
                for settlement in summary.get("settlements", {}).values()
                for term in settlement.values()
            ),
        )
    # accumulate the historical scale watermark before deriving the bound
    book.scale_watermark = max(book.scale_watermark, session_watermark)
    zero_tolerance = _ulp_zero_scale_tolerance(book.scale_watermark)

    # settlement runs may legitimately drive NAV to (float-dust around) zero
    # at the zero-recovery bound; the floor is relative to the historical
    # scale of the book, never to an absolute money amount
    nav_floor = (
        -_NEG_TOL * max(book.scale_watermark, prev_nav, abs(market_pnl), fee, nav)
        if allow_zero_nav
        else 0.0
    )
    if not math.isfinite(nav) or nav < nav_floor:
        violations.append(f"{book_name} nav not finite/positive on {trade_date}: {nav}")
    if not math.isfinite(book.cash):
        violations.append(f"{book_name} cash not finite on {trade_date}: {book.cash}")
    elif nav > 0 and book.cash < -_NEG_TOL * nav:
        violations.append(
            f"{book_name} negative cash beyond tolerance on {trade_date}: {book.cash}"
        )
    for instr, pos in book.positions.items():
        if not math.isfinite(pos.value):
            violations.append(
                f"{book_name} position {instr} value not finite on {trade_date}"
            )
        elif nav > 0 and pos.value < -_NEG_TOL * nav:
            violations.append(
                f"{book_name} negative position {instr} on {trade_date}: {pos.value}"
            )

    positions_sum = math.fsum(p.value for p in book.positions.values())
    r = nav - (book.cash + positions_sum)
    scale = nav if nav > 0 else (prev_nav if prev_nav > 0 else 0.0)
    _record_residual(
        acc, violations, "asset_identity", abs(r), scale, trade_date, book_name,
        zero_tolerance,
    )

    settlements = (
        summary.get("settlements", {}) if summary is not None else {}
    )
    settlement_fee_total = math.fsum(v["fee"] for v in settlements.values())
    shortfall_total = math.fsum(v["shortfall"] for v in settlements.values())

    r = nav - (
        prev_nav + market_pnl - fee - settlement_fee_total - shortfall_total
    )
    scale = prev_nav if prev_nav > 0 else (nav if nav > 0 else 0.0)
    _record_residual(
        acc, violations, "daily_nav_bridge", abs(r), scale, trade_date, book_name,
        zero_tolerance,
    )

    if summary is None:
        return violations

    _check_summary_finite(violations, book_name, trade_date, summary)

    cash_before = summary["cash_before"]
    v_minus = summary["v_minus"]
    signed = summary["signed"]
    pre_values = summary["pre_values"]
    frozen_pre = summary["frozen_pre"]

    scale_v = v_minus if v_minus > 0 else (nav if nav > 0 else 0.0)

    actual_traded = math.fsum(abs(s) for s in signed.values())
    recovered_total = math.fsum(v["recovered"] for v in settlements.values())
    # market-cost consistency only; the settlement fee is a separate term
    r = fee - cost_rate * (actual_traded - recovered_total)
    _record_residual(
        acc, violations, "fee_consistency", abs(r), scale_v, trade_date, book_name,
        zero_tolerance,
    )

    r = book.cash - (
        cash_before
        - math.fsum(signed.values())
        - fee
        - settlement_fee_total
    )
    _record_residual(
        acc, violations, "cash_flow", abs(r), scale_v, trade_date, book_name,
        zero_tolerance,
    )

    r = nav - (v_minus - fee - settlement_fee_total - shortfall_total)
    _record_residual(
        acc, violations, "rebalance_nav", abs(r), scale_v, trade_date, book_name,
        zero_tolerance,
    )

    # position reconciliation against the ACTUAL book, not the summary.
    all_instrs = set(pre_values) | set(signed) | set(book.positions)
    for instr in sorted(all_instrs):
        pre = pre_values.get(instr, 0.0)
        post_actual = book.positions[instr].value if instr in book.positions else 0.0
        s = signed.get(instr, 0.0)
        shortfall = settlements.get(instr, {}).get("shortfall", 0.0)
        r = (post_actual - pre) - s + shortfall
        pscale = abs(pre) if pre != 0 else scale_v
        _record_residual(
            acc, violations, "position_reconciliation",
            abs(r), pscale, trade_date, book_name, zero_tolerance,
        )

    # frozen invariance against the ACTUAL book.
    for instr, pre in frozen_pre.items():
        post_actual = book.positions[instr].value if instr in book.positions else 0.0
        r = post_actual - pre
        pscale = abs(pre) if pre != 0 else scale_v
        _record_residual(
            acc, violations, "frozen_invariance",
            abs(r), pscale, trade_date, book_name, zero_tolerance,
        )

    return violations


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
            math.fsum(abs(p.value) for p in book.positions.values()) / nav
            if nav > 0
            else 0.0
        )
        net_exposure = (
            math.fsum(p.value for p in book.positions.values()) / nav if nav > 0 else 0.0
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
