"""v0.2.2 formal tamper matrix: exact key sets and child-wise aggregates."""

import csv
import json

import pytest

from quantlab.artifacts import COMPLETION_MARKER, INCOMPLETE_MARKER, ArtifactPublisher
from quantlab.execution.artifacts import (
    EXECUTION_READINESS_SCHEMA_V0_2_2,
    READINESS_CHECK_COLUMNS,
    execution_readiness_artifact_contract,
    verify_execution_readiness_artifact,
)
from quantlab.execution.readiness import (
    READINESS_CHECK_IDS_V0_2_2,
)

HEAD = "a" * 40
RUN_ID = "20260907T010000"

_READY = set(READINESS_CHECK_IDS_V0_2_2) - {
    "security_master_identity", "instrument_code_lineage",
    "suspension_partition_coverage", "pit_trading_rule_resolution",
    "statutory_and_broker_fee_readiness", "price_limit_and_cage_readiness",
    "corporate_action_share_ledger", "auction_minute_orderbook_fill_evidence",
    "paper_broker_gateway", "live_operational_controls",
}
_CRITICAL = {
    "canonical_calendar": "historical",
    "security_master_identity": "historical",
    "instrument_code_lineage": "historical",
    "raw_daily_bar_fields": "historical",
    "stock_st_partition_coverage": "historical",
    "suspension_partition_coverage": "historical",
    "pit_trading_rule_resolution": "historical",
    "t_plus_one_sellability": "framework|historical",
    "price_limit_and_cage_readiness": "historical",
    "statutory_and_broker_fee_readiness": "historical|paper|live",
    "corporate_action_share_ledger": "historical",
    "auction_minute_orderbook_fill_evidence": "historical",
    "paper_broker_gateway": "paper",
    "live_operational_controls": "live",
}

_TRUE_SCENARIOS = [
    "synthetic", "non_trading", "production_submission_path_reachable",
    "gating_dimensions_fail_closed", "account_aware_order_planning",
    "order_plan_deterministic", "buy_limit_from_order_price_evidence",
    "buys_funded_from_available_cash_only", "plan_to_order_lineage",
    "transactional_submission_committed", "omitted_held_name_exits",
    "non_conforming_delta_blocks", "aggregate_cash_contention_blocked",
    "atomic_cash_reservation", "partial_fill_drawdown", "cancel_release",
    "multi_partial_fee_reconciliation", "full_fill_release",
    "batch_rollback_preserves_state", "share_contention_blocked",
    "atomic_share_reservation", "wrong_trade_date_rejected",
    "weekend_t_plus_one_exact", "holiday_t_plus_one_exact",
    "missing_next_session_fail_closed", "stale_assessment_rejected",
    "event_replay_matches_fresh_run",
    "same_state_batch_committed", "sell_share_reservation",
    "typed_quote_lineage", "stale_batch_rejected",
    "day_submission_date_bound", "authority_lineage_bound",
]
_FAULT = {
    "synthetic": True, "non_trading": True,
    "external_broker_submission": False,
    "batch_first_submission_runtimeerror_restored": True,
    "batch_middle_submission_keyboardinterrupt_restored": True,
    "batch_last_submission_runtimeerror_restored": True,
    "invariant_failure_after_mutation_restored": True,
    "single_append_baseexception_restored": True,
    "all_passed": True,
}
_FEE = {
    "reconciled": True, "fee_cap_semantics": "cumulative_order_lifetime",
    "typed_fee_quote_required": True,
    "multi_partial_fee_reconciliation": True, "full_fill_release": True,
    "partial_fill_drawdown": True, "cancel_release": True,
}


