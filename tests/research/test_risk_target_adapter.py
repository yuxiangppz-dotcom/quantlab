"""Contract tests for translating risk caps onto target portfolios."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.market_risk import (
    RULE_CONFIG_FINGERPRINTS,
    STATUS_OK,
    STATUS_UNKNOWN,
    RiskDecision,
    RiskInputs,
    RiskState,
    StrategyNav,
    evaluate_market_risk_rule,
)
from quantlab.research.risk_target_adapter import (
    RESULT_NOT_READY,
    RESULT_READY,
    RiskTargetResult,
    apply_risk_cap,
)

SERIES = "nav_series_a"


def _weekdays(count: int, start: date = date(2024, 1, 1)) -> tuple[date, ...]:
    sessions: list[date] = []
    day = start
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    return tuple(sessions)


def _portfolio(weights: dict[str, float], as_of: date) -> TargetPortfolio:
    positions = tuple(
        TargetWeight(instrument_id=name, target_weight=weight)
        for name, weight in weights.items()
    )
    cash = 1.0 - sum(weights.values())
    return TargetPortfolio(as_of=as_of, positions=positions, cash_weight=cash)


def _cap_decision(cap: Decimal, decision_date: date) -> RiskDecision:
    return RiskDecision(
        rule_id="D",
        decision_date=decision_date,
        next_execution_date=decision_date + timedelta(days=1),
        status=STATUS_OK,
        cap=cap,
        reasons=(),
        config_fingerprint=RULE_CONFIG_FINGERPRINTS["D"],
    )


def _unknown_decision(decision_date: date) -> RiskDecision:
    return RiskDecision(
        rule_id="V",
        decision_date=decision_date,
        next_execution_date=decision_date + timedelta(days=1),
        status=STATUS_UNKNOWN,
        cap=None,
        reasons=("insufficient_warmup",),
        config_fingerprint=RULE_CONFIG_FINGERPRINTS["V"],
    )


class TestNotReadyContract:
    def test_unknown_decision_is_detectably_not_ready(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"000001.SZ": 0.5, "600000.SH": 0.3}, sessions[-1])
        result = apply_risk_cap(portfolio, _unknown_decision(sessions[-1]))
        assert result.status == RESULT_NOT_READY
        assert result.target is None
        assert "insufficient_warmup" in result.reason
        assert result.applied_cap is None and result.scale_factor is None

    def test_not_ready_is_never_an_all_cash_target(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"000001.SZ": 0.6, "600000.SH": 0.3}, sessions[-1])
        result = apply_risk_cap(portfolio, _unknown_decision(sessions[-1]))
        assert result.status == RESULT_NOT_READY
        assert result.target is None

    def test_not_ready_result_cannot_carry_a_target(self):
        with pytest.raises(ValueError, match="must not carry a target"):
            RiskTargetResult(
                RESULT_NOT_READY,
                _portfolio({"000001.SZ": 1.0}, date(2024, 1, 2)),
                "reason",
                None,
                None,
            )

    def test_ready_result_requires_a_target_and_signal_date(self):
        with pytest.raises(ValueError, match="must carry a target"):
            RiskTargetResult(RESULT_READY, None, None, 0.5, 1.0)
        with pytest.raises(ValueError, match="record signal_as_of"):
            RiskTargetResult(
                RESULT_READY, _portfolio({}, date(2024, 1, 2)), None, 0.5, 1.0, None
            )

    def test_unknown_from_real_rule_propagates(self):
        sessions = _weekdays(30)
        inputs = RiskInputs(decision_date=sessions[-1], sessions=sessions)
        decision = evaluate_market_risk_rule("VMD", inputs)
        assert decision.status == STATUS_UNKNOWN
        portfolio = _portfolio({"000001.SZ": 0.8}, sessions[-1])
        result = apply_risk_cap(portfolio, decision)
        assert result.status == RESULT_NOT_READY
        assert result.target is None
        assert result.reason


class TestTemporalContract:
    def test_newer_target_with_older_decision_rejects(self):
        sessions = _weekdays(5)
        decision = _cap_decision(Decimal("0.5"), sessions[2])
        portfolio = _portfolio({"A": 0.6, "B": 0.3}, sessions[4])
        with pytest.raises(ValueError, match="postdates the decision"):
            apply_risk_cap(portfolio, decision)

    def test_same_day_translation_keeps_date(self):
        sessions = _weekdays(5)
        decision = _cap_decision(Decimal("0.5"), sessions[2])
        portfolio = _portfolio({"A": 0.6, "B": 0.3}, sessions[2])
        result = apply_risk_cap(portfolio, decision)
        assert result.status == RESULT_READY
        assert result.target is not None
        assert result.target.as_of == sessions[2]
        assert result.signal_as_of == sessions[2]

    def test_older_target_projects_onto_decision_date(self):
        sessions = _weekdays(5)
        decision = _cap_decision(Decimal("0.5"), sessions[4])
        portfolio = _portfolio({"A": 0.3, "B": 0.3, "C": 0.3}, sessions[2])
        result = apply_risk_cap(portfolio, decision)
        assert result.status == RESULT_READY
        assert result.target is not None
        assert result.target.as_of == sessions[4]
        assert result.signal_as_of == sessions[2]
        assert result.target.gross_exposure == pytest.approx(0.5, abs=1e-9)
        assert [p.instrument_id for p in result.target.positions] == ["A", "B", "C"]

    def test_projection_with_cap_zero_is_all_cash_at_decision_date(self):
        sessions = _weekdays(5)
        decision = _cap_decision(Decimal("0"), sessions[4])
        portfolio = _portfolio({"A": 0.5, "B": 0.4}, sessions[2])
        result = apply_risk_cap(portfolio, decision)
        assert result.status == RESULT_READY
        assert result.target is not None
        assert result.target.as_of == sessions[4]
        assert result.target.positions == ()
        assert result.target.cash_weight == 1.0
        assert result.signal_as_of == sessions[2]

    def test_projection_of_low_exposure_relabels_without_upsizing(self):
        sessions = _weekdays(5)
        decision = _cap_decision(Decimal("0.8"), sessions[4])
        portfolio = _portfolio({"A": 0.4, "B": 0.2}, sessions[2])
        result = apply_risk_cap(portfolio, decision)
        assert result.status == RESULT_READY
        assert result.scale_factor == 1.0
        assert result.target is not None
        assert result.target.as_of == sessions[4]
        assert result.target.gross_exposure == pytest.approx(0.6)


class TestTargetBoundaryValidation:
    def test_negative_weight_rejects_even_with_cap_zero(self):
        sessions = _weekdays(5)
        portfolio = TargetPortfolio(
            as_of=sessions[-1],
            positions=(TargetWeight(instrument_id="A", target_weight=-0.5),),
            cash_weight=1.5,
        )
        with pytest.raises(ValueError, match="long-only"):
            apply_risk_cap(portfolio, _cap_decision(Decimal("0"), sessions[-1]))
        with pytest.raises(ValueError, match="long-only"):
            apply_risk_cap(portfolio, _cap_decision(Decimal("0.4"), sessions[-1]))

    def test_overweight_and_negative_cash_reject(self):
        sessions = _weekdays(5)
        overweight = _portfolio({"A": 1.5}, sessions[-1])
        with pytest.raises(ValueError):
            apply_risk_cap(overweight, _cap_decision(Decimal("0.8"), sessions[-1]))
        negative_cash = _portfolio({"A": 0.6, "B": 0.6}, sessions[-1])
        with pytest.raises(ValueError, match="negative"):
            apply_risk_cap(negative_cash, _cap_decision(Decimal("0.5"), sessions[-1]))


class TestCapTranslation:
    def test_cap_zero_yields_explicit_all_cash_target(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"000001.SZ": 0.5, "600000.SH": 0.4}, sessions[-1])
        result = apply_risk_cap(portfolio, _cap_decision(Decimal("0"), sessions[-1]))
        assert result.status == RESULT_READY
        assert result.target is not None
        assert result.target.positions == ()
        assert result.target.cash_weight == 1.0
        assert result.applied_cap == 0.0

    def test_proportional_scale_down_conserves_weights_and_cash(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"A": 0.3, "B": 0.3, "C": 0.3}, sessions[-1])
        result = apply_risk_cap(portfolio, _cap_decision(Decimal("0.5"), sessions[-1]))
        assert result.status == RESULT_READY
        target = result.target
        assert target is not None
        assert [p.instrument_id for p in target.positions] == ["A", "B", "C"]
        assert target.gross_exposure == pytest.approx(0.5, abs=1e-9)
        assert target.gross_exposure <= 0.5 + 1e-9
        assert target.cash_weight == pytest.approx(0.5, abs=1e-9)
        total = target.gross_exposure + target.cash_weight
        assert total == pytest.approx(1.0, abs=1e-9)
        ratios = [
            later.target_weight / before.target_weight
            for later, before in zip(target.positions, portfolio.positions, strict=True)
        ]
        assert all(ratio == pytest.approx(ratios[0]) for ratio in ratios)

    def test_no_leverage_and_no_negative_weights(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"A": 0.45, "B": 0.45}, sessions[-1])
        result = apply_risk_cap(portfolio, _cap_decision(Decimal("0.2"), sessions[-1]))
        target = result.target
        assert target is not None
        assert all(p.target_weight >= 0 for p in target.positions)
        assert target.gross_exposure <= 0.2 + 1e-9
        assert target.cash_weight >= 0

    def test_cap_above_gross_never_upsizes(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"A": 0.4, "B": 0.2}, sessions[-1])
        result = apply_risk_cap(portfolio, _cap_decision(Decimal("0.8"), sessions[-1]))
        assert result.status == RESULT_READY
        assert result.scale_factor == 1.0
        assert result.target is portfolio
        assert result.target.gross_exposure == pytest.approx(0.6)

    def test_all_cash_target_passes_through(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({}, sessions[-1])
        result = apply_risk_cap(portfolio, _cap_decision(Decimal("0.2"), sessions[-1]))
        assert result.status == RESULT_READY
        assert result.target is not None
        assert result.target.positions == ()
        assert result.target.cash_weight == 1.0

    def test_input_portfolio_is_untouched(self):
        sessions = _weekdays(5)
        portfolio = _portfolio({"A": 0.3, "B": 0.3, "C": 0.3}, sessions[-1])
        before = tuple(portfolio.positions)
        apply_risk_cap(portfolio, _cap_decision(Decimal("0.5"), sessions[-1]))
        assert portfolio.positions == before
        assert portfolio.cash_weight == pytest.approx(0.1)


class TestIntentTargetExecutionDistinction:
    def test_unsellable_position_is_not_de_risked(self):
        # Synthetic illustration, not historical performance. Intent: a 15%
        # drawdown puts D at coefficient 0.25, cap 0.2. Target: three 0.3
        # weights scale down to 0.2 gross. Execution: C is limit-down and
        # unsellable, so realized gross stays at 0.3 — above the cap — while
        # the risk-compliant target holds 0.2. The adapter only produces the
        # intention; actual holdings live in the execution ledger's authority.
        sessions = _weekdays(5)
        inputs = RiskInputs(
            decision_date=sessions[-1],
            sessions=sessions,
            strategy_nav=StrategyNav(as_of=sessions[-1], nav=Decimal("85")),
            drawdown_state=RiskState.initial(Decimal("100"), SERIES),
            nav_source="synthetic_nav",
            nav_series_id=SERIES,
        )
        decision = evaluate_market_risk_rule("D", inputs)
        assert decision.cap == Decimal("0.2")

        portfolio = _portfolio({"A": 0.3, "B": 0.3, "C": 0.3}, sessions[-1])
        result = apply_risk_cap(portfolio, decision)
        assert result.status == RESULT_READY
        assert result.target is not None
        assert result.target.gross_exposure <= 0.2 + 1e-9

        actual_holdings = {"A": 0.0, "B": 0.0, "C": 0.3}  # C limit-down, unsellable
        realized_gross = sum(actual_holdings.values())
        assert realized_gross > float(decision.cap)
        # The adapter result carries target intent only: no order, fill or
        # holding mutation surface exists to close that gap here.
        assert set(result.__dataclass_fields__) == {
            "status",
            "target",
            "reason",
            "applied_cap",
            "scale_factor",
            "signal_as_of",
        }
