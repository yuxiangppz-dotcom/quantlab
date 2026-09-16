"""Connect daily ML scores to existing stock quantity/cash scenario accounting.

Decisions and desired shares use previous-session marks only. Execution-day
bars constrain fills inside the existing kernel, never the predictive ranking.
No real-data certification or broker authority is inferred from this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pandas as pd

from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.corporate import (
    apply_events,
    capture_entitlements,
    new_state,
    receivable_value,
)
from quantlab.research.ml.execution import admit_orders, exposure_report
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
    corporate_state: dict


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
    corporate_actions=(),
    checkpoint_dir=None,
    binding=None,
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
    if hasattr(market_days, "get"):
        batches = market_days
    else:
        if len({d.session for d in market_days}) != len(market_days):
            raise ValueError("duplicate market day")
        if any(day.orders for day in market_days):
            raise ValueError("market evidence batches must not inject orders")
        batches = {d.session: d for d in market_days}
    if len({e.event_id for e in corporate_actions}) != len(corporate_actions):
        raise ValueError("duplicate corporate event id")
    if any(e.ex_date not in calendar or e.record_date not in calendar for e in corporate_actions):
        raise ValueError("corporate record/ex date outside complete trading calendar")
    book = ResearchBook(calendar[first - 1], initial_cash_fen)
    if len({m.instrument_id for m in initial_marks}) != len(initial_marks):
        raise ValueError("duplicate initial mark")
    if any(m.session != book.asof_date for m in initial_marks):
        raise ValueError("initial marks must be from the decision session")
    marks = {m.instrument_id: m.price_fen for m in initial_marks}
    records, decisions, attempted = [], [], set()
    corporate_state = capture_entitlements(new_state(), book, corporate_actions)
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
        return MLReplayResult(schedule, tuple(decisions), corporate_state)

    from quantlab.research.ml.artifacts import checkpoint_read, checkpoint_write

    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        if not binding:
            raise ValueError("checkpoint binding required")
    checkpoint_gap = False
    for i in range(first, last + 1):
        day = calendar[i]
        checkpoint = checkpoint_dir / f"{day}.json" if checkpoint_dir is not None else None
        if checkpoint is not None and checkpoint.exists():
            if checkpoint_gap:
                raise ValueError("noncontiguous replay checkpoints")
            saved = checkpoint_read(checkpoint)
            if saved["binding"] != binding or saved["index"] != i:
                raise ValueError("checkpoint input binding mismatch")
            record = saved["record"]
            if record.session != day or record.book.asof_date != day:
                raise ValueError("checkpoint date mismatch")
            book, attempted, marks = record.book, set(saved["attempted"]), saved["marks"]
            corporate_state = saved["corporate_state"]
            records.append(record)
            decisions.append(saved["decision"])
            continue
        checkpoint_gap = True
        evidence = batches.get(day)
        if evidence is None:
            return finish("session_batch_missing", day)
        state = {
            "book": book,
            "marks": marks,
            "attempted": attempted,
            "corporate_state": corporate_state,
        }
        try:
            plan = plan_orders(
                book,
                marks,
                corporate_state,
                scores,
                universe,
                calendar,
                day,
                (i - first) % config.rebalance_sessions == 0,
                config,
            )
            state, record, decision = settle_plan(
                state, plan, evidence, corporate_actions, calendar, calendar[first - 1], config
            )
        except ValueError as exc:
            return finish(str(exc), day)
        book, marks = state["book"], state["marks"]
        attempted, corporate_state = set(state["attempted"]), state["corporate_state"]
        records.append(record)
        decisions.append(decision)
        if checkpoint is not None:
            checkpoint_write(
                checkpoint,
                {
                    "binding": binding,
                    "index": i,
                    "record": record,
                    "attempted": attempted,
                    "marks": marks,
                    "corporate_state": corporate_state,
                    "decision": decisions[-1],
                },
            )
    return finish()


def plan_orders(book, marks, corporate_state, scores, universe, calendar, day, scheduled, config):
    """Pure decision using completed-session information, shared by replay and daily service."""
    i = calendar.index(day)
    decision_day = calendar[i - 1]
    if book.asof_date != decision_day:
        raise ValueError("decision book must be from the preceding session")
    scores, universe = scores.copy(), universe.copy()
    scores["trade_date"] = pd.to_datetime(scores.trade_date)
    universe["trade_date"] = pd.to_datetime(universe.trade_date)
    if scores.duplicated(["trade_date", "instrument_id"]).any():
        raise ValueError("duplicate scores")
    if universe.duplicated(["trade_date", "instrument_id"]).any():
        raise ValueError("duplicate universe")
    quantities = {}
    ages = {}
    for lot in book.lots:
        if lot.instrument_id not in marks:
            raise ValueError(f"decision_mark_unknown:{lot.instrument_id}")
        quantities[lot.instrument_id] = quantities.get(lot.instrument_id, 0) + lot.quantity
        # Last acquisition conservatively resets minimum holding age.
        age = i - 1 - calendar.index(lot.acquired_on)
        ages[lot.instrument_id] = min(ages.get(lot.instrument_id, age), age)
    equity = book.cash_fen + sum(q * marks[k] for k, q in quantities.items())
    equity += receivable_value(corporate_state)
    if equity <= 0:
        raise ValueError("nonpositive_equity")
    weights = {k: q * marks[k] / equity for k, q in quantities.items()}
    cross = universe.loc[universe.trade_date.eq(pd.Timestamp(decision_day))].copy()
    daily_scores = scores.loc[scores.trade_date.eq(pd.Timestamp(decision_day))]
    if "fit_asof" not in daily_scores or (
        pd.to_datetime(daily_scores.fit_asof).isna().any()
        or pd.to_datetime(daily_scores.fit_asof).ge(pd.Timestamp(decision_day)).any()
    ):
        raise ValueError("model_fit_cutoff_not_strictly_before_decision")
    cross = cross.merge(
        daily_scores[["instrument_id", "score"]],
        on="instrument_id",
        how="left",
        validate="one_to_one",
    )
    if scheduled and daily_scores.empty:
        raise ValueError("scheduled_scores_missing")
    try:
        if not scheduled:
            # Risk review remains daily; suppress discretionary ranking changes.
            cross["score"] = float("nan")
        decision = buffered_target(decision_day, cross, weights, ages, config)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    desired = {p.instrument_id: p.target_weight for p in decision.target.positions}
    orders = []
    for code in sorted(quantities.keys() | desired.keys()):
        if code not in marks:
            raise ValueError(f"decision_mark_unknown:{code}")
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
    industries = cross.set_index("instrument_id").industry.to_dict()
    orders, deferred = admit_orders(orders, book, marks, industries, config)
    return {
        "orders": orders,
        "industries": industries,
        "signal_date": str(decision_day),
        "execution_date": str(day),
        "model_ids": sorted(daily_scores.model_id.unique().tolist())
        if "model_id" in daily_scores
        else [],
        "scheduled_rebalance": scheduled,
        "planned_one_way_turnover": decision.planned_one_way_turnover,
        "risk_reduction_one_way_turnover": decision.risk_reduction_one_way_turnover,
        "pretrade_deferred": deferred,
        "execution_policy": "prior_close_admission_no_same_auction_sale_credit",
        "target_weights": desired,
    }


def settle_plan(state, plan, evidence, corporate_actions, calendar, inception, config):
    """Consume frozen orders; no score, feature, ranking or target recalculation here."""
    book = state["book"]
    day = date.fromisoformat(plan["execution_date"])
    i = calendar.index(day)
    if evidence.session != day or evidence.orders or book.asof_date != calendar[i - 1]:
        raise ValueError("settlement evidence/book chronology mismatch")
    if plan["signal_date"] != str(book.asof_date):
        raise ValueError("frozen decision date mismatch")
    affected = {e.instrument_id for e in corporate_actions if e.ex_date == day}
    cancelled = [o.order_id for o in plan["orders"] if o.instrument_id in affected]
    orders = tuple(o for o in plan["orders"] if o.instrument_id not in affected)
    morning_book, next_corporate, movements = apply_events(
        book, day, corporate_actions, state["corporate_state"], inception
    )
    batch = ResearchDay(
        day, orders, evidence.contexts, evidence.marks, evidence.corporate_processing_complete
    )
    attempted = set(state["attempted"])
    advance = advance_research_day(
        morning_book, calendar, i, batch, attempted, buy_cash_budget_fen=book.cash_fen
    )
    if advance.status == "stopped":
        raise ValueError(advance.reason)
    record = advance.record
    assert record is not None
    receivable = receivable_value(next_corporate)
    record = replace(record, marked_equity_fen=record.marked_equity_fen + receivable)
    marks = {m.instrument_id: m.price_fen for m in evidence.marks}
    decision = {k: v for k, v in plan.items() if k not in {"orders", "industries"}}
    decision.update(
        {
            "realized_exposure": exposure_report(
                record.book, marks, plan["industries"], config, receivable
            ),
            "corporate_movements": movements,
            "corporate_cancelled_orders": cancelled,
            "receivable_fen": receivable,
        }
    )
    state = {
        "book": record.book,
        "marks": marks,
        "attempted": attempted,
        "corporate_state": capture_entitlements(next_corporate, record.book, corporate_actions),
    }
    return state, record, decision
