"""Connect daily ML scores to existing stock quantity/cash scenario accounting.

Decisions and desired shares use previous-session marks only. Execution-day
bars constrain fills inside the existing kernel, never the predictive ranking.
No real-data certification or broker authority is inferred from this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.portfolio import buffered_target
from quantlab.research.quantity_kernel import ResearchBook, ResearchOrder
from quantlab.research.quantity_scheduler import (
    ResearchDay,
    ResearchScheduleResult,
    advance_research_day,
)


@dataclass(frozen=True)
class MLReplayResult:
    schedule: ResearchScheduleResult
    decisions: tuple[dict, ...]


def replay_scores(
    scores,
    universe,
    calendar: tuple[date, ...],
    market_days,
    initial_marks,
    *,
    start: date,
    end: date,
    initial_cash_fen: int,
    config: MLConfig,
):
    """market_days includes *every* session, including those without rebalancing.

    Inputs are typed ResearchDay batches with no orders, carrying explicit raw
    marks and scenario contexts. A known failed sale cannot fund purchases.
    Unfillable target shortfalls are recorded, not retried daily behind the
    turnover policy. Risk exits are attempted each day outside the normal budget.
    """
    if tuple(sorted(set(calendar))) != calendar or len(calendar) < 3:
        raise ValueError("unique ordered complete calendar required")
    first, last = calendar.index(start), calendar.index(end)
    if first < 1 or last < first or last >= len(calendar) - 1:
        raise ValueError("replay needs previous-decision and next-settlement calendar padding")
    if scores.duplicated(["trade_date", "instrument_id"]).any():
        raise ValueError("replay requires exactly one model and one score per date/instrument")
    if universe.duplicated(["trade_date", "instrument_id"]).any():
        raise ValueError("duplicate PIT universe row")
    if len({d.session for d in market_days}) != len(market_days):
        raise ValueError("duplicate market day")
    if any(day.orders for day in market_days):
        raise ValueError("market evidence batches must not inject orders")
    batches = {d.session: d for d in market_days}
    book = ResearchBook(calendar[first - 1], initial_cash_fen)
    if len({m.instrument_id for m in initial_marks}) != len(initial_marks):
        raise ValueError("duplicate initial mark")
    if any(m.session != book.asof_date for m in initial_marks):
        raise ValueError("initial marks must be from the decision session")
    marks = {m.instrument_id: m.price_fen for m in initial_marks}
    records, decisions, attempted = [], [], set()
    scores, universe = scores.copy(), universe.copy()
    scores["trade_date"] = pd.to_datetime(scores.trade_date)
    universe["trade_date"] = pd.to_datetime(universe.trade_date)

    def finish(reason=None, day=None):
        schedule = ResearchScheduleResult(
            "stopped" if reason else "completed_scenario",
            initial_cash_fen,
            end,
            book,
            tuple(records),
            day,
            reason,
        )
        return MLReplayResult(schedule, tuple(decisions))

    for i in range(first, last + 1):
        day, decision_day = calendar[i], calendar[i - 1]
        evidence = batches.get(day)
        if evidence is None:
            return finish("session_batch_missing", day)
        quantities = {}
        ages = {}
        for lot in book.lots:
            if lot.instrument_id not in marks:
                return finish(f"decision_mark_unknown:{lot.instrument_id}", day)
            quantities[lot.instrument_id] = quantities.get(lot.instrument_id, 0) + lot.quantity
            # Last acquisition conservatively resets minimum holding age.
            age = i - 1 - calendar.index(lot.acquired_on)
            ages[lot.instrument_id] = min(ages.get(lot.instrument_id, age), age)
        equity = book.cash_fen + sum(q * marks[k] for k, q in quantities.items())
        if equity <= 0:
            return finish("nonpositive_equity", day)
        weights = {k: q * marks[k] / equity for k, q in quantities.items()}
        cross = universe.loc[universe.trade_date.eq(pd.Timestamp(decision_day))].copy()
        daily_scores = scores.loc[scores.trade_date.eq(pd.Timestamp(decision_day))]
        if "fit_asof" not in daily_scores or (
            pd.to_datetime(daily_scores.fit_asof).isna().any()
            or pd.to_datetime(daily_scores.fit_asof).ge(pd.Timestamp(decision_day)).any()
        ):
            return finish("model_fit_cutoff_not_strictly_before_decision", day)
        cross = cross.merge(
            daily_scores[["instrument_id", "score"]],
            on="instrument_id",
            how="left",
            validate="one_to_one",
        )
        scheduled = (i - first) % config.rebalance_sessions == 0
        if scheduled and daily_scores.empty:
            return finish("scheduled_scores_missing", day)
        try:
            if not scheduled:
                # Risk review remains daily; suppress discretionary ranking changes.
                cross["score"] = float("nan")
            decision = buffered_target(decision_day, cross, weights, ages, config)
        except ValueError as exc:
            return finish(str(exc), day)
        desired = {p.instrument_id: p.target_weight for p in decision.target.positions}
        orders = []
        for code in sorted(quantities.keys() | desired.keys()):
            if code not in marks:
                return finish(f"decision_mark_unknown:{code}", day)
            # Freeze desired shares at t close. No sizing on t+1's unknown price.
            target_qty = int(desired.get(code, 0) * equity / marks[code])
            difference = target_qty - quantities.get(code, 0)
            risk_sale = difference < 0 and decision.risk_reduction_one_way_turnover > 0
            if not difference or (
                abs(difference) * marks[code] < config.min_trade_fen
                and not risk_sale
                and target_qty != 0
            ):
                continue
            orders.append(
                ResearchOrder(
                    f"ml:{day}:{code}",
                    code,
                    "buy" if difference > 0 else "sell",
                    abs(difference),
                    decision_day,
                )
            )
        batch = ResearchDay(
            day,
            tuple(orders),
            evidence.contexts,
            evidence.marks,
            evidence.corporate_processing_complete,
        )
        local_attempted = set(attempted)
        advance = advance_research_day(book, calendar, i, batch, local_attempted)
        if advance.status == "stopped":
            return finish(advance.reason, day)
        record = advance.record
        assert record is not None
        # Buy-side execution controls: price gaps/failed sales cannot increase
        # a pre-existing risk breach. A small explicit band accommodates marking
        # and modeled fees; passive price drift itself cannot force a fake sale.
        execution_marks = {m.instrument_id: m.price_fen for m in evidence.marks}

        def exposures(state, execution_marks=execution_marks, cross=cross):
            amounts = {}
            for lot in state.lots:
                amounts[lot.instrument_id] = amounts.get(lot.instrument_id, 0) + (
                    lot.quantity * execution_marks[lot.instrument_id]
                )
            nav = state.cash_fen + sum(amounts.values())
            by_name = {k: v / nav for k, v in amounts.items()}
            by_industry = {}
            industries = cross.set_index("instrument_id").industry.to_dict()
            for k, weight in by_name.items():
                group = industries[k]
                by_industry[group] = by_industry.get(group, 0) + weight
            return by_name, by_industry

        before_name, before_industry = exposures(book)
        after_name, after_industry = exposures(record.book)
        tolerance = config.execution_weight_tolerance
        breach = len(after_name) > config.max_positions
        breach |= sum(after_name.values()) > max(
            config.gross_exposure + tolerance, sum(before_name.values()) + 1e-12
        )
        breach |= any(
            v > max(config.max_weight + tolerance, before_name.get(k, 0) + 1e-12)
            for k, v in after_name.items()
        )
        breach |= any(
            v > max(config.max_industry_weight + tolerance, before_industry.get(k, 0) + 1e-12)
            for k, v in after_industry.items()
        )
        if breach and any(o.side == "buy" for o in orders):
            # Repeat the hypothetical day with sells only; no mutations escaped.
            batch = ResearchDay(
                day,
                tuple(o for o in orders if o.side == "sell"),
                evidence.contexts,
                evidence.marks,
                evidence.corporate_processing_complete,
            )
            local_attempted = set(attempted)
            advance = advance_research_day(book, calendar, i, batch, local_attempted)
            record = advance.record
            assert record is not None
            deferred = True
        else:
            deferred = False
        decisions.append(
            {
                "signal_date": str(decision_day),
                "execution_date": str(day),
                "scheduled_rebalance": scheduled,
                "planned_one_way_turnover": decision.planned_one_way_turnover,
                "risk_reduction_one_way_turnover": decision.risk_reduction_one_way_turnover,
                "buys_deferred_after_blocked_exits": deferred,
                "target_weights": desired,
                "buy_risk_guard_triggered": bool(breach),
            }
        )
        book, attempted = record.book, local_attempted
        records.append(record)
        marks = {m.instrument_id: m.price_fen for m in evidence.marks}
    return finish()
