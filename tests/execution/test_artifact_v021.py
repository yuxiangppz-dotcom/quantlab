"""v0.2.1 formal artifact tamper matrix (Part A negative tests).

Every tamper - flipped smoke booleans, deleted positive-weight
instruction, smoke/CSV/summary contradictions, unknown schema, and
self-claiming markers - must abort before promotion or fail external
verification. A semantic failure can never reach COMPLETED.json.
"""

import csv
import json

import pytest

from quantlab.artifacts import COMPLETION_MARKER, INCOMPLETE_MARKER, ArtifactPublisher
from quantlab.execution.artifacts import (
    EXECUTION_READINESS_SCHEMA_V0_2_1,
    READINESS_CHECK_COLUMNS,
    execution_readiness_artifact_contract,
    execution_readiness_semantic_failures,
    verify_execution_readiness_artifact,
)
from quantlab.execution.readiness import (
    READINESS_CHECK_IDS_V0_2_1,
    ReadinessStatus,
)

HEAD = "a" * 40
RUN_ID = "20260906T210000"

_READY = "ready"
_PARTIAL = "partial"
_BLOCKED = "blocked"
_NOT_MODELED = "not_modeled"

_FIXED_STATUS = {
    "security_master_identity": _PARTIAL,
    "instrument_code_lineage": _PARTIAL,
    "suspension_partition_coverage": _PARTIAL,
    "pit_trading_rule_resolution": _PARTIAL,
    "statutory_and_broker_fee_readiness": _BLOCKED,
}
_READY_BY_DEFAULT = set(READINESS_CHECK_IDS_V0_2_1) - set(_FIXED_STATUS) - {
    "price_limit_and_cage_readiness",
    "corporate_action_share_ledger",
    "auction_minute_orderbook_fill_evidence",
    "paper_broker_gateway",
    "live_operational_controls",
}
_CRITICAL_FOR = {
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

_FULL_SMOKES = {
    "synthetic": True,
    "non_trading": True,
    "provider_called": False,
    "canonical_data_written": False,
    "order_submission_attempted": False,
    "fill_claimed": False,
    "external_broker_submission": False,
    "production_submission_path_reachable": True,
    "gating_dimensions_fail_closed": True,
    "account_aware_order_planning": True,
    "order_plan_deterministic": True,
    "buy_limit_from_order_price_evidence": True,
    "buys_funded_from_available_cash_only": True,
    "plan_to_order_lineage": True,
    "transactional_submission_committed": True,
    "omitted_held_name_exits": True,
    "non_conforming_delta_blocks": True,
    "aggregate_cash_contention_blocked": True,
    "atomic_cash_reservation": True,
    "partial_fill_drawdown": True,
    "cancel_release": True,
    "multi_partial_fee_reconciliation": True,
    "full_fill_release": True,
    "batch_rollback_preserves_state": True,
    "share_contention_blocked": True,
    "atomic_share_reservation": True,
    "wrong_trade_date_rejected": True,
    "weekend_t_plus_one_exact": True,
    "holiday_t_plus_one_exact": True,
    "missing_next_session_fail_closed": True,
    "stale_assessment_rejected": True,
    "event_replay_matches_fresh_run": True,
    "submission_gate_matrix": {
        "fillability_unknown": "validated",
        "market_accessibility_unknown": "unknown",
        "fee_determinability_unknown": "unknown",
        "order_admissibility_unknown": "unknown",
    },
    "fault_injection_matrix": {
        "all_passed": True,
        "batch_first_submission_runtimeerror_restored": True,
        "batch_middle_submission_keyboardinterrupt_restored": True,
        "batch_last_submission_runtimeerror_restored": True,
        "invariant_failure_after_mutation_restored": True,
        "single_append_baseexception_restored": True,
    },
}


def _status(check_id: str, tamper: dict) -> str:
    flip = tamper.get("flip", {})
    if check_id in flip:
        return flip[check_id]
    return _READY if check_id in _READY_BY_DEFAULT else _FIXED_STATUS.get(
        check_id, _NOT_MODELED
    )


def _handoff(tamper: dict) -> dict:
    positive_instruction = {
        "instruction_id": "smoke-instruction",
        "targets": [
            {"instrument_id": "600000.SH", "target_shares": 3600},
        ],
    }
    if tamper.get("drop_positive_instruction"):
        positive_instruction = None
    if tamper.get("empty_positive_targets"):
        positive_instruction = {
            "instruction_id": "smoke-instruction", "targets": [],
        }
    return {
        "all_cash": {
            "instruction": None,
            "audit_rows": [],
            "status": "ready",
            "reason_codes": [],
            "is_order_submission": False,
            "is_fill_evidence": False,
        },
        "positive_weight": {
            "instruction": positive_instruction,
            "audit_rows": (
                [{
                    "instrument_id": "600000.SH", "target_shares": 3600,
                }]
                if positive_instruction
                else []
            ),
            "status": "ready",
            "reason_codes": [],
            "is_order_submission": False,
            "is_fill_evidence": False,
        },
    }


def _publish(tmp_path, tamper: dict | None = None, schema: str = EXECUTION_READINESS_SCHEMA_V0_2_1):
    tamper = tamper or {}
    publisher = ArtifactPublisher(
        tmp_path,
        RUN_ID,
        expected_registry=execution_readiness_artifact_contract(schema),
        head=HEAD,
        schema=schema,
    )
    statuses = {
        check_id: _status(check_id, tamper) for check_id in READINESS_CHECK_IDS_V0_2_1
    }
    counts = {
        status.value: sum(value == status.value for value in statuses.values())
        for status in ReadinessStatus
    }
    framework_ready = all(
        statuses.get(check_id) in {"ready", "partial"}
        for check_id in READINESS_CHECK_IDS_V0_2_1
        if check_id.startswith(("artifact_protocol", "target_portfolio",
                                "order_and_ledger", "production", "account_",
                                "atomic_", "stale_", "day_", "calendar_",
                                "transactional_", "fee_budget",
                                "plan_to", "t_plus_one"))
    )
    summary = {
        "analysis_type": "execution_readiness",
        "experiment_schema": schema,
        "run_id": RUN_ID,
        "code_version": HEAD,
        "check_count": len(READINESS_CHECK_IDS_V0_2_1),
        "status_counts": tamper.get("status_counts", counts),
        "gates": {
            "framework_valid": framework_ready,
            "historical_execution_ready": False,
            "paper_execution_ready": False,
            "live_execution_ready": False,
        },
        "claims": {
            "performance_claim": False,
            "fill_claim": False,
            "order_submission": False,
            "canonical_data_written": False,
            "external_provider_called": False,
        },
    }
    if tamper.get("self_claim_success"):
        summary["gates"]["framework_valid"] = True
    (publisher.staging / "summary.json").write_text(json.dumps(summary))
    with (publisher.staging / "readiness_checks.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=READINESS_CHECK_COLUMNS)
        writer.writeheader()
        for check_id in READINESS_CHECK_IDS_V0_2_1:
            writer.writerow({
                "check_id": check_id,
                "category": "framework",
                "status": statuses[check_id],
                "finding": "ok",
                "evidence_json": json.dumps(
                    tamper.get("evidence_override", {}).get(check_id, {})
                ),
                "limitation": "none",
                "critical_for": _CRITICAL_FOR.get(check_id, "framework"),
            })
    (publisher.staging / "rule_inventory.json").write_text("{}")
    (publisher.staging / "handoff_smoke.json").write_text(
        json.dumps(_handoff(tamper))
    )
    smokes = dict(_FULL_SMOKES)
    smokes.update(tamper.get("flip_smoke", {}))
    for key in tamper.get("remove_smoke", []):
        smokes.pop(key, None)
    if tamper.get("extend_smoke"):
        smokes["extra_field"] = True
    if tamper.get("retype_smoke"):
        smokes["synthetic"] = "true"
    (publisher.staging / "order_path_smoke.json").write_text(json.dumps({
        "synthetic": True,
        "non_trading": True,
        "provider_called": False,
        "canonical_data_written": False,
        "order_submission_attempted": False,
        "fill_claimed": False,
        "external_broker_submission": False,
        "scenarios": smokes,
    }))
    (publisher.staging / "transaction_fault_injection.json").write_text(
        json.dumps(tamper.get("fault_evidence", {"all_passed": True,
                    **_FULL_SMOKES["fault_injection_matrix"]}))
    )
    (publisher.staging / "fee_reservation_reconciliation.json").write_text(
        json.dumps(tamper.get("fee_evidence", {"reconciled": True}))
    )
    (publisher.staging / "input_inventory.json").write_text(json.dumps({
        "stable_during_audit": True,
        "partition_families": {
            "suspensions": {
                "missing_files": 0,
                "all_schemas_valid": True,
                "row_audit": {"all_rows_clean": True},
            },
        },
    }))
    if tamper.get("keyboard_interrupt_preflight"):
        from quantlab.execution import artifacts as artifacts_module

        original = artifacts_module._v021_smoke_failures

        def raising(run_dir, schema=None):
            raise KeyboardInterrupt

        artifacts_module._v021_smoke_failures = raising  # type: ignore[attr-defined]
        try:
            publisher.publish(summary)
        finally:
            artifacts_module._v021_smoke_failures = original  # type: ignore[attr-defined]
        return None
    return publisher.publish(summary)


def test_clean_v021_artifact_verifies(tmp_path) -> None:
    final = _publish(tmp_path)
    result = verify_execution_readiness_artifact(
        final,
        expected_run_id=RUN_ID,
        expected_head=HEAD,
        expected_schema=EXECUTION_READINESS_SCHEMA_V0_2_1,
    )
    assert result["complete"] is True


def test_v021_missing_header_fails_without_rebinding_exception(tmp_path) -> None:
    final = _publish(tmp_path)
    checks_path = final / "readiness_checks.csv"
    with checks_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    fieldnames = tuple(
        column for column in READINESS_CHECK_COLUMNS if column != "check_id"
    )
    with checks_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    failures = execution_readiness_semantic_failures(
        final, schema=EXECUTION_READINESS_SCHEMA_V0_2_1
    )

    assert any("columns are non-canonical" in failure for failure in failures)


def test_v021_missing_cell_fails_without_rebinding_exception(tmp_path) -> None:
    final = _publish(tmp_path)
    checks_path = final / "readiness_checks.csv"
    lines = checks_path.read_text().splitlines()
    lines[1] = lines[1].rsplit(",", 1)[0]
    checks_path.write_text("\n".join(lines) + "\n")

    failures = execution_readiness_semantic_failures(
        final, schema=EXECUTION_READINESS_SCHEMA_V0_2_1
    )

    assert any(
        "row 1" in failure and "usable strings" in failure
        for failure in failures
    )


@pytest.mark.parametrize(
    "tamper",
    [
        {"flip_smoke": {"omitted_held_name_exits": False}},
        {"flip_smoke": {"batch_rollback_preserves_state": False}},
        {"flip_smoke": {"external_broker_submission": True}},
        {"remove_smoke": ["full_fill_release"]},
        {"extend_smoke": True},
        {"retype_smoke": True},
        {"drop_positive_instruction": True},
        {"empty_positive_targets": True},
        {"flip": {"plan_to_order_lineage": "blocked"}},
        {"flip": {"account_aware_order_planning": "blocked"}},
        {"status_counts": {
            "ready": 27, "partial": 0, "blocked": 0,
            "not_modeled": 0, "not_applicable": 0,
        }},
        {"fault_evidence": {"all_passed": False}},
        {"fee_evidence": {"reconciled": False}},
        {"self_claim_success": True, "flip": {
            "transactional_submission_atomicity": "blocked",
        }},
    ],
)
def test_semantic_tampering_cannot_reach_a_completed_marker(tmp_path, tamper) -> None:
    with pytest.raises(RuntimeError):
        _publish(tmp_path, tamper)
    assert (tmp_path / RUN_ID).exists() is False
    staging = tmp_path / f"{RUN_ID}.incomplete"
    assert (staging / COMPLETION_MARKER).exists() is False
    assert (staging / INCOMPLETE_MARKER).exists()


def test_tampered_marker_and_manifest_still_fail_external_verification(tmp_path) -> None:
    """Marker/manifest both claim success, but the domain semantics are
    wrong: the independent verifier must reject the artifact."""
    final = _publish(tmp_path)
    rows_path = final / "readiness_checks.csv"
    with rows_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    # flip a smoke boolean so the CSV READY no longer matches the evidence
    smoke = json.loads((final / "order_path_smoke.json").read_text())
    smoke["scenarios"]["plan_to_order_lineage"] = False
    (final / "order_path_smoke.json").write_text(json.dumps(smoke))
    with rows_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=READINESS_CHECK_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(RuntimeError, match="derive"):
        verify_execution_readiness_artifact(
            final,
            expected_run_id=RUN_ID,
            expected_head=HEAD,
            expected_schema=EXECUTION_READINESS_SCHEMA_V0_2_1,
        )


def test_keyboardinterrupt_during_preflight_never_promotes(tmp_path) -> None:
    with pytest.raises(KeyboardInterrupt):
        _publish(tmp_path, {"keyboard_interrupt_preflight": True})
    assert (tmp_path / RUN_ID).exists() is False
    staging = tmp_path / f"{RUN_ID}.incomplete"
    assert (staging / COMPLETION_MARKER).exists() is False
    assert (staging / INCOMPLETE_MARKER).exists()


def test_unknown_schema_is_rejected_by_the_contract() -> None:
    with pytest.raises(ValueError, match="unknown execution readiness schema"):
        execution_readiness_artifact_contract("execution_readiness_v0_9")
    with pytest.raises(ValueError, match="unknown execution readiness schema"):
        verify_execution_readiness_artifact(
            "unused",
            expected_schema="execution_readiness_v0_9",
        )
