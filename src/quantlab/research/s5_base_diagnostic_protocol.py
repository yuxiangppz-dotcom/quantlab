"""Outcome-free frozen protocol for the first S5-B retrospective diagnostic."""

from __future__ import annotations

from dataclasses import dataclass, field

from quantlab.data.models import canonical_payload_fingerprint


@dataclass(frozen=True)
class S5BaseDiagnosticProtocol:
    schema: str
    strategy_id: str
    kernel_schema: str
    materializer_schema: str
    decision_schema: str
    membership_audit_schema: str
    benchmark_id: str
    signal_horizons: tuple[int, ...]
    horizon_semantics: str
    state_metrics: tuple[str, ...]
    signal_metrics: tuple[str, ...]
    comparison_ids: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    period_selection_rule: str
    membership_required_rate: float
    run_budget: int
    allow_parameter_rescan: bool
    holding_policy_frozen: bool
    outcome_freeze_required: bool
    performance_claim: bool
    broker_order_authority: bool
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "schema",
            "strategy_id",
            "kernel_schema",
            "materializer_schema",
            "decision_schema",
            "membership_audit_schema",
            "benchmark_id",
            "horizon_semantics",
            "period_selection_rule",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if (
            not self.signal_horizons
            or tuple(sorted(set(self.signal_horizons))) != self.signal_horizons
            or any(
                type(value) is not int or value <= 0 for value in self.signal_horizons
            )
        ):
            raise ValueError(
                "signal_horizons must be unique increasing positive integers"
            )
        for name in (
            "state_metrics",
            "signal_metrics",
            "comparison_ids",
            "blocker_codes",
        ):
            values = getattr(self, name)
            if (
                not values
                or len(values) != len(set(values))
                or any(not value.strip() for value in values)
            ):
                raise ValueError(f"{name} must contain unique non-empty values")
        if self.membership_required_rate != 1.0:
            raise ValueError("first S5-B diagnostic requires 100% membership coverage")
        if self.run_budget != 1:
            raise ValueError("first S5-B diagnostic run_budget must equal 1")
        if self.allow_parameter_rescan:
            raise ValueError("first S5-B diagnostic cannot allow parameter rescans")
        if self.holding_policy_frozen:
            raise ValueError("signal protocol cannot claim a frozen holding policy")
        if not self.outcome_freeze_required:
            raise ValueError("outcome freeze must be required")
        if self.performance_claim or self.broker_order_authority:
            raise ValueError(
                "diagnostic protocol cannot claim performance or order authority"
            )
        object.__setattr__(
            self, "fingerprint", canonical_payload_fingerprint(_payload(self))
        )


def frozen_s5_base_diagnostic_protocol() -> S5BaseDiagnosticProtocol:
    """Return the single preregistered S5-B v1.3 diagnostic contract."""

    return S5BaseDiagnosticProtocol(
        schema="quantlab_s5b_diagnostic_protocol_v1",
        strategy_id="s5b_base_completion_v1",
        kernel_schema="quantlab_s5b_state_v1",
        materializer_schema="quantlab_s5b_materialization_v1",
        decision_schema="quantlab_s5b_decision_v1",
        membership_audit_schema="quantlab_s5_membership_readiness_v1",
        benchmark_id="000985.SH",
        signal_horizons=(1, 5, 10, 20),
        horizon_semantics="signal_date_close_to_future_session_close_not_execution_pnl",
        state_metrics=(
            "state_counts_and_rates",
            "state_transitions",
            "selected_n",
            "cash_exposure",
        ),
        signal_metrics=(
            "state_forward_return_distributions",
            "confirmed_vs_ready_spread",
            "selected_vs_eligible_nonselected_spread",
            "hit_rate",
            "downside_tail",
            "monthly_and_regime_breakdown",
        ),
        comparison_ids=(
            "s5a_frozen",
            "simple_sector_reversal",
            "simple_industry_trend",
            "alpha158_ridge",
            "broad_market_control",
        ),
        blocker_codes=(
            "blocked_membership_evidence",
            "blocked_eligibility_evidence",
            "blocked_benchmark_evidence",
            "blocked_forward_window_boundary",
        ),
        period_selection_rule="fully_covered_membership_dates_before_outcomes",
        membership_required_rate=1.0,
        run_budget=1,
        allow_parameter_rescan=False,
        holding_policy_frozen=False,
        outcome_freeze_required=True,
        performance_claim=False,
        broker_order_authority=False,
    )


def _payload(protocol: S5BaseDiagnosticProtocol) -> dict[str, object]:
    return {
        name: getattr(protocol, name)
        for name in protocol.__dataclass_fields__
        if name != "fingerprint"
    }
