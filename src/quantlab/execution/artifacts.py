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
    EXECUTION_READINESS_SCHEMA_V0_2_2,
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
EXECUTION_READINESS_TOP_LEVEL_V0_2_2 = EXECUTION_READINESS_TOP_LEVEL_V0_2_1
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
    if schema == EXECUTION_READINESS_SCHEMA_V0_2_2:
        return EXECUTION_READINESS_TOP_LEVEL_V0_2_2
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


def _json_exact_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON-domain values without Python's bool/int coercion."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _json_exact_equal(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _json_exact_equal(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _read_readiness_rows(
    run_dir: Path,
) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    """Parse CSV rows only when the complete canonical shape is usable."""
    checks_path = run_dir / "readiness_checks.csv"
    try:
        with checks_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            columns = tuple(reader.fieldnames or ())
    except (OSError, UnicodeError, csv.Error) as exc:
        return [], (f"readiness CSV semantic parse failed: {exc}",)
    if columns != READINESS_CHECK_COLUMNS:
        return [], ("readiness check columns are non-canonical",)
    if not rows:
        return [], ("readiness CSV contains no data rows",)
    for row_number, row in enumerate(rows, start=1):
        if tuple(row) != READINESS_CHECK_COLUMNS or any(
            not isinstance(row.get(column), str)
            for column in READINESS_CHECK_COLUMNS
        ):
            return [], (
                "readiness CSV row "
                f"{row_number} must contain exactly canonical usable strings",
            )
    return rows, ()


_V022_EXTRA_TRUE_FIELDS = (
    "same_state_batch_committed",
    "sell_share_reservation",
    "typed_quote_lineage",
    "stale_batch_rejected",
    "day_submission_date_bound",
    "authority_lineage_bound",
)


def _v021_smoke_failures(run_dir: Path, schema: str | None = None) -> tuple[str, ...]:
    """Deep canonical validation of the order-path smoke evidence.

    v0.2.2 extends the canonical scenario set with the same-state authority
    keys; the v0.2.1 key set stays frozen for the v0.2.1 artifacts.
    """
    true_fields = _V021_SMOKE_TRUE_FIELDS
    fields = _V021_SMOKE_FIELDS
    if schema == EXECUTION_READINESS_SCHEMA_V0_2_2:
        true_fields = _V021_SMOKE_TRUE_FIELDS + _V022_EXTRA_TRUE_FIELDS
        fields = _V021_SMOKE_FIELDS + _V022_EXTRA_TRUE_FIELDS
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
    if scenario_keys != set(fields):
        missing = sorted(set(fields) - scenario_keys)
        extra = sorted(scenario_keys - set(fields))
        failures.append(
            "order_path_smoke scenarios are non-canonical: "
            f"missing={missing} extra={extra}"
        )
        return tuple(failures)
    for field in true_fields:
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


def _v021_rebinding_failures(
    run_dir: Path,
    rows: list[dict[str, str]] | None = None,
) -> tuple[str, ...]:
    """Re-bind composite readiness rows to the smoke sub-conditions."""
    failures: list[str] = []
    if rows is None:
        rows, structural_failures = _read_readiness_rows(run_dir)
        if structural_failures:
            return structural_failures

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
    return tuple(failures)


