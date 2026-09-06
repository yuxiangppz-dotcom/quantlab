import csv
import json
from datetime import date

import pytest

from quantlab.artifacts import ArtifactPublisher
from quantlab.execution.artifacts import (
    EXECUTION_READINESS_SCHEMA,
    READINESS_CHECK_COLUMNS,
    execution_readiness_artifact_contract,
    verify_execution_readiness_artifact,
)
from quantlab.execution.readiness import (
    ReadinessStatus,
    build_execution_readiness_report,
)

HEAD = "a" * 40
RUN_ID = "20260906T200000"


def _evidence():
    family = {
        "expected_files": 1212,
        "existing_files": 1212,
        "missing_files": 0,
        "missing_examples": [],
        "schema_invalid_files": 0,
        "schema_invalid_examples": [],
        "total_bytes": 100,
        "combined_content_sha256": "b" * 64,
        "schema_samples": [],
        "all_schemas_valid": True,
    }
    return {
        "calendar": {
            "period_calendar_days": 1827,
            "sse_calendar_days": 1827,
            "szse_calendar_days": 1827,
            "all_calendar_days_present": True,
            "sse_open_sessions": 1212,
            "szse_open_sessions": 1212,
            "open_sessions_aligned": True,
            "duplicate_exchange_date_rows": 0,
            "open_session_dates": ["2020-01-02", "2024-12-31"],
        },
        "security_master": {
            "rows": 5000,
            "required_fields_present": True,
            "board_history_effective_dated": False,
        },
        "code_lineage": {
            "records": 3,
            "declared_complete_registry": False,
        },
        "daily": dict(family),
        "stock_st": dict(family),
        "suspensions": dict(family),
    }


def test_readiness_keeps_framework_separate_from_execution_gates() -> None:
    report, inventory = build_execution_readiness_report(
        _evidence(),
        period_start=date(2020, 1, 1),
        period_end=date(2024, 12, 31),
        handoff_smoke_valid=True,
    )
    assert report.framework_valid is True
    assert report.historical_execution_ready is False
    assert report.paper_execution_ready is False
    assert report.live_execution_ready is False
    assert set(report.status_counts) == {status.value for status in ReadinessStatus}
    assert inventory["SZSE:MAIN"]["known_gap"] == "2023-04-10/2024-12-31"


def test_missing_daily_partitions_block_historical_but_not_framework() -> None:
    evidence = _evidence()
    evidence["daily"]["missing_files"] = 1
    report, _ = build_execution_readiness_report(
        evidence,
        period_start=date(2020, 1, 1),
        period_end=date(2024, 12, 31),
        handoff_smoke_valid=True,
    )
    check = next(item for item in report.checks if item.check_id == "raw_daily_bar_fields")
    assert check.status is ReadinessStatus.BLOCKED
    assert report.framework_valid is True
    assert report.historical_execution_ready is False


