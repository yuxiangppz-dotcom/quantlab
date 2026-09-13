"""Financial integration tests for the risk-ledger daily closed loop."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.market_risk import IndexClose, RiskState
from quantlab.research.quantity_kernel import (
    ResearchBook,
    ResearchFeeScenario,
    ResearchQuantityRules,
)
from quantlab.research.quantity_scheduler import (
    RawCloseMark,
    ResearchDay,
    ResearchSession,
    simulate_research_schedule,
)
from quantlab.research.risk_ledger_loop import (
    DAY_RISK_UNKNOWN,
    LedgerSessionEvidence,
    RiskLedgerConfig,
    run_risk_ledger_loop,
)

START = date(2024, 1, 1)
RULES = ResearchQuantityRules(
    scenario_id="synthetic_rules",
    effective_from=date(2023, 1, 1),
    effective_through=date(2026, 12, 31),
    buy_minimum=100,
    buy_increment=100,
    sell_minimum=100,
    sell_increment=100,
    max_order_quantity=100000,
    full_position_odd_exit=True,
)
FEES = ResearchFeeScenario(
    scenario_id="synthetic_fees",
    effective_from=date(2023, 1, 1),
    effective_through=date(2026, 12, 31),
    commission_rate=Decimal("0"),
    minimum_commission_fen=0,
    buy_stamp_rate=Decimal("0"),
    sell_stamp_rate=Decimal("0"),
    additional_fee_rate=Decimal("0"),
    additional_fee_fixed_fen=0,
    adverse_slippage_rate=Decimal("0"),
)
CASH_FEN = 20_000_000  # 200,000 CNY in fen


def _weekdays(count: int, start: date = START) -> tuple[date, ...]:
    sessions: list[date] = []
    day = start
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    return tuple(sessions)


def _context(
    code: str,
    exec_day: date,
    next_day: date,
    close: int,
    *,
    signal_day: date,
    at_down_limit: bool = False,
    session_volume: int = 10**9,
) -> ResearchSession:
    return ResearchSession(
        instrument_id=code,
        execution_date=exec_day,
        next_session=next_day,
        evidence_date=exec_day,
        calendar_verified=True,
        market_open=True,
        corporate_actions_processed=True,
        raw_close_fen=close,
        low_fen=close,
        high_fen=close,
        down_limit_fen=close if at_down_limit else int(close * 0.9),
        up_limit_fen=int(close * 1.1),
        prior20_amount_fen=10**15,
        prior20_asof=signal_day,
        prior20_sessions=20,
        session_amount_fen=10**15,
        session_volume_shares=session_volume,
        participation=Decimal("0.05"),
        rules=RULES,
        fees=FEES,
    )


def _evidence(
    day: date,
    next_day: date,
    prices: dict[str, int],
    *,
    signal_day: date,
    limit_down: tuple[str, ...] = (),
    volumes: dict[str, int] | None = None,
) -> LedgerSessionEvidence:
    volumes = volumes or {}
    return LedgerSessionEvidence(
        session=day,
        contexts=tuple(
            _context(
                code,
                day,
                next_day,
                close,
                signal_day=signal_day,
                at_down_limit=code in limit_down,
                session_volume=volumes.get(code, 10**9),
            )
            for code, close in sorted(prices.items())
        ),
        marks=tuple(RawCloseMark(code, day, close) for code, close in sorted(prices.items())),
        corporate_processing_complete=True,
    )


def _evidence_for_calendar(
    calendar,
    prices_by_day,
    *,
    limit_down_by_day: dict[int, tuple[str, ...]] | None = None,
    volumes_by_day: dict[int, dict[str, int]] | None = None,
):
    """prices_by_day: mapping index -> dict[str, int]."""
    limit_down_by_day = limit_down_by_day or {}
    volumes_by_day = volumes_by_day or {}
    evidence = {}
    for i, day in enumerate(calendar):
        if i in prices_by_day and i + 1 < len(calendar):
            evidence[day] = _evidence(
                day,
                calendar[i + 1],
                prices_by_day[i],
                signal_day=calendar[i - 1],
                limit_down=limit_down_by_day.get(i, ()),
                volumes=volumes_by_day.get(i),
            )
    return evidence


def _target(weights: dict[str, float], as_of: date) -> TargetPortfolio:
    positions = tuple(
        TargetWeight(instrument_id=code, target_weight=weight)
        for code, weight in sorted(weights.items())
    )
    return TargetPortfolio(as_of=as_of, positions=positions, cash_weight=1 - sum(weights.values()))


def _flat_book(cash_fen: int, asof: date) -> ResearchBook:
    return ResearchBook(asof_date=asof, cash_fen=cash_fen)


def _config(rule_id: str = "C80", **overrides) -> RiskLedgerConfig:
    params = dict(
        rule_id=rule_id,
        nav_series_id="synthetic_nav",
        nav_source="synthetic_ledger",
        generation_rules=RULES,
        return_source="synthetic_unscaled" if rule_id in ("V", "VM", "VMD") else "",
        index_source="synthetic_index" if rule_id in ("M", "VM", "VMD") else "",
    )
    params.update(overrides)
    return RiskLedgerConfig(**params)


class TestParityAndEntry:
    def test_c80_loop_matches_old_scheduler_book(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000, "B": 2000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.4, "B": 0.4}, day) for day in calendar[1:5]}
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[4],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert result.status == "completed_scenario"
        # Day 1 decides; its intents execute on day 2.
        day1, day2 = result.records[0], result.records[1]
        assert [o.instrument_id for o in day1.next_intents] == ["A", "B"]
        assert all(o.side == "buy" for o in day1.next_intents)
        # 0.4*20,000,000/1000 = 8000 shares of A; 4000 of B.
        assert dict((o.instrument_id, o.desired_quantity) for o in day1.next_intents) == {
            "A": 8000,
            "B": 4000,
        }
        assert day2.book.cash_fen == CASH_FEN - 8000 * 1000 - 4000 * 2000
        assert day2.book.cash_fen == 4_000_000
        assert day2.position_value_fen == 16_000_000
        assert day2.marked_equity_fen == CASH_FEN
        # Parity: feeding the loop's intents as pre-generated batches to the
        # whole-schedule entry yields the same completed book.
        old = simulate_research_schedule(
            _flat_book(CASH_FEN, calendar[0]),
            calendar,
            (
                ResearchDay(
                    session=calendar[1],
                    orders=(),
                    contexts=evidence[calendar[1]].contexts,
                    marks=evidence[calendar[1]].marks,
                    corporate_processing_complete=True,
                ),
                ResearchDay(
                    session=calendar[2],
                    orders=day1.next_intents,
                    contexts=evidence[calendar[2]].contexts,
                    marks=evidence[calendar[2]].marks,
                    corporate_processing_complete=True,
                ),
            ),
            requested_end=calendar[2],
        )
        assert old.status == "completed_scenario"
        assert old.book.cash_fen == day2.book.cash_fen
        assert sorted((x.instrument_id, x.quantity) for x in old.book.lots) == sorted(
            (x.instrument_id, x.quantity) for x in day2.book.lots
        )
        # Steady state afterwards: no further intents.
        assert all(record.next_intents == () for record in result.records[2:])

    def test_minimum_commission_entered_exactly(self):
        calendar = _weekdays(4)
        prices = {i: {"A": 1000} for i in range(1, 4)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.04}, day) for day in calendar[1:3]}
        fees = ResearchFeeScenario(
            scenario_id="commission_only",
            effective_from=date(2023, 1, 1),
            effective_through=date(2026, 12, 31),
            commission_rate=Decimal("0.000086"),
            minimum_commission_fen=500,
            buy_stamp_rate=Decimal("0"),
            sell_stamp_rate=Decimal("0"),
            additional_fee_rate=Decimal("0"),
            additional_fee_fixed_fen=0,
            adverse_slippage_rate=Decimal("0"),
        )
        patched = {
            day: LedgerSessionEvidence(
                session=day,
                contexts=tuple(
                    ResearchSession(
                        **{
                            **ctx.__dict__,
                            "fees": fees,
                        }
                    )
                    for ctx in ev.contexts
                ),
                marks=ev.marks,
                corporate_processing_complete=True,
            )
            for day, ev in evidence.items()
            if day in calendar[1:3]
        }
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[2],
            base_targets=base,
            evidence=patched,
            config=_config("C80"),
        )
        assert result.status == "completed_scenario"
        day2 = result.records[1]
        # 0.04*20,000,000/1000 = 800 shares -> notional 800,000 fen,
        # commission max(500, 800000*0.000086=68.8 -> 69) = 500 fen minimum.
        assert day2.attempts[0].transition.status == "simulated"
        assert day2.attempts[0].transition.commission_fen == 500
        assert day2.modeled_fees_fen == 500
        assert day2.book.cash_fen == CASH_FEN - 800 * 1000 - 500
        assert day2.marked_equity_fen == CASH_FEN - 500


class TestDrawdownLoop:
    def test_tier2_dedrisk_conserves_value_exactly(self):
        calendar = _weekdays(7)
        prices = {
            1: {"A": 1000},
            2: {"A": 1000},
            3: {"A": 800},
            4: {"A": 800},
            5: {"A": 800},
            6: {"A": 800},
        }
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.8}, day) for day in calendar[1:6]}
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[5],
            base_targets=base,
            evidence=evidence,
            config=_config("D"),
            drawdown_state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert result.status == "completed_scenario"
        day2 = result.records[1]
        assert day2.book.cash_fen == CASH_FEN - 16000 * 1000 == 4_000_000
        day3 = result.records[2]
        # Equity 4,000,000 + 16000*800 = 16,800,000; drawdown 16% -> tier2, cap 0.2.
        assert day3.marked_equity_fen == 16_800_000
        assert day3.decision.cap == Decimal("0.2")
        sell = [o for o in day3.next_intents if o.side == "sell"]
        assert len(sell) == 1 and sell[0].instrument_id == "A"
        # Target 0.2*16,800,000/800 = 4200 shares, so 11,800 are sold next day.
        assert sell[0].desired_quantity == 11_800
        day4 = result.records[3]
        assert day4.book.cash_fen == 4_000_000 + 11_800 * 800 == 13_440_000
        assert day4.position_value_fen == 4200 * 800 == 3_360_000
        assert day4.marked_equity_fen == 16_800_000
        assert day4.actual_gross_exposure == Decimal("0.2")
        assert day4.risk_target_gross_exposure == pytest.approx(0.2)
        assert day4.next_intents == ()

    def test_recovery_returns_to_base_target_without_compounding(self):
        calendar = _weekdays(10)
        prices = {
            1: {"A": 1000},
            2: {"A": 1000},
            3: {"A": 800},
            4: {"A": 800},
            5: {"A": 800},
            6: {"A": 1000},
            7: {"A": 1100},
            8: {"A": 1200},
            9: {"A": 1200},
        }
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.8}, day) for day in calendar[1:9]}
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[8],
            base_targets=base,
            evidence=evidence,
            config=_config("D"),
            drawdown_state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert result.status == "completed_scenario"
        # Day 3 (close 800): dd = (4,000,000+12,800,000-16,800,000)/20,000,000
        # = 16% -> tier2 cap 0.2; sells follow on day 4 down to 4,200 shares.
        day4 = result.records[3]
        assert sum(x.quantity for x in day4.book.lots) == 4200
        # Day 6 (close 1000): equity 13,440,000 + 4,200,000 = 17,640,000,
        # drawdown 11.8% <= 12% -> one-tier recovery to 0.5 cap.
        day6 = result.records[5]
        assert day6.marked_equity_fen == 17_640_000
        assert day6.decision.coefficient == Decimal("0.5")
        # Day 7 (close 1100): the rebuilt position executes at 1100, so equity
        # is still ~9.7% below the watermark and the tier holds at 0.5. Day 8
        # (close 1200) pushes the drawdown under 8%: cap returns to 0.8 and the
        # final target settles at the still-valid base with no double-scaled
        # residual from the de-risked period.
        assert result.records[6].decision.coefficient == Decimal("0.5")
        final = result.records[-1]
        assert final.decision.cap == Decimal("0.8")
        assert final.risk_target_gross_exposure == pytest.approx(0.8)

    def test_risk_exit_and_strategy_expiry_merge_into_one_sell(self):
        calendar = _weekdays(7)
        prices = {i: {"A": 1000, "B": 1000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        # B's base target expires after day 2 while D simultaneously de-risks.
        base = {
            calendar[1]: _target({"A": 0.6, "B": 0.3}, calendar[1]),
            calendar[2]: _target({"A": 0.6, "B": 0.3}, calendar[2]),
            calendar[3]: _target({"A": 0.6}, calendar[3]),
            calendar[4]: _target({"A": 0.6}, calendar[4]),
            calendar[5]: _target({"A": 0.6}, calendar[5]),
        }
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[5],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert result.status == "completed_scenario"
        # C80 scales the 0.9 gross base to 0.8: day-1 buys are proportional
        # (A 0.6*0.8/0.9 -> 10,600 shares; B -> 5,300 shares).
        day2 = result.records[1]
        assert sorted((x.instrument_id, x.quantity) for x in day2.book.lots) == [
            ("A", 10_600),
            ("B", 5_300),
        ]
        for record in result.records:
            sells = [o for o in record.next_intents if o.side == "sell"]
            per_instrument = [o.instrument_id for o in sells]
            assert len(per_instrument) == len(set(per_instrument))
        # B's expiry and the risk scaling merge into one full-exit sell.
        day3 = result.records[2]
        b_sells = [o for o in day3.next_intents if o.instrument_id == "B"]
        assert len(b_sells) == 1 and b_sells[0].desired_quantity == 5_300
        # After B expires it is absent from every later target and ledger.
        final = result.records[-1]
        assert all(x.instrument_id == "A" for x in final.book.lots)
        assert final.book.cash_fen == 20_000_000 - 12_000 * 1000


class TestCapsAndBlocks:
    def _m_world(self):
        calendar = _weekdays(210)
        closes = [1000 + 2 * i for i in range(204)] + [1000, 900, 900, 900, 900, 900]
        prices = {i: {"A": closes[i]} for i in range(1, 210)}
        return calendar, prices

    def test_cap_zero_with_limit_down_keeps_positions_and_reports_gap(self):
        calendar, prices = self._m_world()
        closes = [1000 + 2 * i for i in range(204)] + [1000, 900, 900, 900, 900, 900]
        evidence = _evidence_for_calendar(calendar, prices, limit_down_by_day={205: ("A",)})
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:209]}
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[208],
            base_targets=base,
            evidence=evidence,
            config=_config("M", index_source="synthetic_index"),
            index_closes=tuple(
                IndexClose(session, Decimal(closes[i]))
                for i, session in enumerate(calendar)
            ),
        )
        assert result.status == "completed_scenario"
        # Warmup days: M unknown, no intents, holdings kept.
        assert all(record.status == DAY_RISK_UNKNOWN for record in result.records[:198])
        assert all(record.next_intents == () for record in result.records[:198])
        # First known M day (index 199) is above its mean: buying resumes.
        first_known = result.records[198]
        assert first_known.decision.cap == Decimal("0.8")
        buys = [o for o in first_known.next_intents if o.side == "buy"]
        assert buys and buys[0].instrument_id == "A"
        held = [record for record in result.records if record.book.lots]
        assert held
        # The crash day decision: below mean -> cap 0, all-cash target.
        crash = next(
            record for record in result.records if record.decision.cap == Decimal("0")
        )
        assert crash.risk_target is not None
        assert crash.risk_target.positions == ()
        assert crash.risk_target.cash_weight == 1.0
        # Execution at the down limit is blocked: shares remain and keep
        # marking, while the target claims zero exposure.
        blocked = result.records[204]
        assert blocked.attempts
        assert all(
            attempt.transition.status == "blocked"
            and attempt.transition.reason == "directional_close_limit"
            for attempt in blocked.attempts
        )
        assert sum(x.quantity for x in blocked.book.lots) > 0
        assert blocked.actual_gross_exposure > 0
        assert blocked.risk_target_gross_exposure == 0.0
        # Next day the loop re-issues a new explicit sell that executes.
        final = result.records[-1]
        assert final.book.lots == ()
        assert final.actual_gross_exposure == 0

    def test_partial_capacity_then_next_day_completion(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000} for i in range(1, 6)}
        # 5% participation of 20,000 shares = 1,000-share cap on day index 2.
        evidence = _evidence_for_calendar(
            calendar, prices, volumes_by_day={2: {"A": 20000}}
        )
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[4],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert result.status == "completed_scenario"
        day2 = result.records[1]
        partial = day2.attempts[0].transition
        assert partial.status == "simulated"
        assert partial.reason == "partial_under_declared_constraints"
        assert partial.simulated_quantity == 1000
        lot = day2.book.lots[0]
        assert lot.quantity == 1000
        assert lot.sellable_on > lot.acquired_on  # T+1 lot lock
        assert day2.book.cash_fen == CASH_FEN - 1000 * 1000
        # Day 2's diff re-issues a fresh buy for the remainder (new order id).
        rebuy = [o for o in day2.next_intents if o.side == "buy"]
        assert len(rebuy) == 1 and rebuy[0].instrument_id == "A"
        day3 = result.records[2]
        held = sum(x.quantity for x in day3.book.lots)
        assert held == 10_000  # 0.5*20,000,000/1000


class TestStopAndDeterminism:
    def test_missing_mark_stops_with_last_complete_book(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
        result = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[4],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert result.status == "completed_scenario"
        # Now remove day-4 marks while A is held: the path must stop there and
        # keep every unsold share.
        evidence[calendar[4]] = LedgerSessionEvidence(
            session=calendar[4],
            contexts=evidence[calendar[4]].contexts,
            marks=(),
            corporate_processing_complete=True,
        )
        stopped = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[4],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert stopped.status == "stopped"
        assert stopped.stopped_on == calendar[4]
        assert stopped.stop_reason == "raw_mark_unknown:A"
        assert stopped.records  # last complete day retained
        assert sum(x.quantity for x in stopped.book.lots) > 0
        assert stopped.valid_through == calendar[3]

    def test_future_evidence_mutation_leaves_earlier_days_unchanged(self):
        calendar = _weekdays(6)

        def build(day4_close: int):
            prices = {1: {"A": 1000}, 2: {"A": 1000}, 3: {"A": 1000}, 4: {"A": day4_close}}
            evidence = _evidence_for_calendar(calendar, prices)
            base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
            return run_risk_ledger_loop(
                initial_book=_flat_book(CASH_FEN, calendar[0]),
                calendar=calendar,
                requested_end=calendar[4],
                base_targets=base,
                evidence=evidence,
                config=_config("C80"),
            )

        first = build(1000)
        second = build(1400)
        for before, after in zip(first.records[:3], second.records[:3], strict=True):
            assert before.book == after.book
            assert before.next_intents == after.next_intents
            assert before.risk_target == after.risk_target
        # The mutated future close changes only that day's marks onward.
        assert first.records[3].marked_equity_fen != second.records[3].marked_equity_fen

    def test_restart_resumes_state_without_reset_or_double_counting(self):
        calendar = _weekdays(9, start=date(2024, 12, 23))  # spans the year end
        prices = {
            1: {"A": 1000},
            2: {"A": 1000},
            3: {"A": 800},
            4: {"A": 800},
            5: {"A": 800},
            6: {"A": 800},
            7: {"A": 800},
            8: {"A": 800},
        }
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.8}, day) for day in calendar[1:8]}
        whole = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[7],
            base_targets=base,
            evidence=evidence,
            config=_config("D"),
            drawdown_state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert whole.status == "completed_scenario"
        assert whole.records[2].session.year == 2024
        assert whole.records[-1].session.year == 2025
        # Simulated restart after day 4: replay the persisted book and state.
        partial = run_risk_ledger_loop(
            initial_book=_flat_book(CASH_FEN, calendar[0]),
            calendar=calendar,
            requested_end=calendar[4],
            base_targets=base,
            evidence=evidence,
            config=_config("D"),
            drawdown_state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert partial.status == "completed_scenario"
        resumed = run_risk_ledger_loop(
            initial_book=partial.book,
            calendar=calendar,
            requested_end=calendar[7],
            base_targets=base,
            evidence=evidence,
            config=_config("D"),
            drawdown_state=partial.drawdown_state,
        )
        assert resumed.status == "completed_scenario"
        assert resumed.book == whole.book
        assert resumed.drawdown_state == whole.drawdown_state
        for replayed, original in zip(
            resumed.records, whole.records[4:], strict=True
        ):
            assert replayed.book == original.book
            assert replayed.marked_equity_fen == original.marked_equity_fen

    def test_base_target_must_cover_every_session(self):
        calendar = _weekdays(4)
        evidence = _evidence_for_calendar(calendar, {1: {"A": 1000}, 2: {"A": 1000}})
        with pytest.raises(ValueError, match="base target missing"):
            run_risk_ledger_loop(
                initial_book=_flat_book(CASH_FEN, calendar[0]),
                calendar=calendar,
                requested_end=calendar[2],
                base_targets={},  # empty on purpose
                evidence=evidence,
                config=_config("C80"),
            )

    def test_d_rule_requires_explicit_initial_state(self):
        calendar = _weekdays(4)
        evidence = _evidence_for_calendar(calendar, {1: {"A": 1000}, 2: {"A": 1000}})
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:3]}
        with pytest.raises(ValueError, match="explicit initial or persisted"):
            run_risk_ledger_loop(
                initial_book=_flat_book(CASH_FEN, calendar[0]),
                calendar=calendar,
                requested_end=calendar[2],
                base_targets=base,
                evidence=evidence,
                config=_config("D"),
            )
