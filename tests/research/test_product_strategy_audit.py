from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quantlab.research.product_strategy_audit import (
    _load_configs,
    _product_targets,
)

DAY = date(2026, 9, 9)


def _daily_config(**updates: object) -> dict:
    payload = {
        "config_id": "test_daily",
        "strategy_id": "candidate",
        "model_status": "RESEARCH_CANDIDATE_NOT_PROMOTED",
        "score_definition": "transparent_combo_v1",
        "score_direction": "higher_is_better",
        "target_count": 2,
        "max_weight_per_name": 0.4,
        "gross_exposure": 1.0,
        "tie_policy": "alpha_score_then_instrument_id",
        "allowed_boards": ["主板", "创业板", "科创板"],
        "test_observed": True,
        "performance_claim": False,
    }
    payload.update(updates)
    return payload


def _audit_config(**updates: object) -> dict:
    payload = {
        "schema": "daily_product_strategy_audit_v1",
        "experiment_id": "audit",
        "data_start": "2020-01-01",
        "data_end": "2024-12-31",
        "signal_frequency": "weekly_first_open_session",
        "execution_lag_sessions": 1,
        "transaction_cost_bps": 10.0,
        "settlement_recovery_assumptions": [1.0, 0.0],
        "history_status": "retrospective_history_already_observed_not_fresh_oos",
        "performance_claim": False,
    }
    payload.update(updates)
    return payload


def _write_configs(tmp_path: Path, audit: dict, daily: dict) -> tuple[Path, Path]:
    audit_path = tmp_path / "audit.json"
    daily_path = tmp_path / "daily.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    daily_path.write_text(json.dumps(daily), encoding="utf-8")
    return audit_path, daily_path


def test_product_targets_use_exact_daily_count_tie_break_and_residual_cash() -> None:
    frame = pd.DataFrame(
        [
            {"instrument_id": "000003.SZ", "trade_date": DAY, "transparent_combo_v1": 3.0},
            {"instrument_id": "000002.SZ", "trade_date": DAY, "transparent_combo_v1": 3.0},
            {"instrument_id": "000001.SZ", "trade_date": DAY, "transparent_combo_v1": 3.0},
            {"instrument_id": "000004.SZ", "trade_date": DAY, "transparent_combo_v1": 2.0},
        ]
    )
    config = _daily_config()

    first = _product_targets(frame, config, [DAY])[DAY]
    second = _product_targets(
        frame.sample(frac=1, random_state=7).reset_index(drop=True),
        config,
        [DAY],
    )[DAY]

    assert first == second
    assert [item.instrument_id for item in first.positions] == ["000001.SZ", "000002.SZ"]
    assert all(item.target_weight == pytest.approx(0.4) for item in first.positions)
    assert first.cash_weight == pytest.approx(0.2)


def test_product_targets_respect_lower_is_better_baseline() -> None:
    frame = pd.DataFrame(
        [
            {"instrument_id": "a", "trade_date": DAY, "return_20d": -0.2},
            {"instrument_id": "b", "trade_date": DAY, "return_20d": -0.1},
            {"instrument_id": "c", "trade_date": DAY, "return_20d": 0.3},
        ]
    )
    config = _daily_config(
        score_definition="return_20d",
        score_direction="lower_is_better",
        target_count=1,
        max_weight_per_name=1.0,
    )

    portfolio = _product_targets(frame, config, [DAY])[DAY]

    assert [item.instrument_id for item in portfolio.positions] == ["a"]
    assert portfolio.cash_weight == pytest.approx(0.0)


def test_load_configs_binds_preregistered_recovery_and_daily_tie_contract(tmp_path: Path) -> None:
    paths = _write_configs(tmp_path, _audit_config(), _daily_config())
    audit, daily = _load_configs(*paths)
    assert audit["settlement_recovery_assumptions"] == [1.0, 0.0]
    assert daily["allowed_boards"] == ["主板", "创业板", "科创板"]

    paths = _write_configs(
        tmp_path,
        _audit_config(),
        _daily_config(tie_policy="keep_all_cutoff_ties"),
    )
    with pytest.raises(ValueError, match="tie policy"):
        _load_configs(*paths)


def test_load_configs_rejects_narrowed_historical_board_scope(tmp_path: Path) -> None:
    paths = _write_configs(
        tmp_path,
        _audit_config(),
        _daily_config(allowed_boards=["主板", "创业板"]),
    )
    with pytest.raises(ValueError, match="effective-dated board history"):
        _load_configs(*paths)


def test_load_configs_rejects_favorable_recovery_subset_and_performance_claim(
    tmp_path: Path,
) -> None:
    paths = _write_configs(
        tmp_path,
        _audit_config(settlement_recovery_assumptions=[1.0]),
        _daily_config(),
    )
    with pytest.raises(ValueError, match="recovery assumptions"):
        _load_configs(*paths)

    paths = _write_configs(
        tmp_path,
        _audit_config(performance_claim=True),
        _daily_config(),
    )
    with pytest.raises(ValueError, match="performance_claim=false"):
        _load_configs(*paths)
