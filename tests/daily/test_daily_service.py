from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.daily.service import generate_daily_snapshot, inspect_data_status
from quantlab.data.models import AdjFactor, DailyBar, DailyBasic, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _seed_storage(root: Path) -> tuple[ParquetStorage, list[date]]:
    storage = ParquetStorage(root / "canonical")
    start = date(2026, 1, 2)
    sessions = [start + timedelta(days=offset) for offset in range(25)]
    storage.save_trading_calendar(
        [
            TradingCalendar(exchange=exchange, trade_date=day, is_open=True)
            for day in sessions
            for exchange in ("SSE", "SZSE")
        ]
    )
    storage.save_securities(
        [
            Security(
                instrument_id="000001.SZ",
                symbol="000001",
                name="平安银行",
                exchange="SZSE",
                market="SZ",
                board="主板",
                list_status="L",
                list_date=date(1991, 4, 3),
                delist_date=None,
            ),
            Security(
                instrument_id="600000.SH",
                symbol="600000",
                name="浦发银行",
                exchange="SSE",
                market="SH",
                board="主板",
                list_status="L",
                list_date=date(1999, 11, 10),
                delist_date=None,
            ),
        ]
    )
    for index, day in enumerate(sessions):
        bars = [
            DailyBar(
                instrument_id=instrument_id,
                trade_date=day,
                open=10 + index,
                high=11 + index,
                low=9 + index,
                close=10 + index,
                pre_close=9 + index,
                volume=1000,
                amount=10000,
            )
            for instrument_id in ("000001.SZ", "600000.SH")
        ]
        storage.save_daily_bars_by_date(bars, day)
        storage.save_adj_factors_by_date(
            [AdjFactor(item.instrument_id, day, 1.0) for item in bars], day
        )
    last = sessions[-1]
    storage.save_daily_basic_by_date(
        [
            DailyBasic("000001.SZ", last, 0.01, 100_000_000, 80_000_000),
            DailyBasic("600000.SH", last, 0.02, 200_000_000, 160_000_000),
        ],
        last,
    )
    return storage, sessions


def _config(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "config_id": "test_daily",
                "strategy_id": "momentum_20d_reversal_example",
                "model_status": "baseline_research_example",
                "score_definition": "return_20d",
                "score_direction": "lower_is_better",
                "universe": "V1_SH_SZ_A_share",
                "target_count": 2,
                "max_weight_per_name": 0.5,
                "gross_exposure": 1.0,
                "tie_policy": "alpha_score_then_instrument_id",
                "test_observed": True,
                "performance_claim": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_status_does_not_call_old_data_today(tmp_path: Path) -> None:
    storage, sessions = _seed_storage(tmp_path)
    requested = sessions[-1] + timedelta(days=3)
    status = inspect_data_status(
        storage,
        requested,
        now=datetime(2026, 2, 1, 12, tzinfo=SHANGHAI),
    )
    assert status["effective_as_of"] == sessions[-1].isoformat()
    assert status["status"] == "stale_calendar_unknown"
    assert status["requested_session_status"] == "unknown_calendar_out_of_coverage"
    assert status["stale_open_sessions"] is None


def test_snapshot_is_deterministic_idempotent_and_future_isolated(tmp_path: Path) -> None:
    storage, sessions = _seed_storage(tmp_path)
    effective = sessions[-1]
    config_path = _config(tmp_path / "daily.json")
    product_root = tmp_path / "products"
    now = datetime(2026, 2, 1, 18, tzinfo=SHANGHAI)

    first = generate_daily_snapshot(
        effective,
        storage=storage,
        config_path=config_path,
        product_root=product_root,
        now=now,
    )
    second = generate_daily_snapshot(
        effective,
        storage=storage,
        config_path=config_path,
        product_root=product_root,
        now=now + timedelta(minutes=5),
    )
    ranking_before = pd.read_csv(first.ranking_path)
    assert not first.reused
    assert second.reused
    assert ranking_before.loc[0, "instrument_id"] == "000001.SZ"
    assert ranking_before.loc[1, "instrument_id"] == "600000.SH"
    assert set(ranking_before["selected"]) == {True}
    assert ranking_before["selection_reason"].str.startswith("RESEARCH_TARGET").all()
    assert "transparent_combo_v1" in ranking_before
    assert "low_amplitude" in ranking_before
    assert first.report["target"]["status"] == "research_target_only"
    assert first.report["claims"]["broker_order"] is False

    future = effective + timedelta(days=1)
    storage.save_daily_bars_by_date(
        [
            DailyBar(
                "000001.SZ", future, 999, 1000, 998, 999, 10, 1000, 999000
            )
        ],
        future,
    )
    third = generate_daily_snapshot(
        effective,
        storage=storage,
        config_path=config_path,
        product_root=product_root,
        now=now + timedelta(minutes=10),
    )
    pd.testing.assert_frame_equal(ranking_before, pd.read_csv(third.ranking_path))
    assert third.reused
