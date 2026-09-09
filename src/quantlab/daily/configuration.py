"""Immutable user-selected Daily v1 configuration versions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantlab.daily.service import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    SUPPORTED_BOARDS,
    _atomic_write_text,
)

DEFAULT_USER_CONFIG_ROOT = PROJECT_ROOT / "data" / "products" / "daily_configs"


def create_daily_user_config(
    *,
    strategy: str,
    target_count: int,
    max_weight_per_name: float,
    allowed_boards: list[str],
    config_root: Path = DEFAULT_USER_CONFIG_ROOT,
) -> Path:
    if strategy == "baseline_reversal":
        strategy_fields = {
            "strategy_id": "momentum_20d_reversal_example",
            "model_status": "baseline_research_example_test_observed",
            "score_definition": "return_20d",
            "score_direction": "lower_is_better",
            "test_observed": True,
        }
    elif strategy == "transparent_combo_candidate":
        strategy_fields = {
            "strategy_id": "transparent_combo_v1_candidate",
            "model_status": "candidate_not_promoted_no_cost_control_closure",
            "score_definition": "transparent_combo_v1",
            "score_direction": "higher_is_better",
            "test_observed": True,
        }
    else:
        raise ValueError("unsupported strategy selection")
    if isinstance(target_count, bool) or not 1 <= target_count <= 100:
        raise ValueError("target_count must be between 1 and 100")
    if not 0 < max_weight_per_name <= 0.2:
        raise ValueError("max_weight_per_name must be in (0, 0.2]")
    boards = sorted(set(allowed_boards), key=SUPPORTED_BOARDS.index)
    if not boards or not set(boards).issubset(SUPPORTED_BOARDS):
        raise ValueError("at least one supported board is required")
    core = {
        **strategy_fields,
        "parent_config": "daily_mvp_v1",
        "universe": "V1_SH_SZ_A_share_user_board_subset",
        "target_count": target_count,
        "max_weight_per_name": max_weight_per_name,
        "gross_exposure": 1.0,
        "allowed_boards": boards,
        "tie_policy": "alpha_score_then_instrument_id",
        "performance_claim": False,
        "historical_comparability": (
            "new user configuration; not directly comparable to frozen baseline artifacts"
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    payload = {
        **core,
        "config_id": f"daily_user_{fingerprint[:12]}",
        "config_fingerprint": fingerprint,
    }
    path = config_root / f"{payload['config_id']}.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError("immutable daily config id collision")
        return path
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return path


def list_daily_configs(config_root: Path = DEFAULT_USER_CONFIG_ROOT) -> list[Path]:
    return [DEFAULT_CONFIG_PATH] + sorted(config_root.glob("daily_user_*.json"))
