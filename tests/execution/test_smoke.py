"""The synthetic order-path smoke suite: every audited flag must hold."""

import pytest

from quantlab.execution.smoke import run_order_path_smoke

REQUIRED_TRUE_FLAGS = (
    "synthetic",
    "non_trading",
    "same_state_batch_committed",
    "sell_share_reservation",
    "typed_quote_lineage",
    "stale_batch_rejected",
    "day_submission_date_bound",
    "authority_lineage_bound",
    "production_submission_path_reachable",
    "gating_dimensions_fail_closed",
    "account_aware_order_planning",
    "order_plan_deterministic",
    "buy_limit_from_order_price_evidence",
    "buys_funded_from_available_cash_only",
    "plan_to_order_lineage",
    "transactional_submission_committed",
    "omitted_held_name_exits",
    "non_conforming_delta_blocks",
    "aggregate_cash_contention_blocked",
    "atomic_cash_reservation",
    "partial_fill_drawdown",
    "cancel_release",
    "multi_partial_fee_reconciliation",
    "full_fill_release",
    "batch_rollback_preserves_state",
    "share_contention_blocked",
    "atomic_share_reservation",
    "wrong_trade_date_rejected",
    "weekend_t_plus_one_exact",
    "holiday_t_plus_one_exact",
    "missing_next_session_fail_closed",
    "stale_assessment_rejected",
    "event_replay_matches_fresh_run",
)
REQUIRED_FALSE_FLAGS = (
    "provider_called",
    "canonical_data_written",
    "order_submission_attempted",
    "fill_claimed",
    "external_broker_submission",
)


def test_smoke_gate_matrix_keeps_unknowns_fail_closed() -> None:
    evidence = run_order_path_smoke()
    matrix = evidence["submission_gate_matrix"]
    assert matrix["fillability_unknown"] == "validated"
    assert matrix["market_accessibility_unknown"] == "unknown"
    assert matrix["fee_determinability_unknown"] == "unknown"
    assert matrix["order_admissibility_unknown"] == "unknown"


def test_smoke_evidence_supports_every_v02_readiness_check() -> None:
    evidence = run_order_path_smoke()
    missing = [flag for flag in REQUIRED_TRUE_FLAGS if flag not in evidence]
    assert not missing, f"smoke evidence missing flags: {missing}"
    for flag in REQUIRED_TRUE_FLAGS:
        assert evidence[flag] is True, f"{flag} must be true"
    for flag in REQUIRED_FALSE_FLAGS:
        assert evidence[flag] is False, f"{flag} must stay false"


@pytest.mark.parametrize("flag", list(REQUIRED_TRUE_FLAGS))
def test_each_flag_holds(flag: str) -> None:
    evidence = run_order_path_smoke()
    assert evidence[flag] is True
