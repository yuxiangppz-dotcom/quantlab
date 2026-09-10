"""Product-facing read-only status over QuantLab research evidence.

The status service composes existing evidence primitives. It never creates
research evidence, changes strategy lifecycle state, or grants execution/broker
authority. Existing malformed evidence is treated as an error in this product
path rather than silently omitted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.evidence_catalog import (
    build_evidence_catalog,
    summarize_evidence_catalog,
)
from quantlab.research.forward_shadow import DEFAULT_SHADOW_ROOT
from quantlab.research.forward_shadow_analytics import summarize_forward_shadow
from quantlab.research.strategy_readiness import (
    build_strategy_readiness,
    readiness_summary,
)
from quantlab.research.strategy_registry import load_strategy_registry

DEFAULT_STRATEGY_REGISTRY = PROJECT_ROOT / "config" / "strategy_registry_v1.json"
DEFAULT_EXPERIMENT_ROOT = PROJECT_ROOT / "data" / "experiments"


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_research_status(
    *,
    registry_path: Path = DEFAULT_STRATEGY_REGISTRY,
    experiment_root: Path = DEFAULT_EXPERIMENT_ROOT,
    shadow_root: Path = DEFAULT_SHADOW_ROOT,
) -> dict:
    """Build a deterministic, claim-neutral research status snapshot.

    Missing experiment or Forward Shadow directories are represented by empty
    inventories. If a directory exists but contains malformed evidence, the
    underlying strict validators raise instead of producing a partial status.
    """
    entries = load_strategy_registry(registry_path)
    catalog = build_evidence_catalog(experiment_root, strict=True)
    shadows = summarize_forward_shadow(shadow_root)
    reports = build_strategy_readiness(
        entries,
        shadow_summaries=shadows,
        evidence_catalog=catalog,
    )
    aggregate = readiness_summary(reports)
    catalog_summary = summarize_evidence_catalog(catalog)

    core = {
        "schema": "quantlab_research_status_v1",
        "sources": {
            "strategy_registry_sha256": _sha256(registry_path),
            "evidence_catalog_fingerprint": catalog.catalog_fingerprint,
            "experiment_root_present": experiment_root.is_dir(),
            "forward_shadow_root_present": shadow_root.is_dir(),
        },
        "evidence_catalog": catalog_summary,
        "forward_shadow": [asdict(item) for item in shadows],
        "strategies": [asdict(item) for item in reports],
        "readiness": aggregate,
        "claims": {
            "performance_claim": False,
            "automatic_strategy_promotion": False,
            "execution_authority": False,
            "broker_order_authority": False,
            "forward_shadow_returns_are_overlapping_label_diagnostics": True,
        },
    }
    return {**core, "status_fingerprint": _canonical_hash(core)}


def format_research_status(payload: dict) -> str:
    """Render a compact human-readable status without performance inference."""
    lines = [
        "QuantLab research status",
        f"  strategies: {payload['readiness']['strategy_count']}",
        f"  evidence artifacts: {payload['evidence_catalog']['entry_count']}",
        f"  evidence issues: {payload['evidence_catalog']['issue_count']}",
        f"  ready for user review: {payload['readiness']['ready_for_user_review_count']}",
        f"  user approved: {payload['readiness']['user_approved_count']}",
        "  broker order authority: false",
    ]
    for item in payload["strategies"]:
        blockers = ", ".join(item["blockers"]) if item["blockers"] else "none"
        lines.extend(
            [
                f"  - {item['strategy_id']}@{item['version']}: {item['registry_status']}",
                (
                    "    forward: "
                    f"predictions={item['forward_prediction_count']} "
                    f"complete={item['forward_complete_evaluation_count']} "
                    f"pending={item['forward_pending_prediction_count']}"
                ),
                f"    blockers: {blockers}",
            ]
        )
    lines.append(f"  fingerprint: {payload['status_fingerprint']}")
    return "\n".join(lines)
