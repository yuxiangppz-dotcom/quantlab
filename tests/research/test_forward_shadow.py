from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantlab.daily.service import generate_daily_snapshot
from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DataValidationError,
    Security,
    TradingCalendar,
)
from quantlab.data.storage import ParquetStorage
from quantlab.research.forward_shadow import (
    evaluate_matured_forward_shadows,
    generate_forward_shadow,
    latest_forward_shadow,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _seed(tmp_path: Path) -> tuple[ParquetStorage, Path, Path, date]:
    storage = ParquetStorage(tmp_path / "canonical")
    start = date(2026, 1, 2)
    sessions = [start + timedelta(days=offset) for offset in range(50)]
    storage.save_trading_calendar([TradingCalendar("SSE", day, True) for day in sessions])
    instruments = ("000001.SZ", "600000.SH")
    storage.save_securities(
        [
            Security(
                code,
                code[:6],
                code,
                "SZSE" if code.endswith("SZ") else "SSE",
                "A",
                "主板",
                "L",
                start,
                None,
            )
            for code in instruments
        ]
    )
    for index, day in enumerate(sessions):
        bars = [
            DailyBar(
                code,
                day,
                10 + index,
                11 + index,
                9 + index,
                10 + index + shift,
                9 + index,
                1000,
                10000,
            )
            for code, shift in zip(instruments, (0.0, 0.5), strict=True)
        ]
        storage.save_daily_bars_by_date(bars, day)
        storage.save_adj_factors_by_date([AdjFactor(code, day, 1.0) for code in instruments], day)
    signal = sessions[24]
    storage.save_daily_basic_by_date(
        [DailyBasic(code, signal, 0.01, 100_000_000, 80_000_000) for code in instruments], signal
    )
    config = tmp_path / "daily.json"
    config.write_text(
        json.dumps(
            {
                "config_id": "test",
                "strategy_id": "baseline",
                "model_status": "baseline",
                "score_definition": "return_20d",
                "score_direction": "lower_is_better",
                "target_count": 2,
                "max_weight_per_name": 0.5,
                "gross_exposure": 1.0,
                "tie_policy": "alpha_then_code",
                "test_observed": True,
                "performance_claim": False,
            }
        )
    )
    products = tmp_path / "products"
    generate_daily_snapshot(
        signal,
        storage=storage,
        config_path=config,
        product_root=products,
        now=datetime(2026, 3, 1, 18, tzinfo=SHANGHAI),
    )
    shadow_config = tmp_path / "shadow.json"
    shadow_config.write_text(
        json.dumps(
            {
                "schema": "quantlab_forward_shadow_config_v1",
                "config_id": "shadow_test",
                "universe": "test",
                "target_count": 1,
                "max_weight_per_name": 1.0,
                "label_horizon_sessions": 20,
                "models": [
                    {
                        "model_id": "baseline",
                        "version": "v1",
                        "source_column": "return_20d",
                        "direction": "lower_is_better",
                        "status": "baseline",
                    },
                    {
                        "model_id": "combo",
                        "version": "v1",
                        "source_column": "transparent_combo_v1",
                        "direction": "higher_is_better",
                        "status": "RESEARCH_CANDIDATE_NOT_PROMOTED",
                    },
                ],
            }
        )
    )
    return storage, products, shadow_config, signal


def test_forward_shadow_is_immutable_idempotent_and_separate_from_evaluation(
    tmp_path: Path,
) -> None:
    storage, products, config, signal = _seed(tmp_path)
    root = tmp_path / "shadow"
    first = generate_forward_shadow(
        product_root=products,
        shadow_root=root,
        config_path=config,
        now=datetime(2026, 3, 1, 19, tzinfo=SHANGHAI),
    )
    second = generate_forward_shadow(
        product_root=products,
        shadow_root=root,
        config_path=config,
        now=datetime(2026, 3, 2, 19, tzinfo=SHANGHAI),
    )
    assert [item.reused for item in first] == [False, False]
    assert [item.reused for item in second] == [True, True]
    assert {item.prediction_fingerprint for item in first} == {
        item.prediction_fingerprint for item in second
    }
    manifests = latest_forward_shadow(root)
    assert {item["trade_date"] for item in manifests} == {signal.isoformat()}
    assert all(not item["claims"]["broker_order"] for item in manifests)

    evaluations = evaluate_matured_forward_shadows(
        storage=storage, shadow_root=root, evaluation_root=tmp_path / "evaluations"
    )
    assert len(evaluations) == 2
    assert all(path.is_relative_to(tmp_path / "evaluations") for path in evaluations)
    assert (
        evaluate_matured_forward_shadows(
            storage=storage, shadow_root=root, evaluation_root=tmp_path / "evaluations"
        )
        == []
    )


def test_forward_shadow_binds_core_fixed_count_portfolio_contract(tmp_path: Path) -> None:
    _, products, config, _ = _seed(tmp_path)
    payload = json.loads(config.read_text())
    payload["max_weight_per_name"] = 0.4
    config.write_text(json.dumps(payload))

    result = generate_forward_shadow(
        product_root=products,
        shadow_root=tmp_path / "shadow",
        config_path=config,
    )[0]
    manifest = json.loads((result.prediction_dir / "prediction.json").read_text())
    rows = list(csv.DictReader((result.prediction_dir / "target_portfolio.csv").open()))

    assert manifest["portfolio_contract"] == {
        "constructor": "fixed_count_v1",
        "requested_target_count": 1,
        "score_direction": "lower_is_better",
        "gross_exposure": 1.0,
        "max_weight_per_name": 0.4,
        "tie_policy": "alpha_score_then_instrument_id",
    }
    assert manifest["target_count"] == 1
    assert manifest["target_weight_sum"] == pytest.approx(0.4)
    assert manifest["cash_weight"] == pytest.approx(0.6)
    assert len(rows) == 1
    assert float(rows[0]["target_weight"]) == pytest.approx(0.4)


def test_forward_shadow_tampering_fails_closed(tmp_path: Path) -> None:
    _, products, config, _ = _seed(tmp_path)
    root = tmp_path / "shadow"
    result = generate_forward_shadow(product_root=products, shadow_root=root, config_path=config)[0]
    (result.prediction_dir / "scores.csv").write_text("tampered\n")
    with pytest.raises(DataValidationError, match="immutable forward-shadow file mismatch"):
        generate_forward_shadow(product_root=products, shadow_root=root, config_path=config)


def test_forward_shadow_manifest_tampering_fails_closed(tmp_path: Path) -> None:
    _, products, config, _ = _seed(tmp_path)
    root = tmp_path / "shadow"
    result = generate_forward_shadow(product_root=products, shadow_root=root, config_path=config)[0]
    marker = result.prediction_dir / "prediction.json"
    payload = json.loads(marker.read_text())
    payload["code_head"] = "0" * 40
    marker.write_text(json.dumps(payload))
    with pytest.raises(DataValidationError, match="manifest content mismatch"):
        generate_forward_shadow(product_root=products, shadow_root=root, config_path=config)


def test_matured_evaluation_rejects_tampered_prediction_target(tmp_path: Path) -> None:
    storage, products, config, _ = _seed(tmp_path)
    root = tmp_path / "shadow"
    result = generate_forward_shadow(product_root=products, shadow_root=root, config_path=config)[0]
    target = result.prediction_dir / "target_portfolio.csv"
    target.write_text("instrument_id,target_weight\n000001.SZ,1.0\n", encoding="utf-8")

    with pytest.raises(
        DataValidationError,
        match="immutable forward-shadow file mismatch: target_portfolio.csv",
    ):
        evaluate_matured_forward_shadows(
            storage=storage,
            shadow_root=root,
            evaluation_root=tmp_path / "evaluations",
        )


def test_matured_evaluation_rejects_tampered_prediction_manifest(tmp_path: Path) -> None:
    storage, products, config, _ = _seed(tmp_path)
    root = tmp_path / "shadow"
    result = generate_forward_shadow(product_root=products, shadow_root=root, config_path=config)[0]
    marker = result.prediction_dir / "prediction.json"
    payload = json.loads(marker.read_text())
    payload["code_head"] = "f" * 40
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DataValidationError, match="manifest content mismatch"):
        evaluate_matured_forward_shadows(
            storage=storage,
            shadow_root=root,
            evaluation_root=tmp_path / "evaluations",
        )
