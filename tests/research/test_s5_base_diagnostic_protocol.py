from dataclasses import FrozenInstanceError, replace

import pytest

from quantlab.research.s5_base_diagnostic_protocol import (
    S5BaseDiagnosticProtocol,
    frozen_s5_base_diagnostic_protocol,
)


def test_frozen_protocol_binds_exact_components_and_one_day_horizon() -> None:
    protocol = frozen_s5_base_diagnostic_protocol()
    assert protocol.strategy_id == "s5b_base_completion_v1"
    assert protocol.signal_horizons == (1, 5, 10, 20)
    assert protocol.membership_required_rate == 1.0
    assert protocol.run_budget == 1
    assert protocol.allow_parameter_rescan is False


def test_one_day_horizon_is_not_a_holding_or_execution_claim() -> None:
    protocol = frozen_s5_base_diagnostic_protocol()
    assert protocol.holding_policy_frozen is False
    assert protocol.horizon_semantics.endswith("not_execution_pnl")
    assert protocol.performance_claim is False
    assert protocol.broker_order_authority is False


def test_protocol_is_immutable_and_factory_is_deterministic() -> None:
    first = frozen_s5_base_diagnostic_protocol()
    second = frozen_s5_base_diagnostic_protocol()
    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.run_budget = 2  # type: ignore[misc]


def test_material_change_changes_fingerprint() -> None:
    protocol = frozen_s5_base_diagnostic_protocol()
    changed = replace(protocol, benchmark_id="000300.SH")
    assert changed.fingerprint != protocol.fingerprint


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"signal_horizons": (5, 1)}, "signal_horizons"),
        ({"signal_horizons": (1, 1)}, "signal_horizons"),
        ({"membership_required_rate": 0.99}, "100%"),
        ({"run_budget": 2}, "run_budget"),
        ({"allow_parameter_rescan": True}, "rescans"),
        ({"holding_policy_frozen": True}, "holding policy"),
        ({"outcome_freeze_required": False}, "outcome freeze"),
        ({"performance_claim": True}, "performance"),
        ({"comparison_ids": ("s5a", "s5a")}, "comparison_ids"),
    ],
)
def test_invalid_protocol_variants_fail_closed(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(frozen_s5_base_diagnostic_protocol(), **changes)


def test_required_blockers_and_negative_result_metrics_are_explicit() -> None:
    protocol = frozen_s5_base_diagnostic_protocol()
    assert protocol.blocker_codes == (
        "blocked_membership_evidence",
        "blocked_eligibility_evidence",
        "blocked_benchmark_evidence",
        "blocked_forward_window_boundary",
    )
    assert "downside_tail" in protocol.signal_metrics
    assert "monthly_and_regime_breakdown" in protocol.signal_metrics


def test_direct_construction_cannot_omit_nonempty_identity() -> None:
    protocol = frozen_s5_base_diagnostic_protocol()
    with pytest.raises(ValueError, match="benchmark_id"):
        replace(protocol, benchmark_id="")


def test_protocol_type_exposes_no_outcome_values() -> None:
    names = set(S5BaseDiagnosticProtocol.__dataclass_fields__)
    assert not names.intersection({"returns", "nav", "cagr", "sharpe", "max_drawdown"})
