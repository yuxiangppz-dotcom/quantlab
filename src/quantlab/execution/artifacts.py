"""Formal artifact contract and semantic verifier for execution readiness.

The readiness domain semantics (check inventory, status counts, gate
re-derivation, prohibited performance fields, explicit false claims, handoff
denials, input stability) are bound to the artifact CONTRACT itself, so they
run at every enforcement point: the publisher's preflight (before
promotion), the post-promotion formal verification (before the completion
marker is accepted), and the independent external verifier.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from quantlab.artifacts import ArtifactContract, verify_formal_artifact
from quantlab.execution.readiness import (
    EXECUTION_READINESS_SCHEMA_V0_2_1,
    ReadinessStatus,
    framework_check_ids,
    readiness_check_ids,
    v021_composite_statuses,
)

EXECUTION_READINESS_SCHEMA = "execution_readiness_v0_1"
EXECUTION_READINESS_SCHEMA_V0_2 = "execution_readiness_v0_2"
EXECUTION_READINESS_TOP_LEVEL = (
    "summary.json",
    "readiness_checks.csv",
    "rule_inventory.json",
    "handoff_smoke.json",
    "input_inventory.json",
)
EXECUTION_READINESS_TOP_LEVEL_V0_2 = EXECUTION_READINESS_TOP_LEVEL + (
    "order_path_smoke.json",
)
EXECUTION_READINESS_TOP_LEVEL_V0_2_1 = EXECUTION_READINESS_TOP_LEVEL_V0_2 + (
    "transaction_fault_injection.json",
    "fee_reservation_reconciliation.json",
)
READINESS_CHECK_COLUMNS = (
    "check_id",
    "category",
    "status",
    "finding",
    "evidence_json",
    "limitation",
    "critical_for",
)
_PERFORMANCE_KEYS = {
    "total_return",
    "cagr",
    "sharpe",
    "max_drawdown",
    "alpha",
    "beta",
    "tracking_error",
    "information_ratio",
}
# v0.2.1 order-path smoke: the exact canonical field set with required
# truth values (anything missing, flipped, extended, or retyped is a
# semantic failure)
_V021_SMOKE_TRUE_FIELDS = (
    "synthetic",
    "non_trading",
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
_V021_SMOKE_FALSE_FIELDS = (
    "provider_called",
    "canonical_data_written",
    "order_submission_attempted",
    "fill_claimed",
    "external_broker_submission",
)
_V021_SMOKE_FIELDS = (
    _V021_SMOKE_TRUE_FIELDS + _V021_SMOKE_FALSE_FIELDS
    + ("submission_gate_matrix", "fault_injection_matrix")
)
_V021_FAULT_REQUIRED_KEYS = (
    "batch_first_submission_runtimeerror_restored",
    "batch_middle_submission_keyboardinterrupt_restored",
    "batch_last_submission_runtimeerror_restored",
    "invariant_failure_after_mutation_restored",
    "single_append_baseexception_restored",
)


def execution_readiness_artifact_contract(
    schema: str = EXECUTION_READINESS_SCHEMA,
) -> ArtifactContract:
    """Contract whose domain validator enforces readiness semantics on the
    actual directory at preflight, post-promotion formal verification, and
    external verification.

    The inventory is explicit per schema version; unknown schemas are
    rejected with no fallback to v0.1.
    """
    return ArtifactContract(
        name="execution_readiness",
        schema=schema,
        groups={},
        top_level=_inventory_for_schema(schema),
        semantic_validators=(validate_execution_readiness_semantics,),
    )


def _inventory_for_schema(schema: str) -> tuple[str, ...]:
    if schema == EXECUTION_READINESS_SCHEMA_V0_2_1:
        return EXECUTION_READINESS_TOP_LEVEL_V0_2_1
    if schema == EXECUTION_READINESS_SCHEMA_V0_2:
        return EXECUTION_READINESS_TOP_LEVEL_V0_2
    if schema == EXECUTION_READINESS_SCHEMA:
        return EXECUTION_READINESS_TOP_LEVEL
    raise ValueError(f"unknown execution readiness schema: {schema!r}")


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower()
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _v021_smoke_failures(run_dir: Path) -> tuple[str, ...]:
    """Deep canonical validation of the v0.2.1 order-path smoke evidence."""
    failures: list[str] = []
    try:
        payload = json.loads((run_dir / "order_path_smoke.json").read_text())
    except (OSError, ValueError) as exc:
        return (f"order_path_smoke.json semantic parse failed: {exc}",)
    if not isinstance(payload, dict):
        return ("order_path_smoke.json must be a JSON object",)
    header = {"synthetic", "non_trading", "provider_called",
              "canonical_data_written", "order_submission_attempted",
              "fill_claimed", "external_broker_submission", "scenarios"}
    if not header <= set(payload):
        failures.append("order_path_smoke.json outer fields are missing")
    for field in ("synthetic", "non_trading"):
        if payload.get(field) is not True:
            failures.append(f"order_path_smoke {field} must be true")
    for field in ("provider_called", "canonical_data_written",
                  "order_submission_attempted", "fill_claimed",
                  "external_broker_submission"):
        if payload.get(field) is not False:
            failures.append(f"order_path_smoke {field} must be false")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, dict):
        failures.append("order_path_smoke scenarios must be an object")
        return tuple(failures)
    scenario_keys = set(scenarios)
    if scenario_keys != set(_V021_SMOKE_FIELDS):
        missing = sorted(set(_V021_SMOKE_FIELDS) - scenario_keys)
        extra = sorted(scenario_keys - set(_V021_SMOKE_FIELDS))
        failures.append(
            "order_path_smoke scenarios are non-canonical: "
            f"missing={missing} extra={extra}"
        )
        return tuple(failures)
    for field in _V021_SMOKE_TRUE_FIELDS:
        if scenarios.get(field) is not True:
            failures.append(f"smoke scenario {field} must be true")
    for field in _V021_SMOKE_FALSE_FIELDS:
        if scenarios.get(field) is not False:
            failures.append(f"smoke scenario {field} must be false")
    gate_matrix = scenarios.get("submission_gate_matrix")
    if not isinstance(gate_matrix, dict):
        failures.append("smoke submission_gate_matrix must be an object")
    else:
        expected_matrix = {
            "fillability_unknown": "validated",
            "market_accessibility_unknown": "unknown",
            "fee_determinability_unknown": "unknown",
            "order_admissibility_unknown": "unknown",
        }
        if gate_matrix != expected_matrix:
            failures.append(
                "smoke submission_gate_matrix is non-canonical"
            )
    fault = scenarios.get("fault_injection_matrix")
    if not isinstance(fault, dict):
        failures.append("smoke fault_injection_matrix must be an object")
    elif fault.get("all_passed") is not True or any(
        fault.get(key) is not True for key in _V021_FAULT_REQUIRED_KEYS
    ):
        failures.append(
            "smoke fault_injection_matrix is not fully passing"
        )
    return tuple(failures)


def _v021_handoff_failures(run_dir: Path) -> tuple[str, ...]:
    """Positive-weight and all-cash handoff self-consistency."""
    failures: list[str] = []
    try:
        handoff = json.loads((run_dir / "handoff_smoke.json").read_text())
    except (OSError, ValueError) as exc:
        return (f"handoff_smoke.json semantic parse failed: {exc}",)
    if not isinstance(handoff, dict) or {"all_cash", "positive_weight"} - set(
        handoff
    ):
        return ("handoff_smoke.json must contain all_cash and "
                "positive_weight scenarios",)
    for variant in handoff.values():
        if variant.get("is_order_submission") is not False:
            failures.append("handoff smoke must deny order submission")
        if variant.get("is_fill_evidence") is not False:
            failures.append("handoff smoke must deny fill evidence")
    positive = handoff["positive_weight"]
    if not isinstance(positive, dict):
        return (*failures, "positive_weight handoff must be an object",)
    if positive.get("status") != "ready":
        failures.append("positive_weight handoff must be ready")
    instruction = positive.get("instruction")
    if not isinstance(instruction, dict):
        failures.append("positive_weight handoff must carry an instruction")
    else:
        targets = instruction.get("targets")
        if not isinstance(targets, list) or not any(
            isinstance(item, dict) and item.get("target_shares", 0) > 0
            for item in targets
        ):
            failures.append(
                "positive_weight handoff needs a positive share target"
            )
        audit_rows = positive.get("audit_rows")
        if isinstance(audit_rows, list) and isinstance(targets, list):
            target_map = {
                item.get("instrument_id"): item.get("target_shares")
                for item in targets
                if isinstance(item, dict)
            }
            for row in audit_rows:
                if not isinstance(row, dict):
                    continue
                instrument_id = row.get("instrument_id")
                if (
                    instrument_id in target_map
                    and row.get("target_shares")
                    != target_map[instrument_id]
                ):
                    failures.append(
                        "positive_weight handoff audit/target mismatch for "
                        f"{instrument_id}"
                    )
    all_cash = handoff["all_cash"]
    if isinstance(all_cash, dict):
        all_cash_instruction = all_cash.get("instruction")
        if all_cash_instruction not in (None, {}) and (
            not isinstance(all_cash_instruction, dict)
            or all_cash_instruction.get("targets")
        ):
            failures.append("all-cash handoff must carry no share targets")
    else:
        failures.append("all_cash handoff must be an object")
    return tuple(failures)


def _v021_rebinding_failures(run_dir: Path) -> tuple[str, ...]:
    """Re-bind composite readiness rows to the smoke sub-conditions."""
    failures: list[str] = []
    checks_path = run_dir / "readiness_checks.csv"
    try:
        with checks_path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, KeyError) as exc:
        return (f"readiness CSV parse failed during rebinding: {exc}",)

    smoke_failures = _v021_smoke_failures(run_dir)
    try:
        smoke = json.loads((run_dir / "order_path_smoke.json").read_text())
        scenarios = smoke.get("scenarios") or {}
    except (OSError, ValueError):
        scenarios = {}
    try:
        inventory = json.loads((run_dir / "input_inventory.json").read_text())
        suspensions = (
            inventory.get("partition_families", {}).get("suspensions", {})
        )
        raw_clean = bool(
            suspensions.get("missing_files") == 0
            and suspensions.get("all_schemas_valid") is True
            and suspensions.get("row_audit", {}).get("all_rows_clean") is True
        )
    except (OSError, ValueError):
        raw_clean = False
    derived = v021_composite_statuses(scenarios, raw_clean)
    # the handoff row is bound to the deep handoff validation
    derived["target_portfolio_handoff"] = (
        "ready" if not _v021_handoff_failures(run_dir) else "blocked"
    )
    for row in rows:
        expected = derived.get(row["check_id"])
        if expected is not None and row["status"] != expected:
            failures.append(
                f"readiness row {row['check_id']} claims {row['status']} "
                f"but its evidence sub-conditions derive {expected}"
            )
    failures.extend(smoke_failures)
    return tuple(failures)


def execution_readiness_semantic_failures(
    run_dir: str | Path,
    schema: str = EXECUTION_READINESS_SCHEMA,
) -> tuple[str, ...]:
    """Re-derive every readiness domain semantic from the bytes on disk."""
    run_dir = Path(run_dir)
    expected_check_ids = readiness_check_ids(schema)
    expected_framework_ids = framework_check_ids(schema)
    failures: list[str] = []
    payloads: dict[str, Any] = {}
    json_names = [
        "summary.json",
        "rule_inventory.json",
        "handoff_smoke.json",
        "input_inventory.json",
    ]
    if schema == EXECUTION_READINESS_SCHEMA_V0_2_1:
        json_names += [
            "order_path_smoke.json",
            "transaction_fault_injection.json",
            "fee_reservation_reconciliation.json",
        ]
    for name in json_names:
        try:
            payloads[name] = json.loads((run_dir / name).read_text())
        except (OSError, ValueError) as exc:
            failures.append(f"{name} semantic parse failed: {exc}")
    # the performance-field scan covers EVERY json file in the directory
    for name in sorted(path.name for path in run_dir.glob("*.json")):
        if name in payloads:
            continue
        try:
            payloads[name] = json.loads((run_dir / name).read_text())
        except (OSError, ValueError) as exc:
            failures.append(f"{name} semantic parse failed: {exc}")
    keys = {key for payload in payloads.values() for key in _walk_keys(payload)}
    forbidden = sorted(keys & _PERFORMANCE_KEYS)
    if forbidden:
        failures.append(f"performance fields are prohibited: {forbidden}")

    summary = payloads.get("summary.json", {})
    gates = summary.get("gates")
    expected_gate_names = {
        "framework_valid",
        "historical_execution_ready",
        "paper_execution_ready",
        "live_execution_ready",
    }
    if not isinstance(gates, dict) or set(gates) != expected_gate_names:
        failures.append("summary readiness gates are missing or non-canonical")
    elif any(not isinstance(value, bool) for value in gates.values()):
        failures.append("summary readiness gates must be booleans")
    if summary.get("analysis_type") != "execution_readiness":
        failures.append("summary analysis_type must be execution_readiness")
    claims = summary.get("claims", {})
    for claim in (
        "performance_claim",
        "fill_claim",
        "order_submission",
        "canonical_data_written",
        "external_provider_called",
    ):
        if claims.get(claim) is not False:
            failures.append(f"summary claim {claim} must be explicitly false")

    try:
        checks_path = run_dir / "readiness_checks.csv"
        with checks_path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        columns = tuple(rows[0].keys()) if rows else ()
        if columns != READINESS_CHECK_COLUMNS:
            failures.append("readiness check columns are non-canonical")
        allowed = {status.value for status in ReadinessStatus}
        if any(row["status"] not in allowed for row in rows):
            failures.append("readiness check contains an invalid status")
        if len({row["check_id"] for row in rows}) != len(rows):
            failures.append("readiness check ids are not unique")
        if tuple(row["check_id"] for row in rows) != expected_check_ids:
            failures.append("readiness check inventory or order is non-canonical")
        if summary.get("check_count") != len(rows):
            failures.append("summary check_count does not match readiness rows")
        csv_keys: set[str] = set()
        for row in rows:
            try:
                csv_keys.update(_walk_keys(json.loads(row["evidence_json"])))
            except ValueError as exc:
                failures.append(
                    f"readiness evidence_json is invalid for {row['check_id']}: {exc}"
                )
        csv_forbidden = sorted(csv_keys & _PERFORMANCE_KEYS)
        if csv_forbidden:
            failures.append(
                f"performance fields are prohibited in readiness evidence: {csv_forbidden}"
            )
        observed_counts = {
            status.value: sum(row["status"] == status.value for row in rows)
            for status in ReadinessStatus
        }
        if summary.get("status_counts") != observed_counts:
            failures.append("summary status_counts do not match readiness rows")
        by_id = {row["check_id"]: row for row in rows}
        framework_valid = all(
            by_id[check_id]["status"] in {"ready", "partial"}
            for check_id in expected_framework_ids
            if check_id in by_id
        ) and expected_framework_ids <= set(by_id)

        def gate_ready(name: str) -> bool:
            scoped = [
                row for row in rows if name in row["critical_for"].split("|")
            ]
            return bool(scoped) and all(
                row["status"] in {"ready", "not_applicable"} for row in scoped
            )

        derived_gates = {
            "framework_valid": framework_valid,
            "historical_execution_ready": gate_ready("historical"),
            "paper_execution_ready": framework_valid and gate_ready("paper"),
            "live_execution_ready": (
                framework_valid and gate_ready("paper") and gate_ready("live")
            ),
        }
        if gates != derived_gates:
            failures.append("summary readiness gates do not match check evidence")
    except (OSError, KeyError) as exc:
        failures.append(f"readiness CSV semantic parse failed: {exc}")

    handoff = payloads.get("handoff_smoke.json", {})
    handoff_variants = (
        list(handoff.values())
        if {"all_cash", "positive_weight"} <= set(handoff)
        else [handoff]
    )
    for variant in handoff_variants:
        if not isinstance(variant, dict):
            failures.append("handoff smoke variant must be an object")
            continue
        if variant.get("is_order_submission") is not False:
            failures.append("handoff smoke must deny order submission")
        if variant.get("is_fill_evidence") is not False:
            failures.append("handoff smoke must deny fill evidence")
    inventory = payloads.get("input_inventory.json", {})
    if inventory.get("stable_during_audit") is not True:
        failures.append("input inventory was not stable during audit")

    if schema == EXECUTION_READINESS_SCHEMA_V0_2_1:
        failures.extend(_v021_smoke_failures(run_dir))
        failures.extend(_v021_handoff_failures(run_dir))
        failures.extend(_v021_rebinding_failures(run_dir))
        fault = payloads.get("transaction_fault_injection.json", {})
        if not isinstance(fault, dict) or fault.get("all_passed") is not True:
            failures.append(
                "transaction fault injection evidence must show all_passed"
            )
        fee = payloads.get("fee_reservation_reconciliation.json", {})
        if not isinstance(fee, dict) or fee.get("reconciled") is not True:
            failures.append(
                "fee/reservation reconciliation evidence must show "
                "reconciled=true"
            )
    return tuple(failures)


def validate_execution_readiness_semantics(
    contract: ArtifactContract,
    run_dir: Path,
) -> tuple[str, ...]:
    """Contract-level domain validator bound to the readiness contract."""
    return execution_readiness_semantic_failures(
        run_dir, schema=contract.schema or EXECUTION_READINESS_SCHEMA
    )


def verify_execution_readiness_artifact(
    run_dir: str | Path,
    *,
    expected_run_id: str | None = None,
    expected_head: str | None = None,
    expected_schema: str = EXECUTION_READINESS_SCHEMA,
    formal: bool = True,
) -> dict[str, Any]:
    """Verify generic integrity plus readiness-specific semantics.

    The domain semantics are part of the contract, so this single call
    enforces them in formal mode (after promotion, before the completion
    marker is accepted), in preflight mode, and from the independent
    external verifier.
    """
    return verify_formal_artifact(
        Path(run_dir),
        execution_readiness_artifact_contract(expected_schema),
        expected_run_id=expected_run_id,
        expected_head=expected_head,
        expected_schema=expected_schema,
        formal=formal,
    )
