"""v0.2.2 formal tamper matrix: exact key sets and child-wise aggregates."""

import csv
import json

import pytest

from quantlab.artifacts import COMPLETION_MARKER, INCOMPLETE_MARKER, ArtifactPublisher
from quantlab.execution.artifacts import (
    EXECUTION_READINESS_SCHEMA_V0_2_2,
    READINESS_CHECK_COLUMNS,
    execution_readiness_artifact_contract,
    execution_readiness_semantic_failures,
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
    smokes = _smoke_payload(tamper)
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
            writer.writerow({
                "check_id": check_id, "category": "framework",
                "status": status, "finding": "ok",
                "evidence_json": json.dumps(
                    _canonical_evidence(check_id, smokes["scenarios"])
                ),
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
    (publisher.staging / "order_path_smoke.json").write_text(json.dumps(smokes))
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


def _smoke_payload(tamper: dict | None = None) -> dict:
    tamper = tamper or {}
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
    return {
        "synthetic": True, "non_trading": True, "provider_called": False,
        "canonical_data_written": False, "order_submission_attempted": False,
        "fill_claimed": False, "external_broker_submission": False,
        "scenarios": smokes,
    }


def _canonical_evidence(check_id: str, scenarios: dict) -> dict:
    """Canonical framework-row evidence, value-bound to the smoke payload.

    Mirrors the row evidence the v0.2.2 report builder derives from its
    smoke evidence (and therefore the completed v0.2.2 artifact bytes).
    """
    if check_id == "artifact_protocol":
        return {"publication": "staging -> manifest -> preflight -> "
                               "promotion -> marker"}
    if check_id == "target_portfolio_handoff":
        return {"smoke_valid": True, "price_role": "planning_only"}
    if check_id == "order_and_ledger_contracts":
        return {"money_unit": "integer_fen", "timestamp_policy":
                "timezone_aware"}
    if check_id == "production_submission_path_reachable":
        return {
            "engine": "AShareConstraintEngine",
            "derived_status": "validated",
            "fillability_unknown_allowed": True,
            "gates_rechecked": sorted(scenarios["submission_gate_matrix"]),
        }
    if check_id == "account_aware_order_planning":
        return {
            "plan_id_deterministic": scenarios["order_plan_deterministic"],
            "independent_order_price_evidence": scenarios[
                "buy_limit_from_order_price_evidence"
            ],
            "omitted_held_name_exits": scenarios["omitted_held_name_exits"],
            "non_conforming_delta_blocks": scenarios[
                "non_conforming_delta_blocks"
            ],
            "buys_funded_from_available_cash_only": scenarios[
                "buys_funded_from_available_cash_only"
            ],
            "required_subconditions": list(_REQUIRED[check_id]),
        }
    if check_id == "atomic_cash_reservation":
        return {
            "contention_blocked": scenarios[
                "aggregate_cash_contention_blocked"
            ],
            "partial_fill_drawdown": scenarios["partial_fill_drawdown"],
            "full_fill_release": scenarios["full_fill_release"],
            "cancel_release": scenarios["cancel_release"],
            "multi_partial_fee_reconciliation": scenarios[
                "multi_partial_fee_reconciliation"
            ],
            "batch_rollback_preserves_state": scenarios[
                "batch_rollback_preserves_state"
            ],
            "required_subconditions": list(_REQUIRED[check_id]),
        }
    if check_id == "atomic_share_reservation":
        return {"contention_blocked": scenarios["share_contention_blocked"]}
    if check_id == "stale_assessment_rejection":
        return {"stale_rejected": scenarios["stale_assessment_rejected"]}
    if check_id == "day_trade_date_binding":
        return {
            "wrong_trade_date_rejected": scenarios["wrong_trade_date_rejected"]
        }
    if check_id == "calendar_derived_t_plus_one":
        return {
            "calendar_bound": True,
            "weekend_exact": scenarios["weekend_t_plus_one_exact"],
            "holiday_exact": scenarios["holiday_t_plus_one_exact"],
            "incomplete_coverage_fail_closed": scenarios[
                "missing_next_session_fail_closed"
            ],
            "required_subconditions": list(_REQUIRED[check_id]),
        }
    if check_id == "transactional_submission_atomicity":
        return {
            "fault_injection_matrix": dict(
                scenarios["fault_injection_matrix"]
            ),
            "batch_rollback_preserves_state": scenarios[
                "batch_rollback_preserves_state"
            ],
            "transactional_submission_committed": scenarios[
                "transactional_submission_committed"
            ],
        }
    if check_id == "fee_budget_limit_protection":
        return {
            "fee_cap_semantics": "cumulative_order_lifetime",
            "typed_fee_quote_required": True,
            "multi_partial_fee_reconciliation": scenarios[
                "multi_partial_fee_reconciliation"
            ],
            "full_fill_release": scenarios["full_fill_release"],
        }
    if check_id == "plan_to_order_lineage":
        return {
            "plan_to_order_lineage": scenarios["plan_to_order_lineage"],
            "transactional_submission_committed": scenarios[
                "transactional_submission_committed"
            ],
            "external_broker_submission": scenarios[
                "external_broker_submission"
            ],
        }
    if check_id == "submission_authority_and_day_binding":
        return {
            "authority_lineage_bound": scenarios["authority_lineage_bound"],
            "day_submission_date_bound": scenarios[
                "day_submission_date_bound"
            ],
            "same_state_batch_committed": scenarios[
                "same_state_batch_committed"
            ],
            "stale_batch_rejected": scenarios["stale_batch_rejected"],
            "typed_quote_lineage": scenarios["typed_quote_lineage"],
            "required_subconditions": list(_REQUIRED[check_id]),
        }
    if check_id == "t_plus_one_sellability":
        return {
            "model": "position_lot",
            "same_day_sale_blocked": True,
            "calendar_bound_next_session_derivation": True,
            "weekend_exact": scenarios["weekend_t_plus_one_exact"],
            "holiday_exact": scenarios["holiday_t_plus_one_exact"],
            "incomplete_coverage_fail_closed": scenarios[
                "missing_next_session_fail_closed"
            ],
            "required_subconditions": list(_REQUIRED[check_id]),
        }
    return {}


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
    "calendar_derived_t_plus_one": [
        "weekend_t_plus_one_exact", "holiday_t_plus_one_exact",
        "missing_next_session_fail_closed",
    ],
    "submission_authority_and_day_binding": [
        "authority_lineage_bound", "day_submission_date_bound",
        "same_state_batch_committed", "stale_batch_rejected",
        "typed_quote_lineage",
    ],
    "t_plus_one_sellability": [
        "weekend_t_plus_one_exact", "holiday_t_plus_one_exact",
        "missing_next_session_fail_closed",
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


# ------------------- hardened semantic reader: value-binding contract ----

def _failures(run_dir) -> tuple[str, ...]:
    return execution_readiness_semantic_failures(
        run_dir, schema=EXECUTION_READINESS_SCHEMA_V0_2_2
    )


def _rewrite_row(run_dir, check_id: str, mutate) -> None:
    with (run_dir / "readiness_checks.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        if row["check_id"] == check_id:
            mutate(row)
    with (run_dir / "readiness_checks.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=READINESS_CHECK_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _mutate_evidence(mutator) -> None:
    def mutate(row) -> None:
        evidence = json.loads(row["evidence_json"])
        mutator(evidence)
        row["evidence_json"] = json.dumps(evidence)
    return mutate


def test_clean_canonical_rows_verify_under_the_hardened_reader(tmp_path) -> None:
    final = _publish(tmp_path)
    assert _failures(final) == ()


def test_missing_readiness_csv_is_a_deterministic_failure(tmp_path) -> None:
    """A missing readiness_checks.csv must produce semantic failures, never
    an UnboundLocalError/NameError from an unbound row variable."""
    final = _publish(tmp_path)
    (final / "readiness_checks.csv").unlink()
    failures = _failures(final)
    assert any("readiness CSV" in failure for failure in failures)


def test_unreadable_readiness_csv_is_a_deterministic_failure(tmp_path) -> None:
    final = _publish(tmp_path)
    (final / "readiness_checks.csv").unlink()
    (final / "readiness_checks.csv").mkdir()
    failures = _failures(final)
    assert any("readiness CSV" in failure for failure in failures)


def test_empty_readiness_csv_is_a_deterministic_failure(tmp_path) -> None:
    final = _publish(tmp_path)
    (final / "readiness_checks.csv").write_text("")
    failures = _failures(final)
    assert failures and all(
        isinstance(failure, str) for failure in failures
    )
    assert any(
        "readiness CSV" in failure or "columns" in failure
        for failure in failures
    )


def test_malformed_readiness_csv_bytes_are_a_deterministic_failure(
    tmp_path,
) -> None:
    """Non-UTF-8 CSV bytes must surface as a semantic parse failure, not
    an incidental UnicodeDecodeError escaping the verifier."""
    final = _publish(tmp_path)
    (final / "readiness_checks.csv").write_bytes(
        b"\xff\xfe\x00\x01not,avalid,utf8,csv"
    )
    failures = _failures(final)
    assert any("readiness CSV" in failure for failure in failures)


def test_ready_row_with_a_non_required_false_field_is_rejected(tmp_path) -> None:
    """A framework READY row whose evidence carries a FALSE field that is
    not part of any required_subconditions disclosure is contradictory
    evidence and must fail the hardened reader."""
    final = _publish(tmp_path)

    def flip_false(evidence: dict) -> None:
        evidence["contention_blocked"] = False

    _rewrite_row(
        final, "atomic_share_reservation", _mutate_evidence(flip_false)
    )
    failures = _failures(final)
    assert any("atomic_share_reservation" in failure for failure in failures)


def test_ready_row_missing_required_subconditions_is_rejected(tmp_path) -> None:
    """A composite framework row that omits its required_subconditions
    disclosure cannot claim READY without declaring its children."""
    final = _publish(tmp_path)

    def drop(evidence: dict) -> None:
        evidence.pop("required_subconditions")

    _rewrite_row(
        final, "account_aware_order_planning", _mutate_evidence(drop)
    )
    failures = _failures(final)
    assert any("account_aware_order_planning" in failure for failure in failures)


def test_undeclared_smoke_derived_evidence_key_is_rejected(tmp_path) -> None:
    """An evidence key borrowed from the smoke vocabulary that the row's
    canonical contract does not declare is rejected."""
    final = _publish(tmp_path)

    def smuggle(evidence: dict) -> None:
        evidence["typed_quote_lineage"] = True

    _rewrite_row(
        final, "atomic_share_reservation", _mutate_evidence(smuggle)
    )
    failures = _failures(final)
    assert any("atomic_share_reservation" in failure for failure in failures)


def test_retyped_evidence_value_is_rejected(tmp_path) -> None:
    """A boolean evidence field retyped to a string is a value-binding
    failure even though its text looks true."""
    final = _publish(tmp_path)

    def retype(evidence: dict) -> None:
        evidence["contention_blocked"] = "true"

    _rewrite_row(
        final, "atomic_share_reservation", _mutate_evidence(retype)
    )
    failures = _failures(final)
    assert any("atomic_share_reservation" in failure for failure in failures)


def test_reordered_required_subconditions_are_rejected(tmp_path) -> None:
    """The required_subconditions disclosure is canonical and ordered; a
    reordered list is non-canonical."""
    final = _publish(tmp_path)

    def reorder(evidence: dict) -> None:
        evidence["required_subconditions"] = list(
            reversed(evidence["required_subconditions"])
        )

    _rewrite_row(
        final, "calendar_derived_t_plus_one", _mutate_evidence(reorder)
    )
    failures = _failures(final)
    assert any("calendar_derived_t_plus_one" in failure for failure in failures)


def test_contradictory_row_value_against_smoke_is_rejected(tmp_path) -> None:
    """A framework row claiming a smoke-bound field the smoke evidence
    contradicts (row true, smoke false) is rejected while the smoke file
    itself still satisfies its own canonical key set."""
    final = _publish(tmp_path)
    smoke = json.loads((final / "order_path_smoke.json").read_text())
    smoke["scenarios"]["stale_assessment_rejected"] = False
    (final / "order_path_smoke.json").write_text(json.dumps(smoke))
    failures = _failures(final)
    assert any(
        "stale_assessment_rejection" in failure for failure in failures
    )
