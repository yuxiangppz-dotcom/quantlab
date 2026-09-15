"""Read-only composition of strategy, evidence and Forward Shadow status."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import (
    build_evidence_catalog,
    summarize_evidence_catalog,
)
from quantlab.research.forward_shadow_analytics import summarize_forward_shadow
from quantlab.research.strategy_readiness import (
    build_registered_strategy_readiness,
    readiness_summary,
)


def _sha256(path: Path, label: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DataValidationError(f"cannot read required {label}: {path}") from exc
    return hashlib.sha256(raw).hexdigest()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_research_status(
    *,
    registry_path: Path,
    forward_config_path: Path,
    experiment_root: Path,
    shadow_root: Path,
    evaluation_root: Path | None = None,
) -> dict:
    """Compose claim-neutral readiness from existing immutable local evidence.

    Missing optional evidence roots are reported as absent and yield zero
    evidence. Existing malformed artifacts propagate an error and are never
    silently skipped.
    """

    evaluation_root = evaluation_root or shadow_root / "evaluations"
    registry_sha256 = _sha256(registry_path, "strategy registry")
    forward_config_sha256 = _sha256(forward_config_path, "Forward Shadow config")

    # Legacy experiment summaries and malformed artifacts are classified and
    # reported instead of crashing the whole status view; the strict entry
    # remains for consumers that must hard-fail.
    catalog = build_evidence_catalog(experiment_root, strict=False)
    shadow_summaries = summarize_forward_shadow(
        shadow_root,
        evaluation_root=evaluation_root,
    )
    reports = build_registered_strategy_readiness(
        registry_path,
        forward_config_path,
        shadow_summaries=shadow_summaries,
        evidence_catalog=catalog,
    )

    core = {
        "schema": "quantlab_research_status_v1",
        "sources": {
            "strategy_registry_sha256": registry_sha256,
            "forward_config_sha256": forward_config_sha256,
            "experiment_root_exists": experiment_root.is_dir(),
            "shadow_root_exists": shadow_root.is_dir(),
            "evaluation_root_exists": evaluation_root.is_dir(),
        },
        "evidence_catalog": {
            "summary": summarize_evidence_catalog(catalog),
            "entries": [asdict(item) for item in catalog.entries],
            "issues": [asdict(item) for item in catalog.issues],
        },
        "incompatible_artifacts": [
            {
                "relative_path": issue.relative_path,
                "classification": issue.classification,
                "message": issue.message,
            }
            for issue in catalog.issues
        ],
        "forward_shadow": {
            "summaries": [asdict(item) for item in shadow_summaries],
            "label_semantics": "overlapping_forward_labels_are_diagnostics_not_portfolio_nav",
        },
        "strategy_readiness": {
            "summary": readiness_summary(reports),
            "strategies": [asdict(item) for item in reports],
        },
        "performance_claim": False,
        "strategy_promotion_authority": False,
        "broker_order_authority": False,
        "claim": "read_only_structural_research_status_not_performance_or_execution_authority",
    }
    # Overall naming describes report integrity, never strategy readiness.
    has_unrecognized = any(
        issue.classification == "unrecognized_format" for issue in catalog.issues
    )
    has_malformed = any(
        issue.classification == "malformed_evidence" for issue in catalog.issues
    )
    has_legacy = any(
        issue.classification == "identified_legacy" for issue in catalog.issues
    )
    if has_unrecognized or has_malformed:
        core["overall_status"] = "report_has_evidence_issues"
    elif has_legacy:
        core["overall_status"] = "report_has_legacy_artifacts"
    else:
        core["overall_status"] = "clean"
    return {**core, "status_fingerprint": _canonical_hash(core)}


def format_research_status(payload: dict) -> str:
    """Render a compact human view while preserving explicit blockers."""

    sources = payload["sources"]
    catalog = payload["evidence_catalog"]["summary"]
    strategies = payload["strategy_readiness"]["strategies"]
    incompatible = payload.get("incompatible_artifacts", [])
    lines = [
        "QuantLab research status",
        f"  overall status: {payload.get('overall_status', 'clean')}",
        f"  evidence root: {'present' if sources['experiment_root_exists'] else 'missing'}",
        f"  evidence artifacts: {catalog['entry_count']}",
        f"  Forward Shadow root: {'present' if sources['shadow_root_exists'] else 'missing'}",
        f"  status fingerprint: {payload['status_fingerprint']}",
        "  strategies:",
    ]
    for strategy in strategies:
        blockers = strategy["blockers"]
        blocker_text = ", ".join(blockers) if blockers else "none"
        lines.extend(
            [
                (
                    f"    {strategy['strategy_id']}@{strategy['version']}: "
                    f"{strategy['registry_status']}"
                ),
                f"      blockers: {blocker_text}",
                (
                    "      user approved: "
                    f"{strategy['strategy_approval_authority']}; "
                    f"broker authority: {strategy['broker_order_authority']}"
                ),
            ]
        )
    if incompatible:
        lines.append("  incompatible artifacts:")
        for artifact in incompatible:
            lines.append(
                f"    {artifact['relative_path']}: "
                f"{artifact['classification']} - {artifact['message']}"
            )
    lines.append("  performance claim: false")
    return "\n".join(lines)
