import json
from pathlib import Path

import pytest

from quantlab.daily.configuration import create_daily_user_config


def test_user_daily_config_is_versioned_and_idempotent(tmp_path: Path) -> None:
    kwargs = {
        "strategy": "transparent_combo_candidate",
        "target_count": 15,
        "max_weight_per_name": 0.05,
        "allowed_boards": ["科创板", "主板"],
        "config_root": tmp_path,
    }
    first = create_daily_user_config(**kwargs)
    before = first.read_bytes()
    second = create_daily_user_config(**kwargs)
    assert second == first
    assert second.read_bytes() == before
    config = json.loads(first.read_text())
    assert config["score_definition"] == "transparent_combo_v1"
    assert config["model_status"] == "candidate_not_promoted_test_observed_portfolio_audited"
    assert config["allowed_boards"] == ["主板", "科创板"]
    assert config["performance_claim"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "strategy": "unknown",
            "target_count": 20,
            "max_weight_per_name": 0.05,
            "allowed_boards": ["主板"],
        },
        {
            "strategy": "baseline_reversal",
            "target_count": 0,
            "max_weight_per_name": 0.05,
            "allowed_boards": ["主板"],
        },
        {
            "strategy": "baseline_reversal",
            "target_count": 20,
            "max_weight_per_name": 0.5,
            "allowed_boards": ["主板"],
        },
        {
            "strategy": "baseline_reversal",
            "target_count": 20,
            "max_weight_per_name": 0.05,
            "allowed_boards": [],
        },
    ],
)
def test_invalid_user_config_rejected(tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        create_daily_user_config(**kwargs, config_root=tmp_path)