def _publish(tmp_path, *, performance_field=False, wrong_gate=False):
    contract = execution_readiness_artifact_contract()
    publisher = ArtifactPublisher(
        tmp_path, RUN_ID, expected_registry=contract, head=HEAD,
        schema=EXECUTION_READINESS_SCHEMA,
    )
    summary = {
        "analysis_type": "execution_readiness",
        "experiment_schema": EXECUTION_READINESS_SCHEMA,
        "run_id": RUN_ID,
        "code_version": HEAD,
        "check_count": 17,
        "status_counts": {
            "ready": 3,
            "partial": 1,
            "blocked": 0,
            "not_modeled": 13,
            "not_applicable": 0,
        },
        "gates": {
            "framework_valid": True,
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
    if performance_field:
        summary["sharpe"] = 1.0
    if wrong_gate:
        summary["gates"]["historical_execution_ready"] = True
    (publisher.staging / "summary.json").write_text(json.dumps(summary))
    with (publisher.staging / "readiness_checks.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=READINESS_CHECK_COLUMNS)
        writer.writeheader()
        framework_ids = {
            "artifact_protocol",
            "target_portfolio_handoff",
            "order_and_ledger_contracts",
            "t_plus_one_sellability",
        }
        from quantlab.execution.readiness import READINESS_CHECK_IDS

        for check_id in READINESS_CHECK_IDS:
            status = "ready" if check_id in framework_ids else "not_modeled"
            if check_id == "t_plus_one_sellability":
                status = "partial"
            critical_for = "framework" if check_id in framework_ids else "historical"
            if check_id == "paper_broker_gateway":
                critical_for = "paper"
            elif check_id == "live_operational_controls":
                critical_for = "live"
            writer.writerow({
                "check_id": check_id, "category": "framework", "status": status,
                "finding": "ok", "evidence_json": "{}", "limitation": "none",
                "critical_for": critical_for,
            })
    (publisher.staging / "rule_inventory.json").write_text("{}")
    (publisher.staging / "handoff_smoke.json").write_text(json.dumps({
        "is_order_submission": False, "is_fill_evidence": False,
    }))
    (publisher.staging / "input_inventory.json").write_text(json.dumps({
        "stable_during_audit": True,
    }))
    return publisher.publish(summary)


def test_execution_readiness_artifact_verifies_domain_semantics(tmp_path) -> None:
    final = _publish(tmp_path)
    result = verify_execution_readiness_artifact(
        final, expected_run_id=RUN_ID, expected_head=HEAD
    )
    assert result["complete"] is True


def test_execution_readiness_artifact_rejects_performance_fields(tmp_path) -> None:
    """A prohibited performance field is a domain semantic failure: it must
    abort publication BEFORE promotion and never leave a success marker."""
    from quantlab.artifacts import COMPLETION_MARKER, INCOMPLETE_MARKER

    with pytest.raises(RuntimeError, match="performance fields are prohibited"):
        _publish(tmp_path, performance_field=True)
    assert (tmp_path / RUN_ID).exists() is False
    staging = tmp_path / f"{RUN_ID}.incomplete"
    assert (staging / COMPLETION_MARKER).exists() is False
    assert (staging / INCOMPLETE_MARKER).exists()


def test_execution_readiness_artifact_rederives_gates(tmp_path) -> None:
    """Gates are re-derived from check evidence at preflight: a hand-written
    gate can never reach promotion."""
    from quantlab.artifacts import COMPLETION_MARKER, INCOMPLETE_MARKER

    with pytest.raises(RuntimeError, match="gates do not match"):
        _publish(tmp_path, wrong_gate=True)
    assert (tmp_path / RUN_ID).exists() is False
    staging = tmp_path / f"{RUN_ID}.incomplete"
    assert (staging / COMPLETION_MARKER).exists() is False
    assert (staging / INCOMPLETE_MARKER).exists()


# ---------------------------------------------------------------------------
# v0.2.1: composite READY re-binding and per-family audit semantics
# ---------------------------------------------------------------------------

FULL_SMOKE = {
    "production_submission_path_reachable": True,
    "submission_gate_matrix": {"fillability_unknown": "validated"},
    "order_plan_deterministic": True,
    "buy_limit_from_order_price_evidence": True,
    "omitted_held_name_exits": True,
    "non_conforming_delta_blocks": True,
    "buys_funded_from_available_cash_only": True,
    "aggregate_cash_contention_blocked": True,
    "partial_fill_drawdown": True,
    "full_fill_release": True,
    "cancel_release": True,
    "multi_partial_fee_reconciliation": True,
    "batch_rollback_preserves_state": True,
    "fault_injection_matrix": {
        "all_passed": True,
        "batch_first_submission_runtimeerror_restored": True,
        "batch_middle_submission_keyboardinterrupt_restored": True,
        "batch_last_submission_runtimeerror_restored": True,
        "invariant_failure_after_mutation_restored": True,
        "single_append_baseexception_restored": True,
    },
    "transactional_submission_committed": True,
    "plan_to_order_lineage": True,
    "external_broker_submission": False,
    "weekend_t_plus_one_exact": True,
    "holiday_t_plus_one_exact": True,
    "missing_next_session_fail_closed": True,
    "stale_assessment_rejected": True,
    "wrong_trade_date_rejected": True,
    "share_contention_blocked": True,
    "atomic_share_reservation": True,
    "atomic_cash_reservation": True,
}


def _v021_report(overrides: dict | None = None):
    from quantlab.execution.readiness import EXECUTION_READINESS_SCHEMA_V0_2_1

    evidence = _evidence()
    smoke = dict(FULL_SMOKE)
    for key, value in (overrides or {}).items():
        if value is None:
            smoke.pop(key, None)
        else:
            smoke[key] = value
    return build_execution_readiness_report(
        evidence,
        period_start=date(2020, 1, 1),
        period_end=date(2024, 12, 31),
        handoff_smoke_valid=True,
        schema=EXECUTION_READINESS_SCHEMA_V0_2_1,
        order_path_smoke=smoke,
    )[0]


def _by_id(report, check_id):
    return {check.check_id: check for check in report.checks}[check_id]


def test_v021_framework_checks_all_ready_on_full_smoke() -> None:
    report = _v021_report()
    from quantlab.execution.readiness import (
        EXECUTION_READINESS_SCHEMA_V0_2_1,
        framework_check_ids,
        readiness_check_ids,
    )

    assert len(report.checks) == len(readiness_check_ids(
        EXECUTION_READINESS_SCHEMA_V0_2_1
    ))
    for check_id in sorted(framework_check_ids(EXECUTION_READINESS_SCHEMA_V0_2_1)):
        assert _by_id(report, check_id).status in {
            ReadinessStatus.READY, ReadinessStatus.PARTIAL,
        }, check_id
    assert report.framework_valid is True
    # suspension raw integrity can no longer auto-upgrade to ready
    assert _by_id(report, "suspension_partition_coverage").status is (
        ReadinessStatus.PARTIAL
    )


@pytest.mark.parametrize(
    ("flip", "expected_blocked"),
    [
        ({"holiday_t_plus_one_exact": False}, "calendar_derived_t_plus_one"),
        ({"holiday_t_plus_one_exact": None}, "calendar_derived_t_plus_one"),
        ({"missing_next_session_fail_closed": False}, "calendar_derived_t_plus_one"),
        ({"omitted_held_name_exits": False}, "account_aware_order_planning"),
        ({"buys_funded_from_available_cash_only": False}, "account_aware_order_planning"),
        ({"full_fill_release": False}, "atomic_cash_reservation"),
        ({"multi_partial_fee_reconciliation": False}, "atomic_cash_reservation"),
        ({"batch_rollback_preserves_state": False}, "atomic_cash_reservation"),
        ({"batch_rollback_preserves_state": False}, "transactional_submission_atomicity"),
        ({"transactional_submission_committed": False}, "plan_to_order_lineage"),
    ],
)
def test_v021_composite_ready_requires_every_disclosed_subcondition(
    flip, expected_blocked
) -> None:
    report = _v021_report(flip)
    assert _by_id(report, expected_blocked).status is ReadinessStatus.BLOCKED
    assert report.framework_valid is False


def test_v021_t_plus_one_stays_partial_without_holiday_binding() -> None:
    report = _v021_report({"holiday_t_plus_one_exact": False})
    assert _by_id(report, "t_plus_one_sellability").status is (
        ReadinessStatus.PARTIAL
    )


def test_v021_fault_matrix_total_boolean_is_not_trusted() -> None:
    """all_passed=true with a failing member must not yield READY."""
    report = _v021_report({
        "fault_injection_matrix": {
            "all_passed": True,
            "batch_middle_submission_keyboardinterrupt_restored": False,
        },
    })
    assert _by_id(
        report, "transactional_submission_atomicity"
    ).status is ReadinessStatus.BLOCKED


def test_v021_unknown_schema_is_rejected_everywhere() -> None:
    from quantlab.execution.readiness import (
        framework_check_ids,
        readiness_check_ids,
    )

    with pytest.raises(ValueError, match="unknown execution readiness schema"):
        readiness_check_ids("execution_readiness_v0_9")
    with pytest.raises(ValueError, match="unknown execution readiness schema"):
        framework_check_ids("execution_readiness_v0_9")
    with pytest.raises(ValueError, match="unknown execution readiness schema"):
        build_execution_readiness_report(
            _evidence(),
            period_start=date(2020, 1, 1),
            period_end=date(2024, 12, 31),
            handoff_smoke_valid=True,
            schema="execution_readiness_v0_9",
        )