def _publish(tmp_path, tamper: dict | None = None):
    tamper = tamper or {}
    publisher = ArtifactPublisher(
        tmp_path, RUN_ID,
        expected_registry=execution_readiness_artifact_contract(
            EXECUTION_READINESS_SCHEMA_V0_2_2
        ),
        head=HEAD, schema=EXECUTION_READINESS_SCHEMA_V0_2_2,
    )
    counts = {
        "ready": len(_READY),
        "partial": 4, "blocked": 1, "not_modeled": 5, "not_applicable": 0,
    }
    summary = {
        "analysis_type": "execution_readiness",
        "experiment_schema": EXECUTION_READINESS_SCHEMA_V0_2_2,
        "run_id": RUN_ID, "code_version": HEAD,
        "check_count": len(READINESS_CHECK_IDS_V0_2_2),
        "status_counts": counts,
        "gates": {
            "framework_valid": True,
            "historical_execution_ready": False,
            "paper_execution_ready": False,
            "live_execution_ready": False,
        },
        "claims": {
            "performance_claim": False, "fill_claim": False,
            "order_submission": False, "external_broker_submission": False,
            "canonical_data_written": False, "external_provider_called": False,
        },
    }
    if tamper.get("drop_claim"):
        summary["claims"].pop(tamper["drop_claim"])
    if tamper.get("extend_claims"):
        summary["claims"]["extra_claim"] = False
    if tamper.get("claim_true"):
        summary["claims"][tamper["claim_true"]] = True
    (publisher.staging / "summary.json").write_text(json.dumps(summary))
    with (publisher.staging / "readiness_checks.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=READINESS_CHECK_COLUMNS)
        writer.writeheader()
        for check_id in READINESS_CHECK_IDS_V0_2_2:
            if check_id in _READY:
                status = "ready"
            elif check_id == "statutory_and_broker_fee_readiness":
                status = "blocked"
            elif check_id in {
                "price_limit_and_cage_readiness",
                "corporate_action_share_ledger",
                "auction_minute_orderbook_fill_evidence",
                "paper_broker_gateway", "live_operational_controls",
            }:
                status = "not_modeled"
            else:
                status = "partial"
            evidence = {}
            if check_id in {
                "account_aware_order_planning",
                "atomic_cash_reservation", "atomic_share_reservation",
                "plan_to_order_lineage", "calendar_derived_t_plus_one",
                "submission_authority_and_day_binding",
            }:
                evidence["required_subconditions"] = _REQUIRED[check_id]
            writer.writerow({
                "check_id": check_id, "category": "framework",
                "status": status, "finding": "ok",
                "evidence_json": json.dumps(evidence),
                "limitation": "none",
                "critical_for": _CRITICAL.get(check_id, "framework"),
            })
    (publisher.staging / "rule_inventory.json").write_text("{}")
    (publisher.staging / "handoff_smoke.json").write_text(json.dumps({
        "all_cash": {"instruction": None, "is_order_submission": False,
                     "is_fill_evidence": False, "status": "ready"},
        "positive_weight": {
            "instruction": {"instruction_id": "x", "targets": [
                {"instrument_id": "600000.SH", "target_shares": 3600}]},
            "audit_rows": [{"instrument_id": "600000.SH",
                            "target_shares": 3600}],
            "is_order_submission": False, "is_fill_evidence": False,
            "status": "ready",
        },
    }))
    smokes = {key: True for key in _TRUE_SCENARIOS}
    for key in ("provider_called", "canonical_data_written",
                "order_submission_attempted", "fill_claimed",
                "external_broker_submission"):
        smokes[key] = False
    smokes["submission_gate_matrix"] = {
        "fillability_unknown": "validated",
        "market_accessibility_unknown": "unknown",
        "fee_determinability_unknown": "unknown",
        "order_admissibility_unknown": "unknown",
    }
    smokes["fault_injection_matrix"] = dict(_FAULT)
    if tamper.get("flip_smoke"):
        smokes[tamper["flip_smoke"]] = not smokes[tamper["flip_smoke"]]
    (publisher.staging / "order_path_smoke.json").write_text(json.dumps({
        "synthetic": True, "non_trading": True, "provider_called": False,
        "canonical_data_written": False, "order_submission_attempted": False,
        "fill_claimed": False, "external_broker_submission": False,
        "scenarios": smokes,
    }))
    fault = dict(_FAULT)
    if tamper.get("fault_child_false"):
        fault[tamper["fault_child_false"]] = False
    if tamper.get("fault_extra_key"):
        fault["extra"] = True
    (publisher.staging / "transaction_fault_injection.json").write_text(
        json.dumps(fault)
    )
    fee = dict(_FEE)
    if tamper.get("fee_child_false"):
        fee[tamper["fee_child_false"]] = False
    (publisher.staging / "fee_reservation_reconciliation.json").write_text(
        json.dumps(fee)
    )
    (publisher.staging / "input_inventory.json").write_text(json.dumps({
        "stable_during_audit": True,
        "partition_families": {"suspensions": {
            "missing_files": 0, "all_schemas_valid": True,
            "row_audit": {"all_rows_clean": True},
        }},
    }))
    return publisher.publish(summary)


_REQUIRED = {
    "account_aware_order_planning": [
        "order_plan_deterministic", "buy_limit_from_order_price_evidence",
        "omitted_held_name_exits", "non_conforming_delta_blocks",
        "buys_funded_from_available_cash_only",
    ],
    "atomic_cash_reservation": [
        "aggregate_cash_contention_blocked", "partial_fill_drawdown",
        "full_fill_release", "cancel_release",
        "multi_partial_fee_reconciliation", "batch_rollback_preserves_state",
    ],
    "atomic_share_reservation": ["share_contention_blocked"],
    "plan_to_order_lineage": [
        "plan_to_order_lineage", "transactional_submission_committed",
    ],
    "calendar_derived_t_plus_one": [
        "weekend_t_plus_one_exact", "holiday_t_plus_one_exact",
        "missing_next_session_fail_closed",
    ],
    "submission_authority_and_day_binding": [
        "authority_lineage_bound", "day_submission_date_bound",
        "same_state_batch_committed", "stale_batch_rejected",
        "typed_quote_lineage",
    ],
}


def test_clean_v022_artifact_verifies(tmp_path) -> None:
    final = _publish(tmp_path)
    result = verify_execution_readiness_artifact(
        final, expected_run_id=RUN_ID, expected_head=HEAD,
        expected_schema=EXECUTION_READINESS_SCHEMA_V0_2_2,
    )
    assert result["complete"] is True


@pytest.mark.parametrize(
    "tamper",
    [
        {"fault_child_false": "batch_last_submission_runtimeerror_restored"},
        {"fault_extra_key": True},
        {"fee_child_false": "cancel_release"},
        {"flip_smoke": "stale_batch_rejected"},
        {"flip_smoke": "typed_quote_lineage"},
        {"drop_claim": "external_broker_submission"},
        {"extend_claims": True},
        {"claim_true": "fill_claim"},
    ],
)
def test_v022_tampering_cannot_reach_a_completed_marker(tmp_path, tamper) -> None:
    with pytest.raises(RuntimeError):
        _publish(tmp_path, tamper)
    assert (tmp_path / RUN_ID).exists() is False
    staging = tmp_path / f"{RUN_ID}.incomplete"
    assert (staging / COMPLETION_MARKER).exists() is False
    assert (staging / INCOMPLETE_MARKER).exists()
