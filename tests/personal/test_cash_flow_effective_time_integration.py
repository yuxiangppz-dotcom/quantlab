from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quantlab.data.models import DailyBar, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import import_account_csv, load_account
from quantlab.personal.tracking import (
    import_manual_cash_flows,
    load_effective_account,
    load_tracking_summary,
    preview_manual_cash_flows,
)
from quantlab.personal.valuation_checkpoint import materialize_valuation_checkpoint

ACCOUNT_HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)
FLOW_HEADER = (
    "account_id,external_flow_id,effective_at,reported_at,direction,amount_cny\n"
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


def _seed(tmp_path: Path) -> tuple[Path, ParquetStorage]:
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            ACCOUNT_HEADER
            + "mine,manual_tracking,2026-09-09T09:00:00+08:00,1000.00,"
            "000001.SZ,100,100,8.00,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_trading_calendar(
        [TradingCalendar("SZSE", date(2026, 9, 9), True)]
    )
    storage.save_daily_bars_by_date(
        [DailyBar("000001.SZ", date(2026, 9, 9), 10, 10, 10, 10, 10, 1, 10)],
        date(2026, 9, 9),
    )
    return account_root, storage


def _flow(
    *,
    flow_id: str = "D1",
    effective_at: str = "2026-09-09T10:00:00+08:00",
    reported_at: str = "2026-09-09T18:00:00+08:00",
    direction: str = "DEPOSIT",
    amount: str = "100.00",
) -> bytes:
    return (
        FLOW_HEADER
        + f"mine,{flow_id},{effective_at},{reported_at},{direction},{amount}\n"
    ).encode()


def _write_daily_snapshot(product_root: Path) -> None:
    trade_date = date(2026, 9, 9)
    out = product_root / trade_date.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    ranking = pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
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
            "config_id": "timing-test",
            "strategy_id": "timing-test",
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


def _write_legacy_v2_journal(account_root: Path) -> Path:
    account = load_account("mine", account_root=account_root)
    event = {
        "event_type": "external_cash_flow",
        "event_id": "cashflow:mine:LEGACY",
        "flow_id": "LEGACY",
        "occurred_at": "2026-09-09T10:00:00+08:00",
        "account_id": "mine",
        "direction": "deposit",
        "amount_fen": 10_000,
        "source_sha256": "a" * 64,
        "source_row_sha256": "b" * 64,
    }
    events = [event]
    payload = {
        "schema": "quantlab_manual_tracking_journal_v2",
        "account_id": "mine",
        "opening_account_fingerprint": account["account_fingerprint"],
        "events": events,
        "events_fingerprint": hashlib.sha256(
            json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "opening_mark": {"status": "unavailable_no_daily_snapshot"},
        "latest_import": {
            "kind": "external_cash_flow",
            "source_sha256": "a" * 64,
            "accepted_count": 1,
            "duplicate_count": 0,
        },
    }
    payload["journal_fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = (
        account_root
        / "mine"
        / "tracking"
        / account["account_fingerprint"]
        / "journal.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_exact_import_exposes_economic_and_reported_watermarks(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    path, preview = import_manual_cash_flows(
        "mine",
        _flow(),
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    payload = json.loads(path.read_text())
    assert payload["schema"] == "quantlab_manual_tracking_journal_v3"
    assert payload["events"][0]["effective_at"] == "2026-09-09T10:00:00+08:00"
    assert payload["events"][0]["reported_at"] == "2026-09-09T18:00:00+08:00"
    assert preview["latest_economic_fact_at"] == "2026-09-09T10:00:00+08:00"
    assert preview["latest_reported_at"] == "2026-09-09T18:00:00+08:00"
    assert preview["cash_flow_timing_performance_eligible"] is True


def test_report_before_effective_is_rejected_before_write(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    source = _flow(
        effective_at="2026-09-09T18:00:00+08:00",
        reported_at="2026-09-09T17:59:59+08:00",
    )
    with pytest.raises(ValueError, match="cannot precede"):
        preview_manual_cash_flows(
            "mine", source, account_root=account_root, storage=storage
        )
    assert not list(account_root.glob("mine/tracking/*/journal.json"))


def test_legacy_v2_replays_but_is_timing_ineligible(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    _write_legacy_v2_journal(account_root)
    summary = load_tracking_summary("mine", account_root=account_root, storage=storage)
    assert summary["cash_fen"] == 110_000
    assert summary["cash_flow_timing_quality"] == (
        "legacy_reported_time_used_as_effective_unverified"
    )
    assert summary["cash_flow_timing_performance_eligible"] is False
    assert summary["latest_external_cash_flow_effective_at"] == (
        "2026-09-09T10:00:00+08:00"
    )
    assert summary["latest_external_cash_flow_reported_at"] == (
        "2026-09-09T10:00:00+08:00"
    )
    effective = load_effective_account("mine", account_root=account_root, storage=storage)
    assert effective["cash_flow_timing_performance_eligible"] is False


def test_new_exact_flow_upgrades_legacy_journal_without_erasing_quality(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    path = _write_legacy_v2_journal(account_root)
    import_manual_cash_flows(
        "mine",
        _flow(
            flow_id="D2",
            effective_at="2026-09-09T11:00:00+08:00",
            reported_at="2026-09-09T12:00:00+08:00",
        ),
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    payload = json.loads(path.read_text())
    assert payload["schema"] == "quantlab_manual_tracking_journal_v3"
    assert len(payload["events"]) == 2
    summary = load_tracking_summary("mine", account_root=account_root, storage=storage)
    assert summary["cash_flow_timing_performance_eligible"] is False
    qualities = {item["timing_quality"] for item in summary["cash_flows"]}
    assert qualities == {
        "exact_effective_time",
        "legacy_reported_time_used_as_effective_unverified",
    }


def test_valuation_checkpoint_binds_legacy_timing_blocker(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    _write_legacy_v2_journal(account_root)
    product_root = tmp_path / "products"
    _write_daily_snapshot(product_root)

    _, checkpoint, _ = materialize_valuation_checkpoint(
        "mine",
        account_root=account_root,
        product_root=product_root,
        storage=storage,
    )
    assert checkpoint["schema"] == "quantlab_account_valuation_checkpoint_v2"
    assert checkpoint["latest_economic_account_fact_at"] == (
        "2026-09-09T10:00:00+08:00"
    )
    assert checkpoint["cash_flow_timing_quality"] == (
        "legacy_reported_time_used_as_effective_unverified"
    )
    assert checkpoint["cash_flow_timing_performance_eligible"] is False
    assert checkpoint["performance_input_status"] == "blocked_legacy_cash_flow_timing"
    assert checkpoint["performance_claim"] is False
