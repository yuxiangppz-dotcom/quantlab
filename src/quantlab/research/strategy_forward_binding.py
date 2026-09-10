"""Fail-closed binding between strategy governance and Forward Shadow identities.

Strategy ids and Forward Shadow model ids are deliberately allowed to differ.
This module makes that difference explicit and auditable rather than relying on
name similarity. It never interprets performance and never grants strategy
authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from quantlab.data.models import DataValidationError
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
    FORWARD_EVIDENCE_ACCUMATING,
    FORWARD_SHADOW,
    USER_APPROVED,
    load_strategy_registry,
)

_FORWARD_STATES = {
    FORWARD_SHADOW,
    FORWARD_EVIDENCE_ACCUMATING,
    ELIGIBLE_FOR_USER_REVIEW,
    USER_APPROVED,
}


@dataclass(frozen=True)
class StrategyForwardBinding:
    strategy_id: str
    strategy_version: str
    role: str
    forward_model_id: str
    source_column: str
    direction: str


@dataclass(frozen=True)
class StrategyForwardBindingAudit:
    forward_config_id: str
    forward_config_ref: str
    bindings: tuple[StrategyForwardBinding, ...]
    binding_fingerprint: str


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_repo_relative_ref(value: object) -> str:
    ref = _nonempty(value, "strategy registry forward_config_ref")
    parsed = PurePosixPath(ref)
    if (
        not parsed.parts
        or parsed.is_absolute()
        or ".." in parsed.parts
        or parsed.parts[0] == "."
    ):
        raise DataValidationError(
            "forward_config_ref must be a safe repository-relative path"
        )
    return ref


def validate_strategy_forward_binding(
    registry_path: Path,
    forward_config_path: Path,
) -> StrategyForwardBindingAudit:
    """Validate one-to-one registry ownership of every configured shadow model.

    The registry file is expected to live under the repository ``config``
    directory. Its top-level ``forward_config_ref`` is resolved relative to the
    repository root inferred from that location, which prevents a registry from
    claiming evidence from a different file with the same basename.
    """
    registry_path = Path(registry_path)
    forward_config_path = Path(forward_config_path)
    try:
        raw_registry = json.loads(registry_path.read_text(encoding="utf-8"))
        forward_config = json.loads(forward_config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataValidationError(
            "strategy/forward binding input is unreadable or invalid JSON"
        ) from exc

    if raw_registry.get("schema") != "quantlab_strategy_registry_v1":
        raise DataValidationError(
            "unsupported strategy registry schema for forward binding"
        )
    if forward_config.get("schema") != "quantlab_forward_shadow_config_v1":
        raise DataValidationError("unsupported Forward Shadow config schema")

    entries = load_strategy_registry(registry_path)
    raw_entries = raw_registry.get("strategies")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(entries):
        raise DataValidationError(
            "strategy registry entries changed during binding validation"
        )

    forward_ref = _safe_repo_relative_ref(raw_registry.get("forward_config_ref"))
    repo_root = registry_path.parent.parent
    expected_forward_path = (
        repo_root / Path(*PurePosixPath(forward_ref).parts)
    ).resolve()
    if forward_config_path.resolve() != expected_forward_path:
        raise DataValidationError(
            "forward_config_path does not match strategy registry forward_config_ref"
        )
    if any(
        forward_ref not in entry.evidence_refs
        for entry in entries
        if entry.status in _FORWARD_STATES
    ):
        raise DataValidationError(
            "every forward-observation strategy must reference the configured "
            "Forward Shadow file"
        )

    config_id = _nonempty(
        forward_config.get("config_id"),
        "Forward Shadow config_id",
    )
    models = forward_config.get("models")
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(item, dict) for item in models)
    ):
        raise DataValidationError(
            "Forward Shadow config models must be a non-empty object list"
        )

    model_by_key: dict[tuple[str, str], dict] = {}
    for model in models:
        model_id = _nonempty(model.get("model_id"), "Forward Shadow model_id")
        version = _nonempty(
            model.get("version"),
            "Forward Shadow model version",
        )
        source = _nonempty(
            model.get("source_column"),
            "Forward Shadow source_column",
        )
        direction = _nonempty(
            model.get("direction"),
            "Forward Shadow direction",
        )
        if direction not in {"higher_is_better", "lower_is_better"}:
            raise DataValidationError(
                f"unsupported Forward Shadow direction: {direction!r}"
            )
        key = (model_id, version)
        if key in model_by_key:
            raise DataValidationError(
                f"duplicate Forward Shadow model identity: {key}"
            )
        model_by_key[key] = {
            **model,
            "source_column": source,
            "direction": direction,
        }

    raw_by_key: dict[tuple[str, str], dict] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise DataValidationError("strategy registry entries must be objects")
        key = (raw.get("strategy_id"), raw.get("version"))
        if key in raw_by_key:
            raise DataValidationError(
                "duplicate strategy entry during forward binding"
            )
        raw_by_key[key] = raw

    claimed_models: set[tuple[str, str]] = set()
    bindings: list[StrategyForwardBinding] = []
    for entry in entries:
        raw = raw_by_key.get(entry.key)
        if raw is None:
            raise DataValidationError(
                "typed strategy entry has no matching raw registry object"
            )
        forward_model_id = raw.get("forward_model_id")
        if entry.status not in _FORWARD_STATES:
            if forward_model_id is not None and not isinstance(
                forward_model_id,
                str,
            ):
                raise DataValidationError(
                    "forward_model_id must be a string when declared"
                )
            continue

        model_id = _nonempty(
            forward_model_id,
            "forward-observation strategy forward_model_id",
        )
        model_key = (model_id, entry.version)
        model = model_by_key.get(model_key)
        if model is None:
            raise DataValidationError(
                "strategy forward binding does not resolve to a configured model: "
                f"{entry.strategy_id}@{entry.version} -> "
                f"{model_id}@{entry.version}"
            )
        if model_key in claimed_models:
            raise DataValidationError(
                "Forward Shadow model is claimed by multiple strategies: "
                f"{model_key}"
            )
        claimed_models.add(model_key)

        expected_score_source = (
            f"{model['source_column']} {model['direction']}"
        )
        if entry.score_source != expected_score_source:
            raise DataValidationError(
                "strategy score_source does not match Forward Shadow model contract: "
                f"{entry.score_source!r} != {expected_score_source!r}"
            )
        bindings.append(
            StrategyForwardBinding(
                strategy_id=entry.strategy_id,
                strategy_version=entry.version,
                role=entry.role,
                forward_model_id=model_id,
                source_column=model["source_column"],
                direction=model["direction"],
            )
        )

    model_keys = set(model_by_key)
    if claimed_models != model_keys:
        orphaned = sorted(model_keys - claimed_models)
        missing = sorted(claimed_models - model_keys)
        raise DataValidationError(
            "strategy/Forward Shadow model ownership mismatch: "
            f"orphaned={orphaned}, missing={missing}"
        )

    ordered = tuple(
        sorted(
            bindings,
            key=lambda item: (item.strategy_id, item.strategy_version),
        )
    )
    fingerprint_payload = {
        "schema": "quantlab_strategy_forward_binding_v1",
        "forward_config_id": config_id,
        "forward_config_ref": forward_ref,
        "bindings": [asdict(item) for item in ordered],
    }
    return StrategyForwardBindingAudit(
        forward_config_id=config_id,
        forward_config_ref=forward_ref,
        bindings=ordered,
        binding_fingerprint=_canonical_hash(fingerprint_payload),
    )
