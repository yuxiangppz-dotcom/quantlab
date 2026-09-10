from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from quantlab.data.models import DailyBar
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import import_account_csv
from quantlab.personal.valuation_checkpoint import (
    load_latest_valuation_checkpoint,
    materialize_valuation_checkpoint,
    validate_valuation_checkpoint,
)

ACCOUNT_HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _write_daily_snapshot(
    product_root: Path,
    *,
    trade_date: date = date(2026, 9, 9),
    instrument_id: str = "000001.SZ",
) -> str:
    out = product_root / trade_date.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    ranking = pd.DataFrame(
        [
            {
                "instrument_id": instrument_id,
                "trade_date": trade_date.isoformat(),
                "alpha_score": 1.0,
                "rank": 1,
                "selected": True,
                "target_weight": 1.0,
            }
        ]
    )
    target = ranking[["instrument_id", "rank", "alpha_score", "target_weight"]]
    ranking.to_csv(out / "ranking.csv", index=False)
    target.to_csv(out / "target_portfolio.csv", index=False)
    (out / "report.html").write_text("ok", encoding="utf-8")
    report = {
        "effective_as_of": trade_date.isoformat(),
        "model": {
            "config_id": "valuation-test",
            "strategy_id": "valuation-test",
            "model_status": "test_fixture",
            "score_definition": "transparent_combo_v1",
            "score_direction": "higher_is_better",
            "target_count": 1,
            "max_weight_per_name": 1.0,
            "gross_exposure": 1.0,
            "tie_policy": "alpha_score_then_instrument_id",
        },
        "ranking": {
            "universe_rows": 1,
            "valid_score_rows": 1,
            "selected_rows": 1,
            "tie_policy": "alpha_score_then_instrument_id",
        },
        "target": {
            "position_weight": 1.0,
            "position_weight_sum": 1.0,
            "cash_weight": 0.0,
        },
    }
    fingerprint = _canonical_hash(
        {
            "report": report,
            "ranking_sha256": hashlib.sha256((out / "ranking.csv").read_bytes()).hexdigest(),
            "target_sha256": hashlib.sha256(
                (out / "target_portfolio.csv").read_bytes()
            ).hexdigest(),
        }
    )
    report["content_fingerprint"] = fingerprint
    (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return fingerprint


def _import_account(
    account_root: Path,
    *,
    cash: str = "1000.00",
    as_of: str = "2026-09-09T12:00:00+08:00",
    instrument_id: str = "000001.SZ",
    quantity: int = 100,
) -> None:
    instrument = instrument_id if quantity else ""
    cost = "8.00" if quantity else ""
    import_account_csv(
        (
            ACCOUNT_HEADER
            + f"mine,manual_tracking,{as_of},{cash},{instrument},{quantity},{quantity},"
            + f"{cost},none_declared\n"
        ).encode(),
        account_root=account_root,
    )


def _setup(
    tmp_path: Path,
    *,
    close: float = 10.25,
    cash: str = "1000.00",
    quantity: int = 100,
) -> tuple[Path, Path, ParquetStorage, str]:
    account_root = tmp_path / "accounts"
    product_root = tmp_path / "products"
    _import_account(account_root, cash=cash, quantity=quantity)
    daily_fingerprint = _write_daily_snapshot(product_root)
    storage = ParquetStorage(tmp_path / "canonical")
    if quantity:
        storage.save_daily_bars_by_date(
            [
                DailyBar(
                    "000001.SZ",
                    date(2026, 9, 9),
                    close,
                    close,
                    close,
                    close,
                    close,
                    1,
                    close,
                )
            ],
            date(2026, 9, 9),
        )
    return account_root, product_root, storage, daily_fingerprint


def test_checkpoint_marks_raw_close_and_reuses_identical_evidence(tmp_path: Path) -> None:
    account_root, product_root, storage, daily_fingerprint = _setup(tmp_path)
    first_path, first, reused = materialize_valuation_checkpoint(
        "mine",
        account_root=account_root,
        product_root=product_root,
        storage=storage,
        now=datetime.fromisoformat("2026-09-10T09:00:00+08:00"),
    )
    assert reused is False
    assert first["daily_content_fingerprint"] == daily_fingerprint
    assert first["positions"] == [
        {
            "instrument_id": "000001.SZ",
            "quantity": 100,
            "raw_close_fen": 1025,
            "market_value_fen": 102_500,
        }
    ]
    assert first["cash_fen"] == 100_000
    assert first["nav_fen"] == 202_500
    assert first["price_basis"] == "raw_same_session_close"
    assert first["performance_claim"] is False
    before = first_path.read_bytes()

    second_path, second, reused = materialize_valuation_checkpoint(
        "mine",
        account_root=account_root,
        product_root=product_root,
        storage=storage,
        now=datetime.fromisoformat("2026-09-10T10:00:00+08:00"),
    )
    assert reused is True
    assert second_path == first_path
    assert second["generated_at"] == first["generated_at"]
    assert second_path.read_bytes() == before
    assert second_path.parent.name == first["checkpoint_fingerprint"]
    assert load_latest_valuation_checkpoint(
        "mine", account_root=account_root
    ) == (second_path, second)


def test_changed_account_fingerprint_creates_same_day_sibling(tmp_path: Path) -> None:
    account_root, product_root, storage, _ = _setup(tmp_path)
    first_path, first, _ = materialize_valuation_checkpoint(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    first_bytes = first_path.read_bytes()
    _import_account(account_root, cash="1200.00")

    second_path, second, reused = materialize_valuation_checkpoint(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert reused is False
    assert second_path != first_path
    assert second["account_fingerprint"] != first["account_fingerprint"]
    assert second["nav_fen"] == first["nav_fen"] + 20_000
    assert first_path.read_bytes() == first_bytes
    assert first_path.exists() and second_path.exists()


def test_missing_held_price_fails_closed_without_checkpoint(tmp_path: Path) -> None:
    account_root = tmp_path / "accounts"
    product_root = tmp_path / "products"
    _import_account(account_root, instrument_id="000002.SZ")
    _write_daily_snapshot(product_root)
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_daily_bars_by_date(
        [DailyBar("000001.SZ", date(2026, 9, 9), 10, 10, 10, 10, 10, 1, 10)],
        date(2026, 9, 9),
    )

    with pytest.raises(ValueError, match="missing same-session raw closes"):
        materialize_valuation_checkpoint(
            "mine", account_root=account_root, product_root=product_root, storage=storage
        )
    assert not list((account_root / "mine" / "valuations").glob("**/checkpoint.json"))


def test_daily_price_date_before_latest_account_fact_is_rejected(tmp_path: Path) -> None:
    account_root = tmp_path / "accounts"
    product_root = tmp_path / "products"
    _import_account(account_root, as_of="2026-09-10T09:00:00+08:00")
    _write_daily_snapshot(product_root, trade_date=date(2026, 9, 9))
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_daily_bars_by_date(
        [DailyBar("000001.SZ", date(2026, 9, 9), 10, 10, 10, 10, 10, 1, 10)],
        date(2026, 9, 9),
    )

    with pytest.raises(ValueError, match="predates the latest effective account fact"):
        materialize_valuation_checkpoint(
            "mine", account_root=account_root, product_root=product_root, storage=storage
        )


def test_tampered_checkpoint_and_active_path_traversal_fail_closed(tmp_path: Path) -> None:
    account_root, product_root, storage, _ = _setup(tmp_path)
    checkpoint_path, _, _ = materialize_valuation_checkpoint(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    payload = json.loads(checkpoint_path.read_text())
    payload["nav_fen"] += 1
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="NAV arithmetic mismatch|fingerprint mismatch"):
        validate_valuation_checkpoint(checkpoint_path, expected_account_id="mine")

    root = account_root / "mine" / "valuations"
    outside = account_root / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (root / "ACTIVE.json").write_text(
        json.dumps(
            {
                "schema": "quantlab_account_valuation_active_v1",
                "checkpoint_path": "../../../outside.json",
                "checkpoint_fingerprint": "f" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="escapes valuation root"):
        load_latest_valuation_checkpoint("mine", account_root=account_root)


def test_cash_only_account_checkpoint_requires_no_price_partition(tmp_path: Path) -> None:
    account_root, product_root, storage, _ = _setup(tmp_path, quantity=0)
    path, payload, reused = materialize_valuation_checkpoint(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert reused is False
    assert payload["positions"] == []
    assert payload["nav_fen"] == payload["cash_fen"] == 100_000
    assert payload["evidence"]["daily_bar_partition_sha256"] is None
    assert validate_valuation_checkpoint(path, expected_account_id="mine") == payload