_V022_SUMMARY_CLAIMS = (
    "performance_claim",
    "fill_claim",
    "order_submission",
    "external_broker_submission",
    "canonical_data_written",
    "external_provider_called",
)
# -- canonical value-binding contract for every framework READY/PARTIAL
# row's evidence: fixed canonical values, evidence fields bound field by
# field to the smoke scenario they claim to derive from, and the exact
# required_subconditions disclosure for composite rows. Any missing,
# extra (undeclared smoke-derived), retyped, reordered, or contradictory
# value is a semantic failure; a READY claim requires every bound child
# to hold its ready value.
_V022_ROW_CONST_BINDINGS: dict[str, dict[str, Any]] = {
    "artifact_protocol": {
        "publication": "staging -> manifest -> preflight -> promotion -> "
                       "marker",
    },
    "target_portfolio_handoff": {"price_role": "planning_only"},
    "order_and_ledger_contracts": {
        "money_unit": "integer_fen",
        "timestamp_policy": "timezone_aware",
    },
    "production_submission_path_reachable": {
        "engine": "AShareConstraintEngine",
        "derived_status": "validated",
        "fillability_unknown_allowed": True,
    },
    "calendar_derived_t_plus_one": {"calendar_bound": True},
    "fee_budget_limit_protection": {
        "fee_cap_semantics": "cumulative_order_lifetime",
        "typed_fee_quote_required": True,
    },
    "t_plus_one_sellability": {
        "model": "position_lot",
        "same_day_sale_blocked": True,
        "calendar_bound_next_session_derivation": True,
    },
}
_V022_ROW_SMOKE_BINDINGS: dict[str, dict[str, str]] = {
    "account_aware_order_planning": {
        "plan_id_deterministic": "order_plan_deterministic",
        "independent_order_price_evidence": "buy_limit_from_order_price_evidence",
        "omitted_held_name_exits": "omitted_held_name_exits",
        "non_conforming_delta_blocks": "non_conforming_delta_blocks",
        "buys_funded_from_available_cash_only": "buys_funded_from_available_cash_only",
    },
    "atomic_cash_reservation": {
        "contention_blocked": "aggregate_cash_contention_blocked",
        "partial_fill_drawdown": "partial_fill_drawdown",
        "full_fill_release": "full_fill_release",
        "cancel_release": "cancel_release",
        "multi_partial_fee_reconciliation": "multi_partial_fee_reconciliation",
        "batch_rollback_preserves_state": "batch_rollback_preserves_state",
    },
    "atomic_share_reservation": {
        "contention_blocked": "share_contention_blocked",
    },
    "stale_assessment_rejection": {
        "stale_rejected": "stale_assessment_rejected",
    },
    "day_trade_date_binding": {
        "wrong_trade_date_rejected": "wrong_trade_date_rejected",
    },
    "calendar_derived_t_plus_one": {
        "weekend_exact": "weekend_t_plus_one_exact",
        "holiday_exact": "holiday_t_plus_one_exact",
        "incomplete_coverage_fail_closed": "missing_next_session_fail_closed",
    },
    "t_plus_one_sellability": {
        "weekend_exact": "weekend_t_plus_one_exact",
        "holiday_exact": "holiday_t_plus_one_exact",
        "incomplete_coverage_fail_closed": "missing_next_session_fail_closed",
    },
    "transactional_submission_atomicity": {
        "batch_rollback_preserves_state": "batch_rollback_preserves_state",
        "transactional_submission_committed": "transactional_submission_committed",
    },
    "fee_budget_limit_protection": {
        "multi_partial_fee_reconciliation": "multi_partial_fee_reconciliation",
        "full_fill_release": "full_fill_release",
    },
    "plan_to_order_lineage": {
        "plan_to_order_lineage": "plan_to_order_lineage",
        "transactional_submission_committed": "transactional_submission_committed",
        "external_broker_submission": "external_broker_submission",
    },
    "submission_authority_and_day_binding": {
        "authority_lineage_bound": "authority_lineage_bound",
        "day_submission_date_bound": "day_submission_date_bound",
        "same_state_batch_committed": "same_state_batch_committed",
        "stale_batch_rejected": "stale_batch_rejected",
        "typed_quote_lineage": "typed_quote_lineage",
    },
}
# children whose READY value is explicitly False (denials stay denials)
_V022_FALSE_WHEN_READY = frozenset({"external_broker_submission"})
# composite rows that must disclose exactly this ordered subcondition set
_V022_ROW_REQUIRED_SUBCONDITIONS: dict[str, tuple[str, ...]] = {
    "account_aware_order_planning": (
        "order_plan_deterministic",
        "buy_limit_from_order_price_evidence",
        "omitted_held_name_exits",
        "non_conforming_delta_blocks",
        "buys_funded_from_available_cash_only",
    ),
    "atomic_cash_reservation": (
        "aggregate_cash_contention_blocked",
        "partial_fill_drawdown",
        "full_fill_release",
        "cancel_release",
        "multi_partial_fee_reconciliation",
        "batch_rollback_preserves_state",
    ),
    "calendar_derived_t_plus_one": (
        "weekend_t_plus_one_exact",
        "holiday_t_plus_one_exact",
        "missing_next_session_fail_closed",
    ),
    "submission_authority_and_day_binding": (
        "authority_lineage_bound",
        "day_submission_date_bound",
        "same_state_batch_committed",
        "stale_batch_rejected",
        "typed_quote_lineage",
    ),
    "t_plus_one_sellability": (
        "weekend_t_plus_one_exact",
        "holiday_t_plus_one_exact",
        "missing_next_session_fail_closed",
    ),
}
# rows whose evidence carries a derived non-boolean binding handled
# specially: the handoff row binds to the deep handoff validation, the
# submission row to the smoke gate-matrix keys, and the transaction row
# embeds the full smoke fault-injection matrix
_V022_ROW_SPECIAL_KEYS: dict[str, tuple[str, ...]] = {
    "target_portfolio_handoff": ("smoke_valid",),
    "production_submission_path_reachable": ("gates_rechecked",),
    "transactional_submission_atomicity": ("fault_injection_matrix",),
}
# status-only children: scenarios a READY claim requires even though the
# row's evidence does not carry them as fields
_V022_ROW_STATUS_SCENARIOS: dict[str, tuple[str, ...]] = {
    "production_submission_path_reachable": (
        "production_submission_path_reachable",
        "gating_dimensions_fail_closed",
    ),
}
_V022_FAULT_KEYS = (
    "synthetic",
    "non_trading",
    "external_broker_submission",
    "batch_first_submission_runtimeerror_restored",
    "batch_middle_submission_keyboardinterrupt_restored",
    "batch_last_submission_runtimeerror_restored",
    "invariant_failure_after_mutation_restored",
    "single_append_baseexception_restored",
    "all_passed",
)
_V022_FEE_KEYS = (
    "reconciled",
    "fee_cap_semantics",
    "typed_fee_quote_required",
    "multi_partial_fee_reconciliation",
    "full_fill_release",
    "partial_fill_drawdown",
    "cancel_release",
)


