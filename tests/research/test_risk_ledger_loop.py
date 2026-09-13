"""Financial integration tests for the risk-ledger daily closed loop."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.market_risk import IndexClose, RiskState, UnscaledReturn
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
    LedgerSessionEvidence,
    RiskLedgerCheckpoint,
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
# Explicit nonzero manual fees: 0.02% commission (min 5 CNY), 0.1% sell stamp.
FEES_REAL = ResearchFeeScenario(
    scenario_id="synthetic_real_fees",
    effective_from=date(2023, 1, 1),
    effective_through=date(2026, 12, 31),
    commission_rate=Decimal("0.0002"),
    minimum_commission_fen=500,
    buy_stamp_rate=Decimal("0"),
    sell_stamp_rate=Decimal("0.001"),
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
    fees: ResearchFeeScenario = FEES,
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
        fees=fees,
    )


def _evidence(
    day: date,
    next_day: date,
    prices: dict[str, int],
    *,
    signal_day: date,
    limit_down: tuple[str, ...] = (),
    volumes: dict[str, int] | None = None,
    fees: ResearchFeeScenario = FEES,
    corporate: bool | None = True,
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
                fees=fees,
            )
            for code, close in sorted(prices.items())
        ),
        marks=tuple(RawCloseMark(code, day, close) for code, close in sorted(prices.items())),
        corporate_processing_complete=corporate,
    )


def _evidence_for_calendar(
    calendar,
    prices_by_day,
    *,
    limit_down_by_day: dict[int, tuple[str, ...]] | None = None,
    volumes_by_day: dict[int, dict[str, int]] | None = None,
    fees: ResearchFeeScenario = FEES,
):
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
                fees=fees,
            )
    return evidence


def _target(weights: dict[str, float], as_of: date) -> TargetPortfolio:
    positions = tuple(
        TargetWeight(instrument_id=code, target_weight=weight)
        for code, weight in sorted(weights.items())
    )
    return TargetPortfolio(as_of=as_of, positions=positions, cash_weight=1 - sum(weights.values()))


def _config(rule_id: str = "C80", **overrides) -> RiskLedgerConfig:
    params = dict(
        rule_id=rule_id,
        run_id="run_a",
        nav_series_id="synthetic_nav",
        nav_source="synthetic_ledger",
        generation_rules=RULES,
        return_source="synthetic_unscaled" if rule_id in ("V", "VM", "VMD") else "",
        index_source="synthetic_index" if rule_id in ("M", "VM", "VMD") else "",
    )
    params.update(overrides)
    return RiskLedgerConfig(**params)


def _start(state=None, config=None):
    return RiskLedgerCheckpoint.start(
        signal_date=START,
        initial_cash_fen=CASH_FEN,
        config=config or _config("C80"),
        drawdown_state=state,
    )


def _run(calendar, end, base, evidence, config, **extra):
    checkpoint = extra.pop("checkpoint", None) or _start(
        state=extra.pop("state", None), config=config
    )
    return run_risk_ledger_loop(
        checkpoint=checkpoint,
        calendar=calendar,
        requested_end=end,
        base_targets=base,
        evidence=evidence,
        config=config,
        **extra,
    )


class TestParityAndEntry:
    def test_c80_loop_matches_old_scheduler_book(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000, "B": 2000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.4, "B": 0.4}, day) for day in calendar[1:5]}
        result = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert result.status == "completed_scenario"
        day1, day2 = result.records[0], result.records[1]
        assert dict((o.instrument_id, o.desired_quantity) for o in day1.next_intents) == {
            "A": 8000,
            "B": 4000,
        }
        assert day2.book.cash_fen == 4_000_000
        assert day2.position_value_fen == 16_000_000
        assert day2.marked_equity_fen == CASH_FEN
        # Parity: the loop's intents as pre-generated batches reproduce the
        # same completed book through the whole-schedule entry.
        old = simulate_research_schedule(
            ResearchBook(asof_date=calendar[0], cash_fen=CASH_FEN),
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
        assert all(record.next_intents == () for record in result.records[2:])

    def test_exact_weight_budget_avoids_float_truncation(self):
        # 0.57 * 20,000,000 = 11,400,000 fen exactly; at 11,400 fen/share the
        # mathematical budget is 1000 shares. The float product is
        # 11399999.999999998, which used to truncate to 900 grid shares.
        calendar = _weekdays(4)
        prices = {1: {"A": 11400}, 2: {"A": 11400}}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.57}, day) for day in calendar[1:3]}
        result = _run(calendar, calendar[2], base, evidence, _config("C80"))
        day1 = result.records[0]
        assert day1.next_intents[0].desired_quantity == 1000
        assert result.records[1].book.cash_fen == CASH_FEN - 1000 * 11400

    def test_weight_budget_boundaries_follow_exact_floor(self):
        calendar = _weekdays(4)
        prices = {1: {"A": 11400}, 2: {"A": 11400}}
        evidence = _evidence_for_calendar(calendar, prices)
        # 0.56943 * 20,000,000 = 11,388,600 fen -> 999 shares -> grid 900.
        base = {day: _target({"A": 0.56943}, day) for day in calendar[1:3]}
        result = _run(calendar, calendar[2], base, evidence, _config("C80"))
        assert result.records[0].next_intents[0].desired_quantity == 900
        # 0.57001 * 20,000,000 = 11,400,200 fen -> 1000 shares -> grid 1000.
        base_up = {day: _target({"A": 0.57001}, day) for day in calendar[1:3]}
        result_up = _run(calendar, calendar[2], base_up, evidence, _config("C80"))
        assert result_up.records[0].next_intents[0].desired_quantity == 1000

    def test_scaled_weight_keeps_exact_shares(self):
        # After the adapter scales 0.9 gross to the 0.8 cap, the 0.6 leg sits
        # at 0.6*0.8/0.9; the exact decimal budget must decide the shares.
        calendar = _weekdays(4)
        prices = {1: {"A": 1000, "B": 2000}, 2: {"A": 1000, "B": 2000}}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.6, "B": 0.3}, day) for day in calendar[1:3]}
        result = _run(calendar, calendar[2], base, evidence, _config("C80"))
        scaled_weight = 0.6 * 0.8 / 0.9
        expected = int(Decimal(str(scaled_weight)) * Decimal(CASH_FEN) // Decimal(1000))
        expected -= expected % 100
        assert result.records[0].next_intents[0].desired_quantity == expected

    def test_minimum_commission_entered_exactly(self):
        calendar = _weekdays(4)
        prices = {i: {"A": 1000} for i in range(1, 4)}
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
        evidence = _evidence_for_calendar(calendar, prices, fees=fees)
        base = {day: _target({"A": 0.04}, day) for day in calendar[1:3]}
        result = _run(calendar, calendar[2], base, evidence, _config("C80"))
        day2 = result.records[1]
        assert day2.attempts[0].transition.commission_fen == 500
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
        result = _run(
            calendar,
            calendar[5],
            base,
            evidence,
            _config("D"),
            state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert result.status == "completed_scenario"
        day2 = result.records[1]
        assert day2.book.cash_fen == 4_000_000
        day3 = result.records[2]
        assert day3.marked_equity_fen == 16_800_000
        assert day3.decision.cap == Decimal("0.2")
        sell = [o for o in day3.next_intents if o.side == "sell"]
        assert len(sell) == 1 and sell[0].desired_quantity == 11_800
        day4 = result.records[3]
        assert day4.book.cash_fen == 13_440_000
        assert day4.position_value_fen == 3_360_000
        assert day4.marked_equity_fen == 16_800_000
        assert day4.actual_gross_exposure == Decimal("0.2")
        assert day4.risk_target_gross_exposure == pytest.approx(0.2)

    def test_tier2_with_nonzero_fees_tracks_every_fen(self):
        calendar = _weekdays(7)
        prices = {
            1: {"A": 1000},
            2: {"A": 1000},
            3: {"A": 800},
            4: {"A": 800},
            5: {"A": 800},
            6: {"A": 800},
        }
        evidence = _evidence_for_calendar(calendar, prices, fees=FEES_REAL)
        base = {day: _target({"A": 0.8}, day) for day in calendar[1:6]}
        result = _run(
            calendar,
            calendar[5],
            base,
            evidence,
            _config("D"),
            state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert result.status == "completed_scenario"
        day2 = result.records[1]
        # Buy 16,000 shares: notional 16,000,000, commission max(500, 3200).
        assert day2.book.cash_fen == CASH_FEN - 16_000_000 - 3200
        assert day2.modeled_fees_fen == 3200
        day3 = result.records[2]
        equity3 = CASH_FEN - 16_000_000 - 3200 + 16_000 * 800
        assert day3.marked_equity_fen == equity3
        # Drawdown (20,000,000 - 16,796,800)/20,000,000 = 16.016% -> tier2.
        assert day3.decision.cap == Decimal("0.2")
        sell = [o for o in day3.next_intents if o.side == "sell"][0]
        target_qty = int(Decimal("0.2") * Decimal(equity3) // Decimal(800))
        assert sell.desired_quantity == 16_000 - target_qty - (16_000 - target_qty) % 100
        day4 = result.records[3]
        sold_qty = sell.desired_quantity
        proceeds = sold_qty * 800
        sell_fees = max(500, int(Decimal(proceeds) * Decimal("0.0002"))) + int(
            Decimal(proceeds) * Decimal("0.001")
        )
        assert day4.book.cash_fen == day3.book.cash_fen + proceeds - sell_fees
        assert day4.marked_equity_fen == day4.book.cash_fen + (16_000 - sold_qty) * 800
        assert day4.modeled_fees_fen == sell_fees

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
        result = _run(
            calendar,
            calendar[8],
            base,
            evidence,
            _config("D"),
            state=RiskState.initial(Decimal(CASH_FEN), "synthetic_nav"),
        )
        assert result.status == "completed_scenario"
        assert sum(x.quantity for x in result.records[3].book.lots) == 4200
        assert result.records[5].decision.coefficient == Decimal("0.5")
        assert result.records[6].decision.coefficient == Decimal("0.5")
        final = result.records[-1]
        assert final.decision.cap == Decimal("0.8")
        assert final.risk_target_gross_exposure == pytest.approx(0.8)

    def test_risk_exit_and_strategy_expiry_merge_into_one_sell(self):
        calendar = _weekdays(7)
        prices = {i: {"A": 1000, "B": 1000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {
            calendar[1]: _target({"A": 0.6, "B": 0.3}, calendar[1]),
            calendar[2]: _target({"A": 0.6, "B": 0.3}, calendar[2]),
            calendar[3]: _target({"A": 0.6}, calendar[3]),
            calendar[4]: _target({"A": 0.6}, calendar[4]),
            calendar[5]: _target({"A": 0.6}, calendar[5]),
        }
        result = _run(calendar, calendar[5], base, evidence, _config("C80"))
        assert result.status == "completed_scenario"
        day3 = result.records[2]
        b_sells = [o for o in day3.next_intents if o.instrument_id == "B"]
        assert len(b_sells) == 1 and b_sells[0].desired_quantity == 5_300
        final = result.records[-1]
        assert all(x.instrument_id == "A" for x in final.book.lots)
        assert final.book.cash_fen == 20_000_000 - 12_000 * 1000


class TestCapsAndBlocks:
    def _m_world(self):
        calendar = _weekdays(210)
        closes = [1000 + 2 * i for i in range(204)] + [1000, 900, 900, 900, 900, 900]
        prices = {i: {"A": closes[i]} for i in range(195, 210)}
        index_closes = tuple(
            IndexClose(session, Decimal(closes[i])) for i, session in enumerate(calendar)
        )
        return calendar, prices, index_closes

    def test_cap_zero_with_limit_down_keeps_positions_and_reports_gap(self):
        calendar, prices, index_closes = self._m_world()
        evidence = _evidence_for_calendar(calendar, prices, limit_down_by_day={205: ("A",)})
        base = {day: _target({"A": 0.5}, day) for day in calendar[200:209]}
        # Warmup predates the evaluation window: the loop starts after 200
        # sessions of index history, so M is known on its first decision day.
        start = RiskLedgerCheckpoint.start(
            signal_date=calendar[199],
            initial_cash_fen=CASH_FEN,
            config=_config("M", index_source="synthetic_index"),
        )
        result = run_risk_ledger_loop(
            checkpoint=start,
            calendar=calendar,
            requested_end=calendar[208],
            base_targets=base,
            evidence=evidence,
            config=_config("M", index_source="synthetic_index"),
            index_closes=index_closes,
        )
        assert result.status == "completed_scenario"
        first = result.records[0]
        assert first.session == calendar[200]
        assert first.decision.cap == Decimal("0.8")
        buys = [o for o in first.next_intents if o.side == "buy"]
        assert buys and buys[0].instrument_id == "A"
        # Crash decision below the 200-session mean: cap zero, all-cash target.
        crash = result.records[4]
        assert crash.decision.cap == Decimal("0")
        assert crash.risk_target.positions == ()
        assert crash.risk_target.cash_weight == 1.0
        # Execution at the down limit is blocked: shares remain and keep
        # marking while the target claims zero exposure; both are reported.
        blocked = result.records[5]
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
        evidence = _evidence_for_calendar(calendar, prices, volumes_by_day={2: {"A": 20000}})
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
        result = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert result.status == "completed_scenario"
        day2 = result.records[1]
        partial = day2.attempts[0].transition
        assert partial.status == "simulated"
        assert partial.reason == "partial_under_declared_constraints"
        assert partial.simulated_quantity == 1000
        lot = day2.book.lots[0]
        assert lot.quantity == 1000
        assert lot.sellable_on > lot.acquired_on  # T+1 lot lock
        rebuy = [o for o in day2.next_intents if o.side == "buy"]
        assert len(rebuy) == 1
        day3 = result.records[2]
        assert sum(x.quantity for x in day3.book.lots) == 10_000


class TestStopSemantics:
    def test_m_without_index_closes_stops_on_first_day(self):
        calendar = _weekdays(202)
        prices = {200: {"A": 1000}}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {calendar[200]: _target({"A": 0.5}, calendar[200])}
        # The evaluation window is the single session after a 200-session
        # warmup calendar, so the stop reason is the missing closes alone.
        start = RiskLedgerCheckpoint.start(
            signal_date=calendar[199],
            initial_cash_fen=CASH_FEN,
            config=_config("M", index_source="synthetic_index"),
        )
        result = run_risk_ledger_loop(
            checkpoint=start,
            calendar=calendar,
            requested_end=calendar[200],
            base_targets=base,
            evidence=evidence,
            config=_config("M"),
        )
        assert result.status == "stopped"
        assert result.stopped_on == calendar[200]
        assert "risk_unknown" in result.stop_reason
        assert "missing_index_close" in result.stop_reason
        assert result.records == ()
        assert result.checkpoint.book.cash_fen == CASH_FEN
        assert result.initial_cash_fen == CASH_FEN

    def test_v_warmup_gap_stops_instead_of_idle_cash(self):
        calendar = _weekdays(5)
        prices = {i: {"A": 1000} for i in range(1, 5)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:4]}
        few_returns = tuple(UnscaledReturn(session, 0.0) for session in calendar[:3])
        result = _run(
            calendar,
            calendar[3],
            base,
            evidence,
            _config("V"),
            unscaled_risk_returns=few_returns,
        )
        assert result.status == "stopped"
        assert result.stopped_on == calendar[1]
        assert "insufficient_warmup" in result.stop_reason
        assert result.records == ()

    def test_missing_mark_for_unheld_target_stops(self):
        calendar = _weekdays(5)
        stripped = LedgerSessionEvidence(
            session=calendar[1],
            contexts=_evidence(
                calendar[1], calendar[2], {"A": 1000}, signal_day=calendar[0]
            ).contexts,
            marks=(),
            corporate_processing_complete=True,
        )
        evidence = {calendar[1]: stripped}
        evidence.update(_evidence_for_calendar(calendar, {i: {"A": 1000} for i in range(2, 5)}))
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:4]}
        result = _run(calendar, calendar[3], base, evidence, _config("C80"))
        assert result.status == "stopped"
        assert result.stopped_on == calendar[1]
        assert result.stop_reason == "mark_missing:A"
        assert result.records == ()
        assert result.checkpoint.book.cash_fen == CASH_FEN

    def test_missing_future_evidence_keeps_completed_prefix(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
        del evidence[calendar[3]]
        result = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert result.status == "stopped"
        assert result.stopped_on == calendar[3]
        assert result.stop_reason == "session_evidence_missing"
        assert [r.session for r in result.records] == [calendar[1], calendar[2]]
        assert result.checkpoint.pending_orders == result.records[-1].next_intents

    def test_base_target_missing_stops_with_prefix(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
        del base[calendar[3]]
        result = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert result.status == "stopped"
        assert result.stopped_on == calendar[3]
        assert result.stop_reason == "base_target_missing"
        assert len(result.records) == 2

    def test_corporate_unknown_stops_through_preflight(self):
        calendar = _weekdays(4)
        prices = {i: {"A": 1000} for i in range(1, 4)}
        evidence = _evidence_for_calendar(calendar, prices)
        evidence[calendar[2]] = _evidence(
            calendar[2], calendar[3], {"A": 1000}, signal_day=calendar[1], corporate=None
        )
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:3]}
        result = _run(calendar, calendar[2], base, evidence, _config("C80"))
        assert result.status == "stopped"
        assert result.stopped_on == calendar[2]
        assert result.stop_reason == "corporate_processing_incomplete_or_unknown"
        # Day 1 committed; day 2's execution was discarded whole.
        assert len(result.records) == 1
        assert result.checkpoint.book.asof_date == calendar[1]


class TestCheckpoints:
    def _rich_world(self):
        calendar = _weekdays(9)
        prices = {i: {"A": 1000} for i in range(1, 9)}
        evidence = _evidence_for_calendar(
            calendar,
            prices,
            fees=FEES_REAL,
            volumes_by_day={2: {"A": 20000}},
            limit_down_by_day={6: ("A",)},
        )
        base = {
            calendar[1]: _target({"A": 0.5}, calendar[1]),
            calendar[2]: _target({"A": 0.5}, calendar[2]),
            calendar[3]: _target({"A": 0.5}, calendar[3]),
            calendar[4]: _target({"A": 0.5}, calendar[4]),
            calendar[5]: _target({}, calendar[5]),  # strategy exit
            calendar[6]: _target({}, calendar[6]),
            calendar[7]: _target({}, calendar[7]),
        }
        return calendar, evidence, base

    def test_resume_at_every_split_day_matches_uninterrupted(self):
        calendar, evidence, base = self._rich_world()
        whole = _run(calendar, calendar[7], base, evidence, _config("C80"))
        assert whole.status == "completed_scenario"
        # The world exercises a partial fill (day 2), a blocked sell (day 5)
        # and nonzero fees on every trade.
        assert whole.records[1].attempts[0].transition.reason == (
            "partial_under_declared_constraints"
        )
        assert whole.records[5].attempts[0].transition.reason == "directional_close_limit"
        assert any(record.modeled_fees_fen > 0 for record in whole.records)
        for split_index in range(1, 7):
            split_day = calendar[split_index]
            prefix = _run(calendar, split_day, base, evidence, _config("C80"))
            assert prefix.status == "completed_scenario"
            resumed = run_risk_ledger_loop(
                checkpoint=prefix.checkpoint,
                calendar=calendar,
                requested_end=calendar[7],
                base_targets=base,
                evidence=evidence,
                config=_config("C80"),
            )
            assert resumed.status == "completed_scenario"
            assert resumed.initial_cash_fen == CASH_FEN == whole.initial_cash_fen
            assert resumed.book == whole.book
            assert resumed.checkpoint == whole.checkpoint
            assert len(resumed.records) == len(whole.records) - split_index
            for replayed, original in zip(
                resumed.records, whole.records[split_index:], strict=True
            ):
                assert replayed.session == original.session
                assert replayed.book == original.book
                assert replayed.next_intents == original.next_intents
                assert replayed.modeled_fees_fen == original.modeled_fees_fen
                assert replayed.marked_equity_fen == original.marked_equity_fen
                assert replayed.decision == original.decision

    def test_pending_buy_and_sell_survive_restart(self):
        calendar, evidence, base = self._rich_world()
        whole = _run(calendar, calendar[7], base, evidence, _config("C80"))
        # Split before the first buy executes.
        before_buy = _run(calendar, calendar[1], base, evidence, _config("C80"))
        assert before_buy.checkpoint.pending_orders == whole.records[0].next_intents
        resumed_buy = run_risk_ledger_loop(
            checkpoint=before_buy.checkpoint,
            calendar=calendar,
            requested_end=calendar[2],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert resumed_buy.records[0].book == whole.records[1].book
        # Split after the blocked day, before the strategy-exit sell executes.
        before_sell = _run(calendar, calendar[5], base, evidence, _config("C80"))
        assert before_sell.checkpoint.pending_orders == whole.records[4].next_intents
        resumed_sell = run_risk_ledger_loop(
            checkpoint=before_sell.checkpoint,
            calendar=calendar,
            requested_end=calendar[6],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert resumed_sell.records[0].book == whole.records[5].book
        assert resumed_sell.initial_cash_fen == CASH_FEN

    def test_checkpoint_rejects_changed_rule_under_same_run_id(self):
        calendar, evidence, base = self._rich_world()
        prefix = _run(calendar, calendar[1], base, evidence, _config("C80"))
        with pytest.raises(ValueError, match="configuration does not match"):
            run_risk_ledger_loop(
                checkpoint=prefix.checkpoint,
                calendar=calendar,
                requested_end=calendar[2],
                base_targets=base,
                evidence=evidence,
                config=_config("C50"),  # same run_id, different risk rule
            )

    def test_checkpoint_rejects_changed_grid_under_same_scenario_id(self):
        calendar, evidence, base = self._rich_world()
        prefix = _run(calendar, calendar[1], base, evidence, _config("C80"))
        retitled = ResearchQuantityRules(
            scenario_id=RULES.scenario_id,  # same declared id ...
            effective_from=RULES.effective_from,
            effective_through=RULES.effective_through,
            buy_minimum=RULES.buy_minimum,
            buy_increment=200,  # ... but different actual content
            sell_minimum=RULES.sell_minimum,
            sell_increment=RULES.sell_increment,
            max_order_quantity=RULES.max_order_quantity,
            full_position_odd_exit=RULES.full_position_odd_exit,
        )
        with pytest.raises(ValueError, match="configuration does not match"):
            run_risk_ledger_loop(
                checkpoint=prefix.checkpoint,
                calendar=calendar,
                requested_end=calendar[2],
                base_targets=base,
                evidence=evidence,
                config=_config("C80", generation_rules=retitled),
            )

    def test_c50_prefix_cannot_continue_as_c80(self):
        # The probe shape: a C80 prefix's pending buy would overbuy a C50 run.
        # Configuration binding must refuse instead of mixing experiments.
        calendar = _weekdays(4)
        prices = {1: {"A": 1000}, 2: {"A": 1000}}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.8}, day) for day in calendar[1:3]}
        prefix = _run(calendar, calendar[1], base, evidence, _config("C80"))
        with pytest.raises(ValueError, match="configuration does not match"):
            run_risk_ledger_loop(
                checkpoint=prefix.checkpoint,
                calendar=calendar,
                requested_end=calendar[2],
                base_targets=base,
                evidence=evidence,
                config=_config("C50"),
            )

    def test_checkpoint_binds_pending_signal_date(self):
        calendar, evidence, base = self._rich_world()
        prefix = _run(calendar, calendar[1], base, evidence, _config("C80"))
        with pytest.raises(ValueError, match="signed on the checkpoint session"):
            RiskLedgerCheckpoint(
                book=ResearchBook(asof_date=calendar[2], cash_fen=CASH_FEN),
                pending_orders=prefix.checkpoint.pending_orders,
                drawdown_state=None,
                attempted_order_ids=(),
                initial_cash_fen=CASH_FEN,
                config=prefix.checkpoint.config,
            )


class TestDeterminism:
    def test_future_evidence_mutation_leaves_earlier_days_unchanged(self):
        calendar = _weekdays(6)

        def build(day4_close: int):
            prices = {1: {"A": 1000}, 2: {"A": 1000}, 3: {"A": 1000}, 4: {"A": day4_close}}
            evidence = _evidence_for_calendar(calendar, prices)
            base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
            return _run(calendar, calendar[4], base, evidence, _config("C80"))

        first = build(1000)
        second = build(1400)
        for before, after in zip(first.records[:3], second.records[:3], strict=True):
            assert before.book == after.book
            assert before.next_intents == after.next_intents
            assert before.risk_target == after.risk_target
        assert first.records[3].marked_equity_fen != second.records[3].marked_equity_fen

    def test_missing_mark_stops_with_last_complete_book(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000} for i in range(1, 6)}
        evidence = _evidence_for_calendar(calendar, prices)
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:5]}
        result = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert result.status == "completed_scenario"
        evidence[calendar[4]] = LedgerSessionEvidence(
            session=calendar[4],
            contexts=evidence[calendar[4]].contexts,
            marks=(),
            corporate_processing_complete=True,
        )
        stopped = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert stopped.status == "stopped"
        assert stopped.stopped_on == calendar[4]
        assert stopped.stop_reason == "raw_mark_unknown:A"
        assert stopped.valid_through == calendar[3]
        assert sum(x.quantity for x in stopped.book.lots) > 0

    def test_d_rule_requires_explicit_initial_state(self):
        calendar = _weekdays(4)
        evidence = _evidence_for_calendar(calendar, {1: {"A": 1000}, 2: {"A": 1000}})
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:3]}
        with pytest.raises(ValueError, match="explicit initial or persisted"):
            _run(calendar, calendar[2], base, evidence, _config("D"))


class TestPostExecutionStopResume:
    def _two_stock_world(self):
        """Nonzero fees; B's mark missing on the second decision day."""
        calendar = _weekdays(6)
        complete_prices = {
            1: {"A": 1000},
            2: {"A": 1000, "B": 500},
            3: {"A": 1000, "B": 500},
            4: {"A": 1000, "B": 500},
        }
        evidence = _evidence_for_calendar(
            calendar, complete_prices, fees=FEES_REAL
        )
        base = {
            calendar[1]: _target({"A": 0.5}, calendar[1]),
            calendar[2]: _target({"A": 0.5, "B": 0.25}, calendar[2]),
            calendar[3]: _target({"A": 0.5, "B": 0.25}, calendar[3]),
            calendar[4]: _target({"A": 0.5, "B": 0.25}, calendar[4]),
        }
        return calendar, evidence, base

    def test_post_execution_stop_checkpoint_equals_clean_prefix_and_resumes(self):
        calendar, evidence, base = self._two_stock_world()
        clean = _run(calendar, calendar[4], base, evidence, _config("C80"))
        assert clean.status == "completed_scenario"
        assert clean.records[1].modeled_fees_fen > 0  # real attempt before stop
        # Gap world: B's mark is missing on day 2 (the day A's buy executes).
        gap_prices = {
            1: {"A": 1000},
            2: {"A": 1000},
            3: {"A": 1000, "B": 500},
            4: {"A": 1000, "B": 500},
        }
        gap_evidence = _evidence_for_calendar(
            calendar, gap_prices, fees=FEES_REAL
        )
        stopped = _run(calendar, calendar[4], base, gap_evidence, _config("C80"))
        assert stopped.status == "stopped"
        assert stopped.stopped_on == calendar[2]
        assert stopped.stop_reason == "mark_missing:B"
        # Field-for-field equality with the last COMPLETE checkpoint: only
        # day 1 committed, so the reference is the one-day prefix run — no
        # stale attempted id, no leaked book, no leaked intents.
        reference = _run(calendar, calendar[1], base, evidence, _config("C80"))
        assert stopped.checkpoint == reference.checkpoint
        assert stopped.checkpoint.pending_orders == (
            reference.records[-1].next_intents
        )
        assert stopped.checkpoint.attempted_order_ids == (
            reference.checkpoint.attempted_order_ids
        )
        # Completing the missing input and resuming reproduces the clean run
        # day by day — including the previously attempted A buy.
        resumed = run_risk_ledger_loop(
            checkpoint=stopped.checkpoint,
            calendar=calendar,
            requested_end=calendar[4],
            base_targets=base,
            evidence=evidence,
            config=_config("C80"),
        )
        assert resumed.status == "completed_scenario"
        assert len(resumed.records) == len(clean.records) - 1
        for replayed, original in zip(resumed.records, clean.records[1:], strict=True):
            assert replayed.book == original.book
            assert replayed.next_intents == original.next_intents
            assert replayed.modeled_fees_fen == original.modeled_fees_fen
            assert replayed.marked_equity_fen == original.marked_equity_fen

    def test_risk_unknown_after_fill_rolls_back_attempted_ids(self):
        calendar = _weekdays(6)
        prices = {i: {"A": 1000} for i in range(1, 5)}
        evidence = _evidence_for_calendar(calendar, prices, fees=FEES_REAL)
        base = {day: _target({"A": 0.5}, day) for day in calendar[1:4]}
        clean = _run(calendar, calendar[3], base, evidence, _config("C80"))
        assert clean.status == "completed_scenario"
        # Same world under V with no unscaled returns: day 2's execution of
        # day-1's buy succeeds, then the risk decision is unknown.
        gap = _run(
            calendar,
            calendar[3],
            base,
            evidence,
            _config("V"),
            unscaled_risk_returns=(),
        )
        assert gap.status == "stopped"
        assert gap.stopped_on == calendar[1]
        assert "risk_unknown" in gap.stop_reason
        # No session committed, so the returned checkpoint must equal the
        # start checkpoint (config included) in every field.
        expected = RiskLedgerCheckpoint.start(
            signal_date=START, initial_cash_fen=CASH_FEN, config=_config("V")
        )
        assert gap.checkpoint == expected

    def test_partial_fill_then_stop_keeps_resume_exact(self):
        calendar = _weekdays(9)
        prices = {i: {"A": 1000} for i in range(1, 8)}
        evidence = _evidence_for_calendar(
            calendar,
            prices,
            fees=FEES_REAL,
            volumes_by_day={2: {"A": 20000}},
            limit_down_by_day={5: ("A",)},
        )
        base = {
            calendar[1]: _target({"A": 0.5}, calendar[1]),
            calendar[2]: _target({"A": 0.5}, calendar[2]),
            calendar[3]: _target({"A": 0.5}, calendar[3]),
            calendar[4]: _target({}, calendar[4]),  # strategy exit intent
            calendar[5]: _target({}, calendar[5]),
            calendar[6]: _target({}, calendar[6]),
            calendar[7]: _target({}, calendar[7]),
        }
        clean = _run(calendar, calendar[7], base, evidence, _config("C80"))
        assert clean.status == "completed_scenario"
        assert clean.records[1].attempts[0].transition.reason == (
            "partial_under_declared_constraints"
        )
        assert clean.records[4].attempts[0].transition.reason == "directional_close_limit"
        for split in (2, 3, 4):
            prefix = _run(calendar, calendar[split], base, evidence, _config("C80"))
            # Stop the split's next day by removing its evidence.
            gap_evidence = dict(evidence)
            gap_evidence.pop(calendar[split + 1])
            stopped = _run(
                calendar, calendar[split + 1], base, gap_evidence, _config("C80")
            )
            assert stopped.status == "stopped"
            assert stopped.checkpoint == prefix.checkpoint
            resumed = run_risk_ledger_loop(
                checkpoint=stopped.checkpoint,
                calendar=calendar,
                requested_end=calendar[7],
                base_targets=base,
                evidence=evidence,
                config=_config("C80"),
            )
            reference = _run(calendar, calendar[7], base, evidence, _config("C80"))
            assert resumed.book == reference.book
            assert resumed.checkpoint == reference.checkpoint
