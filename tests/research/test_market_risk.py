"""Boundary tests for the seven frozen market risk cap rules."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.research.market_risk import (
    APPROVED_EQUITY_PROXY_INDEX_ID,
    RULE_CONFIG_FINGERPRINTS,
    RULE_IDS,
    STATUS_OK,
    STATUS_UNKNOWN,
    IndexClose,
    RiskDecision,
    RiskInputs,
    RiskState,
    StrategyNav,
    UnscaledReturn,
    evaluate_market_risk_rule,
)


def _weekdays(count: int, start: date = date(2024, 1, 1)) -> tuple[date, ...]:
    sessions: list[date] = []
    day = start
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    return tuple(sessions)


def _flat_closes(sessions: tuple[date, ...], value: Decimal) -> tuple[IndexClose, ...]:
    return tuple(IndexClose(session=s, close=value) for s in sessions)


def _flat_returns(sessions: tuple[date, ...], value: float) -> tuple[UnscaledReturn, ...]:
    return tuple(UnscaledReturn(session=s, value=value) for s in sessions)


class TestRiskInputsContract:
    def test_rejects_duplicate_sessions(self):
        sessions = _weekdays(4)
        with pytest.raises(ValueError, match="unique and strictly increasing"):
            RiskInputs(decision_date=sessions[-1], sessions=(*sessions, sessions[-1]))

    def test_rejects_unsorted_sessions(self):
        sessions = _weekdays(4)
        with pytest.raises(ValueError, match="unique and strictly increasing"):
            RiskInputs(decision_date=sessions[0], sessions=tuple(reversed(sessions)))

    def test_rejects_decision_date_outside_calendar(self):
        sessions = _weekdays(5)
        with pytest.raises(ValueError, match="decision_date must be one of"):
            RiskInputs(decision_date=sessions[-1] + timedelta(days=3), sessions=sessions)

    def test_rejects_future_observation(self):
        sessions = _weekdays(5)
        future = UnscaledReturn(session=sessions[-1] + timedelta(days=1), value=0.01)
        with pytest.raises(ValueError, match="future session"):
            RiskInputs(
                decision_date=sessions[-1],
                sessions=sessions,
                unscaled_risk_returns=(future,),
                return_source="test",
            )

    def test_rejects_duplicate_observation_sessions(self):
        sessions = _weekdays(5)
        item = IndexClose(session=sessions[0], close=Decimal("100"))
        with pytest.raises(ValueError, match="duplicate session"):
            RiskInputs(
                decision_date=sessions[-1],
                sessions=sessions,
                index_closes=(item, item),
                index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
                index_source="test",
            )

    def test_rejects_nav_with_unhandled_external_flow(self):
        sessions = _weekdays(5)
        nav = StrategyNav(
            as_of=sessions[-1], nav=Decimal("100"), unhandled_external_flow=True
        )
        with pytest.raises(ValueError, match="unhandled external flows"):
            RiskInputs(
                decision_date=sessions[-1], sessions=sessions, strategy_nav=nav
            )

    def test_rejects_nav_as_of_mismatch(self):
        sessions = _weekdays(5)
        nav = StrategyNav(as_of=sessions[0], nav=Decimal("100"))
        with pytest.raises(ValueError, match="as of the decision_date"):
            RiskInputs(
                decision_date=sessions[-1],
                sessions=sessions,
                strategy_nav=nav,
                nav_source="test",
            )

    def test_requires_sources_and_index_id_with_sections(self):
        sessions = _weekdays(5)
        with pytest.raises(ValueError, match="return_source is required"):
            RiskInputs(
                decision_date=sessions[-1],
                sessions=sessions,
                unscaled_risk_returns=_flat_returns(sessions, 0.0),
            )
        with pytest.raises(ValueError, match="index_id is required"):
            RiskInputs(
                decision_date=sessions[-1],
                sessions=sessions,
                index_closes=_flat_closes(sessions, Decimal("100")),
                index_source="test",
            )

    def test_rejects_nonpositive_nav(self):
        with pytest.raises(ValueError, match="positive Decimal"):
            StrategyNav(as_of=date(2024, 1, 2), nav=Decimal("0"))

    def test_rejects_invalid_state_coefficient(self):
        with pytest.raises(ValueError, match="coefficient"):
            RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.7"))


class TestConstantRules:
    def test_constant_caps(self):
        sessions = _weekdays(6)
        inputs = RiskInputs(decision_date=sessions[3], sessions=sessions)
        c80 = evaluate_market_risk_rule("C80", inputs)
        c50 = evaluate_market_risk_rule("C50", inputs)
        assert c80.status == STATUS_OK and c80.cap == Decimal("0.8")
        assert c50.status == STATUS_OK and c50.cap == Decimal("0.5")
        assert c80.next_execution_date == sessions[4]

    def test_last_session_reports_no_future_session(self):
        sessions = _weekdays(5)
        inputs = RiskInputs(decision_date=sessions[-1], sessions=sessions)
        decision = evaluate_market_risk_rule("C80", inputs)
        assert decision.status == STATUS_OK
        assert decision.next_execution_date is None
        assert "no_future_session" in decision.reasons


class TestVolRule:
    def _inputs(self, sessions, returns, decision_index=None):
        idx = len(sessions) - 1 if decision_index is None else decision_index
        return RiskInputs(
            decision_date=sessions[idx],
            sessions=sessions,
            unscaled_risk_returns=returns,
            return_source="synthetic_unscaled",
        )

    def test_insufficient_warmup_below_60_sessions(self):
        sessions = _weekdays(59)
        decision = evaluate_market_risk_rule("V", self._inputs(sessions, ()))
        assert decision.status == STATUS_UNKNOWN
        assert "insufficient_warmup" in decision.reasons

    def test_missing_return_inside_window_is_unknown(self):
        sessions = _weekdays(60)
        returns = _flat_returns(sessions[:-1], 0.01)
        decision = evaluate_market_risk_rule("V", self._inputs(sessions, returns))
        assert decision.status == STATUS_UNKNOWN
        assert "missing_return" in decision.reasons
        assert decision.cap is None

    def test_nonfinite_return_is_unknown_not_a_cap(self):
        sessions = _weekdays(60)
        returns = _flat_returns(sessions[:-1], 0.01) + (
            UnscaledReturn(session=sessions[-1], value=float("nan")),
        )
        decision = evaluate_market_risk_rule("V", self._inputs(sessions, returns))
        assert decision.status == STATUS_UNKNOWN
        assert "invalid_return" in decision.reasons

    def test_zero_volatility_caps_at_maximum(self):
        sessions = _weekdays(60)
        decision = evaluate_market_risk_rule(
            "V", self._inputs(sessions, _flat_returns(sessions, 0.01))
        )
        assert decision.status == STATUS_OK
        assert decision.cap == Decimal("0.8")
        assert decision.vol_estimate == 0.0

    def test_low_volatility_still_capped_at_maximum(self):
        sessions = _weekdays(60)
        values = (0.01, -0.01) * 30
        returns = tuple(
            UnscaledReturn(session=s, value=v) for s, v in zip(sessions, values, strict=True)
        )
        decision = evaluate_market_risk_rule("V", self._inputs(sessions, returns))
        assert decision.status == STATUS_OK
        assert decision.cap == Decimal("0.8")

    def test_high_volatility_scales_below_cap(self):
        sessions = _weekdays(60)
        values = (0.02, -0.02) * 30
        returns = tuple(
            UnscaledReturn(session=s, value=v) for s, v in zip(sessions, values, strict=True)
        )
        decision = evaluate_market_risk_rule("V", self._inputs(sessions, returns))
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        expected = min(0.8, 0.15 / (math.sqrt(variance) * math.sqrt(252)))
        assert decision.status == STATUS_OK
        assert float(decision.cap) == pytest.approx(expected, rel=1e-12)
        assert decision.cap < Decimal("0.8")


class TestMaRegimeRule:
    def _inputs(self, sessions, closes, index_id=APPROVED_EQUITY_PROXY_INDEX_ID):
        return RiskInputs(
            decision_date=sessions[-1],
            sessions=sessions,
            index_closes=closes,
            index_id=index_id,
            index_source="synthetic_index",
        )

    def test_insufficient_warmup_below_200_sessions(self):
        sessions = _weekdays(199)
        decision = evaluate_market_risk_rule(
            "M", self._inputs(sessions, _flat_closes(sessions, Decimal("100")))
        )
        assert decision.status == STATUS_UNKNOWN
        assert "insufficient_warmup" in decision.reasons

    def test_missing_close_inside_window_is_unknown(self):
        sessions = _weekdays(200)
        closes = _flat_closes(sessions[:-1], Decimal("100"))
        decision = evaluate_market_risk_rule("M", self._inputs(sessions, closes))
        assert decision.status == STATUS_UNKNOWN
        assert "missing_index_close" in decision.reasons

    def test_absent_index_data_is_unknown_without_id(self):
        sessions = _weekdays(200)
        inputs = RiskInputs(decision_date=sessions[-1], sessions=sessions)
        decision = evaluate_market_risk_rule("M", inputs)
        assert decision.status == STATUS_UNKNOWN
        assert "missing_index_close" in decision.reasons

    def test_nonpositive_close_is_unknown(self):
        sessions = _weekdays(200)
        closes = _flat_closes(sessions[:-1], Decimal("100")) + (
            IndexClose(session=sessions[-1], close=Decimal("0")),
        )
        decision = evaluate_market_risk_rule("M", self._inputs(sessions, closes))
        assert decision.status == STATUS_UNKNOWN
        assert "invalid_index_close" in decision.reasons

    def test_close_exactly_at_mean_means_off(self):
        sessions = _weekdays(200)
        decision = evaluate_market_risk_rule(
            "M", self._inputs(sessions, _flat_closes(sessions, Decimal("100")))
        )
        assert decision.status == STATUS_OK
        assert decision.ma_above is False
        assert decision.cap == Decimal("0")

    def test_close_above_and_below_mean(self):
        sessions = _weekdays(200)
        base = _flat_closes(sessions[:-1], Decimal("100"))
        up = base + (IndexClose(session=sessions[-1], close=Decimal("101")),)
        down = base + (IndexClose(session=sessions[-1], close=Decimal("99")),)
        assert evaluate_market_risk_rule("M", self._inputs(sessions, up)).cap == (
            Decimal("0.8")
        )
        assert evaluate_market_risk_rule("M", self._inputs(sessions, down)).cap == (
            Decimal("0")
        )

    def test_weekend_gap_between_sessions_is_not_a_gap(self):
        sessions = _weekdays(200, start=date(2024, 6, 3))
        decision = evaluate_market_risk_rule(
            "M", self._inputs(sessions, _flat_closes(sessions, Decimal("100")))
        )
        assert decision.status == STATUS_OK

    def test_alternate_index_id_rejects(self):
        sessions = _weekdays(200)
        with pytest.raises(ValueError, match="accepts only index"):
            evaluate_market_risk_rule(
                "M",
                self._inputs(sessions, _flat_closes(sessions, Decimal("100")), "000300"),
            )


class TestDrawdownGovernor:
    def _inputs(self, sessions, nav_value, state=None, index=None):
        idx = len(sessions) - 1 if index is None else index
        return RiskInputs(
            decision_date=sessions[idx],
            sessions=sessions,
            strategy_nav=StrategyNav(as_of=sessions[idx], nav=nav_value),
            drawdown_state=state,
            nav_source="synthetic_nav",
        )

    def test_missing_nav_is_unknown_and_state_passes_through(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.5"))
        inputs = RiskInputs(
            decision_date=sessions[-1], sessions=sessions, drawdown_state=state
        )
        decision = evaluate_market_risk_rule("D", inputs)
        assert decision.status == STATUS_UNKNOWN
        assert "nav_unavailable" in decision.reasons
        assert decision.drawdown_state is state

    def test_initial_decision_has_full_cap(self):
        sessions = _weekdays(5)
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("100"))
        )
        assert decision.status == STATUS_OK
        assert decision.cap == Decimal("0.8")
        assert decision.coefficient == Decimal("1")
        assert decision.drawdown == Decimal("0")

    def test_drawdown_exactly_ten_percent_enters_tier1(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("1"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("90"), state)
        )
        assert decision.cap == Decimal("0.4")
        assert decision.coefficient == Decimal("0.5")
        assert decision.drawdown == Decimal("0.1")

    def test_just_below_ten_percent_stays_normal(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("1"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("90.01"), state)
        )
        assert decision.coefficient == Decimal("1")
        assert decision.cap == Decimal("0.8")

    def test_drawdown_exactly_fifteen_percent_enters_tier2(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("1"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("85"), state)
        )
        assert decision.coefficient == Decimal("0.25")
        assert decision.cap == Decimal("0.2")

    def test_deterioration_jumps_directly_to_tier2(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("1"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("80"), state)
        )
        assert decision.coefficient == Decimal("0.25")

    def test_tier2_recovers_inside_ten_to_twelve_percent_band(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.25"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("89"), state)
        )
        assert decision.drawdown == Decimal("0.11")
        assert decision.coefficient == Decimal("0.5")

    def test_tier2_stays_severe_between_twelve_and_fifteen_percent(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.25"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("87"), state)
        )
        assert decision.drawdown == Decimal("0.13")
        assert decision.coefficient == Decimal("0.25")

    def test_severe_recovery_boundary_exactly_twelve_percent(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.25"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("88"), state)
        )
        assert decision.drawdown == Decimal("0.12")
        assert decision.coefficient == Decimal("0.5")

    def test_recovery_is_staged_at_most_one_tier(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.25"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("93"), state)
        )
        assert decision.drawdown == Decimal("0.07")
        assert decision.coefficient == Decimal("0.5")

    def test_tier1_recovery_boundary_exactly_eight_percent(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.5"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("92"), state)
        )
        assert decision.drawdown == Decimal("0.08")
        assert decision.coefficient == Decimal("1")

    def test_tier1_holds_between_recovery_and_deterioration(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.5"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("91"), state)
        )
        assert decision.drawdown == Decimal("0.09")
        assert decision.coefficient == Decimal("0.5")

    def test_new_high_updates_hwm_and_recovers(self):
        sessions = _weekdays(5)
        state = RiskState(high_water_mark=Decimal("100"), coefficient=Decimal("0.5"))
        decision = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("120"), state)
        )
        assert decision.high_water_mark == Decimal("120")
        assert decision.drawdown == Decimal("0")
        assert decision.coefficient == Decimal("1")

    def test_hwm_survives_simulated_restart(self):
        sessions = _weekdays(6)
        first = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("100"), index=4)
        )
        persisted = first.drawdown_state
        assert persisted is not None
        second = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("70"), persisted, index=5)
        )
        assert second.high_water_mark == Decimal("100")
        assert second.coefficient == Decimal("0.25")

    def test_hwm_survives_year_boundary(self):
        sessions = _weekdays(10, start=date(2024, 12, 23))
        december = evaluate_market_risk_rule(
            "D", self._inputs(sessions, Decimal("100"), index=6)
        )
        january = evaluate_market_risk_rule(
            "D",
            self._inputs(sessions, Decimal("84"), december.drawdown_state, index=9),
        )
        assert december.decision_date.year == 2024
        assert january.decision_date.year == 2025
        assert january.high_water_mark == Decimal("100")
        assert january.coefficient == Decimal("0.25")


class TestCompositeRules:
    def _vm_inputs(self, sessions, returns, closes):
        return RiskInputs(
            decision_date=sessions[-1],
            sessions=sessions,
            unscaled_risk_returns=returns,
            return_source="synthetic_unscaled",
            index_closes=closes,
            index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
            index_source="synthetic_index",
        )

    def test_vm_takes_component_minimum(self):
        sessions = _weekdays(200)
        inputs = self._vm_inputs(
            sessions, _flat_returns(sessions[-60:], 0.0), _flat_closes(sessions, Decimal("100"))
        )
        decision = evaluate_market_risk_rule("VM", inputs)
        assert decision.status == STATUS_OK
        assert decision.cap == Decimal("0")

    def test_unknown_component_makes_composite_unknown(self):
        sessions = _weekdays(200)
        inputs = self._vm_inputs(
            sessions,
            _flat_returns(sessions[-60:], 0.0),
            _flat_closes(sessions[:-1], Decimal("100")),
        )
        decision = evaluate_market_risk_rule("VM", inputs)
        assert decision.status == STATUS_UNKNOWN
        assert any("M:missing_index_close" in r for r in decision.reasons)
        assert decision.cap is None

    def test_vmd_reports_d_state_even_when_v_unknown(self):
        sessions = _weekdays(200)
        inputs = RiskInputs(
            decision_date=sessions[-1],
            sessions=sessions,
            index_closes=_flat_closes(sessions, Decimal("100")),
            index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
            index_source="synthetic_index",
            strategy_nav=StrategyNav(as_of=sessions[-1], nav=Decimal("85")),
            drawdown_state=RiskState(
                high_water_mark=Decimal("100"), coefficient=Decimal("1")
            ),
            nav_source="synthetic_nav",
        )
        decision = evaluate_market_risk_rule("VMD", inputs)
        assert decision.status == STATUS_UNKNOWN
        assert decision.drawdown_state is not None
        assert decision.drawdown_state.coefficient == Decimal("0.25")
        assert decision.drawdown_state.high_water_mark == Decimal("100")

    def test_vmd_minimum_of_three(self):
        sessions = _weekdays(200)
        inputs = RiskInputs(
            decision_date=sessions[-1],
            sessions=sessions,
            unscaled_risk_returns=_flat_returns(sessions[-60:], 0.0),
            return_source="synthetic_unscaled",
            index_closes=_flat_closes(sessions[:-1], Decimal("100"))
            + (IndexClose(session=sessions[-1], close=Decimal("101")),),
            index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
            index_source="synthetic_index",
            strategy_nav=StrategyNav(as_of=sessions[-1], nav=Decimal("90")),
            drawdown_state=RiskState(
                high_water_mark=Decimal("100"), coefficient=Decimal("1")
            ),
            nav_source="synthetic_nav",
        )
        decision = evaluate_market_risk_rule("VMD", inputs)
        assert decision.status == STATUS_OK
        assert decision.cap == Decimal("0.4")


class TestDeterminismAndRegistry:
    def test_future_mutations_do_not_change_past_decisions(self):
        sessions = _weekdays(260)
        early = sessions[249]
        closes = _flat_closes(sessions[:250], Decimal("100"))
        shorter = RiskInputs(
            decision_date=early,
            sessions=sessions[:252],
            index_closes=closes,
            index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
            index_source="synthetic_index",
        )
        longer = RiskInputs(
            decision_date=early,
            sessions=sessions,
            index_closes=closes,
            index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
            index_source="synthetic_index",
        )
        first = evaluate_market_risk_rule("M", shorter)
        second = evaluate_market_risk_rule("M", longer)
        assert first.cap == second.cap
        assert first.ma_above == second.ma_above
        assert first.ma_mean == second.ma_mean
        assert first.next_execution_date == second.next_execution_date
        # Observations after the decision date are refused outright, so future
        # values can never leak into a past decision.
        future_close = IndexClose(session=early + timedelta(days=1), close=Decimal("137"))
        with pytest.raises(ValueError, match="future session"):
            RiskInputs(
                decision_date=early,
                sessions=sessions,
                index_closes=closes + (future_close,),
                index_id=APPROVED_EQUITY_PROXY_INDEX_ID,
                index_source="synthetic_index",
            )

    def test_all_seven_rules_evaluate(self):
        sessions = _weekdays(200)
        inputs = RiskInputs(decision_date=sessions[-1], sessions=sessions)
        for rule_id in RULE_IDS:
            decision = evaluate_market_risk_rule(rule_id, inputs)
            assert isinstance(decision, RiskDecision)
            assert decision.status in (STATUS_OK, STATUS_UNKNOWN)

    def test_unknown_rule_id_rejects(self):
        sessions = _weekdays(5)
        inputs = RiskInputs(decision_date=sessions[-1], sessions=sessions)
        with pytest.raises(ValueError, match="unknown market risk rule"):
            evaluate_market_risk_rule("X", inputs)

    def test_fingerprints_are_distinct_and_stable(self):
        assert len(set(RULE_CONFIG_FINGERPRINTS.values())) == len(RULE_IDS)
        sessions = _weekdays(200)
        inputs = RiskInputs(decision_date=sessions[-1], sessions=sessions)
        for rule_id in RULE_IDS:
            decision = evaluate_market_risk_rule(rule_id, inputs)
            assert decision.config_fingerprint == RULE_CONFIG_FINGERPRINTS[rule_id]

    def test_decision_forbids_inconsistent_status_and_cap(self):
        sessions = _weekdays(5)
        with pytest.raises(ValueError, match="ok decision must carry a cap"):
            RiskDecision(
                rule_id="C80",
                decision_date=sessions[-1],
                next_execution_date=None,
                status=STATUS_OK,
                cap=None,
                reasons=(),
                config_fingerprint=RULE_CONFIG_FINGERPRINTS["C80"],
            )
        with pytest.raises(ValueError, match="unknown decision must not carry a cap"):
            RiskDecision(
                rule_id="C80",
                decision_date=sessions[-1],
                next_execution_date=None,
                status=STATUS_UNKNOWN,
                cap=Decimal("0.5"),
                reasons=("missing_return",),
                config_fingerprint=RULE_CONFIG_FINGERPRINTS["C80"],
            )