def _v022_deep_failures(
    run_dir: Path,
    payloads: dict[str, Any],
    summary: dict,
    rows: list[dict],
) -> tuple[str, ...]:
    """v0.2.2: exact canonical key sets, child-wise aggregates, and
    field-level cross-binding of every framework readiness row."""
    failures: list[str] = []

    # -- summary claims: EXACT canonical set, every one explicitly false
    claims = summary.get("claims")
    if not isinstance(claims, dict) or set(claims) != set(
        _V022_SUMMARY_CLAIMS
    ):
        failures.append(
            "summary claims must be the exact canonical set "
            f"{sorted(_V022_SUMMARY_CLAIMS)}"
        )
    else:
        for claim, value in claims.items():
            if value is not False:
                failures.append(f"summary claim {claim} must be false")

    # -- transaction fault injection: exact key set, child-wise aggregate
    fault = payloads.get("transaction_fault_injection.json")
    if not isinstance(fault, dict):
        failures.append("transaction_fault_injection.json must be an object")
    else:
        if set(fault) != set(_V022_FAULT_KEYS):
            failures.append(
                "transaction fault injection keys are non-canonical: "
                f"missing={sorted(set(_V022_FAULT_KEYS) - set(fault))} "
                f"extra={sorted(set(fault) - set(_V022_FAULT_KEYS))}"
            )
        children = all(
            fault.get(key) is True
            for key in _V022_FAULT_KEYS
            if key not in ("all_passed", "external_broker_submission")
        )
        if fault.get("all_passed") is not children or not children:
            failures.append(
                "transaction fault injection all_passed must equal the "
                "recomputed AND of every child scenario"
            )
        if fault.get("external_broker_submission") is not False:
            failures.append(
                "transaction fault injection must deny external broker "
                "submission"
            )

    # -- fee reconciliation: exact key set, child-wise aggregate
    fee = payloads.get("fee_reservation_reconciliation.json")
    if not isinstance(fee, dict):
        failures.append("fee_reservation_reconciliation.json must be an object")
    else:
        if set(fee) != set(_V022_FEE_KEYS):
            failures.append(
                "fee reconciliation keys are non-canonical: "
                f"missing={sorted(set(_V022_FEE_KEYS) - set(fee))} "
                f"extra={sorted(set(fee) - set(_V022_FEE_KEYS))}"
            )
        children = all(
            fee.get(key) is True
            for key in (
                "multi_partial_fee_reconciliation",
                "full_fill_release",
                "partial_fill_drawdown",
                "cancel_release",
            )
        )
        if fee.get("reconciled") is not children or not children:
            failures.append(
                "fee reconciliation reconciled must equal the recomputed "
                "AND of its child conditions"
            )
        if fee.get("fee_cap_semantics") != "cumulative_order_lifetime":
            failures.append(
                "fee reconciliation must freeze the cumulative "
                "order-lifetime fee-cap semantics"
            )
        if fee.get("typed_fee_quote_required") is not True:
            failures.append(
                "fee reconciliation must require typed fee quotes"
            )

    # -- same-state smoke section: exact keys and truth values
    try:
        smoke = json.loads((run_dir / "order_path_smoke.json").read_text())
        scenarios = smoke.get("scenarios") or {}
    except (OSError, ValueError):
        scenarios = {}
    for key in (
        "same_state_batch_committed",
        "sell_share_reservation",
        "typed_quote_lineage",
        "stale_batch_rejected",
        "day_submission_date_bound",
        "authority_lineage_bound",
    ):
        if scenarios.get(key) is not True:
            failures.append(f"smoke scenario {key} must be true")

    # -- readiness CSV: every framework READY/PARTIAL row's evidence must
    # satisfy the canonical value-binding contract, field by field,
    # against the smoke evidence it claims to derive from
    for row in rows:
        if "framework" not in row["critical_for"].split("|"):
            continue
        if row["status"] not in {"ready", "partial"}:
            continue
        check_id = row["check_id"]
        consts = _V022_ROW_CONST_BINDINGS.get(check_id, {})
        smoke_bindings = _V022_ROW_SMOKE_BINDINGS.get(check_id, {})
        required = _V022_ROW_REQUIRED_SUBCONDITIONS.get(check_id)
        special_keys = _V022_ROW_SPECIAL_KEYS.get(check_id, ())
        if not consts and not smoke_bindings and not special_keys:
            failures.append(
                f"readiness row {check_id} claims {row['status']} without "
                "a canonical evidence binding contract"
            )
            continue
        try:
            evidence = json.loads(row["evidence_json"])
        except ValueError:
            failures.append(
                f"readiness evidence_json is invalid for {check_id}"
            )
            continue
        if not isinstance(evidence, dict):
            failures.append(
                f"readiness evidence_json must be an object for {check_id}"
            )
            continue
        expected_keys = set(consts) | set(smoke_bindings) | set(special_keys)
        if required is not None:
            expected_keys.add("required_subconditions")
        missing = sorted(expected_keys - set(evidence))
        extra = sorted(set(evidence) - expected_keys)
        if missing:
            failures.append(
                f"readiness row {check_id} evidence is missing canonical "
                f"keys (missing required subconditions or fields): "
                f"{missing}"
            )
        if extra:
            failures.append(
                f"readiness row {check_id} carries undeclared (extra or "
                f"smoke-derived) evidence keys: {extra}"
            )
        for key, value in consts.items():
            if not _json_exact_equal(evidence.get(key), value):
                failures.append(
                    f"readiness row {check_id} evidence field {key} "
                    "contradicts the canonical value (extra, missing, or "
                    "retyped values are rejected)"
                )
        for key, scenario_key in smoke_bindings.items():
            if scenario_key not in scenarios:
                failures.append(
                    f"readiness row {check_id} discloses {key} but the "
                    f"smoke evidence lacks scenario {scenario_key}"
                )
            elif not _json_exact_equal(
                evidence.get(key), scenarios[scenario_key]
            ):
                failures.append(
                    f"readiness row {check_id} evidence field {key} "
                    f"contradicts smoke scenario {scenario_key}"
                )
        if required is not None and not _json_exact_equal(
            evidence.get("required_subconditions"), list(required)
        ):
            failures.append(
                f"readiness row {check_id} required_subconditions "
                "disclosure is missing, incomplete, extended, or reordered"
            )
        if check_id == "target_portfolio_handoff":
            handoff_ok = not _v021_handoff_failures(run_dir)
            if evidence.get("smoke_valid") is not handoff_ok:
                failures.append(
                    f"readiness row {check_id} smoke_valid contradicts "
                    "the deep handoff evidence"
                )
            if row["status"] == "ready" and not handoff_ok:
                failures.append(
                    f"readiness row {check_id} claims ready while its "
                    "handoff smoke is invalid"
                )
        if check_id == "production_submission_path_reachable":
            gate_matrix = scenarios.get("submission_gate_matrix")
            expected_gates = (
                sorted(gate_matrix) if isinstance(gate_matrix, dict) else None
            )
            if not _json_exact_equal(
                evidence.get("gates_rechecked"), expected_gates
            ):
                failures.append(
                    f"readiness row {check_id} gates_rechecked does not "
                    "match the smoke submission gate matrix"
                )
        if check_id == "transactional_submission_atomicity":
            if not _json_exact_equal(
                evidence.get("fault_injection_matrix"),
                scenarios.get("fault_injection_matrix"),
            ):
                failures.append(
                    f"readiness row {check_id} fault_injection_matrix "
                    "does not match the smoke fault-injection matrix"
                )
        if row["status"] == "ready":
            children = tuple(smoke_bindings.values()) + (
                _V022_ROW_STATUS_SCENARIOS.get(check_id, ())
            )
            not_ready = sorted(
                scenario_key
                for scenario_key in children
                if scenarios.get(scenario_key)
                is not (
                    False
                    if scenario_key in _V022_FALSE_WHEN_READY
                    else True
                )
            )
            if not_ready:
                failures.append(
                    f"readiness row {check_id} claims ready while its "
                    f"disclosed children are not all true: {not_ready}"
                )

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
    if schema in (
        EXECUTION_READINESS_SCHEMA_V0_2_1,
        EXECUTION_READINESS_SCHEMA_V0_2_2,
    ):
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

    # CSV state is initialized up front: a missing, unreadable, empty, or
    # malformed readiness_checks.csv must surface as deterministic
    # semantic failures below, never as an UnboundLocalError or an
    # incidental UnicodeDecodeError escaping this verifier
    rows, csv_structure_failures = _read_readiness_rows(run_dir)
    failures.extend(csv_structure_failures)
    if not csv_structure_failures:
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

    if schema in (
        EXECUTION_READINESS_SCHEMA_V0_2_1,
        EXECUTION_READINESS_SCHEMA_V0_2_2,
    ):
        failures.extend(_v021_smoke_failures(run_dir, schema))
        failures.extend(_v021_handoff_failures(run_dir))
        if not csv_structure_failures:
            failures.extend(_v021_rebinding_failures(run_dir, rows))
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
    if (
        schema == EXECUTION_READINESS_SCHEMA_V0_2_2
        and not csv_structure_failures
    ):
        failures.extend(
            _v022_deep_failures(run_dir, payloads, summary, rows)
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
