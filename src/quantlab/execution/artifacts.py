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
    ReadinessStatus,
    framework_check_ids,
    readiness_check_ids,
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


def execution_readiness_artifact_contract(
    schema: str = EXECUTION_READINESS_SCHEMA,
) -> ArtifactContract:
    """Contract whose domain validator enforces readiness semantics on the
    actual directory at preflight, post-promotion formal verification, and
    external verification. The v0.2 schema extends the inventory with the
    order-path smoke evidence."""
    return ArtifactContract(
        name="execution_readiness",
        schema=schema,
        groups={},
        top_level=(
            EXECUTION_READINESS_TOP_LEVEL_V0_2
            if schema == "execution_readiness_v0_2"
            else EXECUTION_READINESS_TOP_LEVEL
        ),
        semantic_validators=(validate_execution_readiness_semantics,),
    )


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower()
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


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
    json_names = (
        "summary.json",
        "rule_inventory.json",
        "handoff_smoke.json",
        "input_inventory.json",
    )
    for name in json_names:
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

    checks_path = run_dir / "readiness_checks.csv"
    try:
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
        if variant.get("is_order_submission") is not False:
            failures.append("handoff smoke must deny order submission")
        if variant.get("is_fill_evidence") is not False:
            failures.append("handoff smoke must deny fill evidence")
    inventory = payloads.get("input_inventory.json", {})
    if inventory.get("stable_during_audit") is not True:
        failures.append("input inventory was not stable during audit")
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
