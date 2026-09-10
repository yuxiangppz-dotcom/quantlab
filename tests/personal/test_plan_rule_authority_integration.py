from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.data.models import DailyBar, Security
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import import_account_csv
from quantlab.personal.plan import build_reference_plan

ACCOUNT_HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)
TARGET_WEIGHT = 0.5


def _write_single_name_snapshot(
    product_root: Path,
    *,
    signal_date: date,
    next_session: date,
    instrument_id: str,
) -> None:
    out = product_root / signal_date.isoformat()
    out.mkdir(parents=True)
    ranking = pd.DataFrame(
        [
            {
                "instrument_id": instrument_id,
                "name": "测试",
                "trade_date": signal_date.isoformat(),
                "alpha_score": 1.0,
                "rank": 1,
                "selected": True,
                "target_weight": TARGET_WEIGHT,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            }
        ]
    )
    target = ranking[["instrument_id", "rank", "alpha_score", "target_weight"]]
    ranking.to_csv(out / "ranking.csv", index=False)
    target.to_csv(out / "target_portfolio.csv", index=False)
    (out / "report.html").write_text("ok", encoding="utf-8")
    report = {
        "effective_as_of": signal_date.isoformat(),
        "next_known_open_session": next_session.isoformat(),
        "model": {
            "config_id": "test",
            "strategy_id": "test_strategy",
            "model_status": "baseline",
            "score_definition": "transparent_combo_v1",
            "score_direction": "higher_is_better",
            "target_count": 1,
            "max_weight_per_name": TARGET_WEIGHT,
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
            "position_weight": TARGET_WEIGHT,
            "position_weight_sum": TARGET_WEIGHT,
            "cash_weight": 1.0 - TARGET_WEIGHT,
        },
    }
    payload = {
        "report": report,
        "ranking_sha256": hashlib.sha256((out / "ranking.csv").read_bytes()).hexdigest(),
        "target_sha256": hashlib.sha256((out / "target_portfolio.csv").read_bytes()).hexdigest(),
    }
    report["content_fingerprint"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    (out / "report.json").write_text(json.dumps(report), encoding="utf-8")


def _build_one_name_plan(
    tmp_path: Path,
    *,
    signal_date: date,
    next_session: date,
    security: Security,
) -> dict:
    product_root = tmp_path / "products"
    _write_single_name_snapshot(
        product_root,
        signal_date=signal_date,
        next_session=next_session,
        instrument_id=security.instrument_id,
    )
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_securities([security])
    storage.save_daily_bars_by_date(
        [
            DailyBar(
                security.instrument_id,
                signal_date,
                10,
                10,
                10,
                10,
                10,
                1,
                10,
            )
        ],
        signal_date,
    )
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            ACCOUNT_HEADER
            + f"mine,manual_tracking,{next_session.isoformat()}T08:00:00+08:00,"
            "10000.00,,0,0,,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    _, _, payload = build_reference_plan(
        "mine",
        account_root=account_root,
        product_root=product_root,
        storage=storage,
    )
    return payload


def test_reference_plan_uses_authoritative_rule_when_session_is_covered(tmp_path: Path) -> None:
    payload = _build_one_name_plan(
        tmp_path,
        signal_date=date(2024, 1, 2),
        next_session=date(2024, 1, 3),
        security=Security(
            "600000.SH",
            "600000",
            "测试",
            "SSE",
            "SH",
            "主板",
            "L",
            date(2000, 1, 1),
            None,
        ),
    )

    row = payload["rows"][0]
    assert row["action"] == "BUY"
    assert row["quantity_rule_status"] == "authoritative_pit_rule"
    assert row["quantity_rule_id"] == "sse-main-2023"
    assert payload["quantity_rule_policy"] == "authoritative_pit_then_explicit_engineering_fallback"
    assert payload["broker_submission"] is False


def test_reference_plan_labels_uncovered_2026_rule_as_unverified_fallback(tmp_path: Path) -> None:
    payload = _build_one_name_plan(
        tmp_path,
        signal_date=date(2026, 9, 9),
        next_session=date(2026, 9, 10),
        security=Security(
            "000001.SZ",
            "000001",
            "测试",
            "SZSE",
            "SZ",
            "主板",
            "L",
            date(2000, 1, 1),
            None,
        ),
    )

    row = payload["rows"][0]
    assert row["action"] == "BUY"
    assert row["quantity_rule_status"] == "engineering_fallback_unverified"
    assert row["quantity_rule_id"] == "reference_fallback_szse_main_v1"
    assert "authoritative PIT rule-book coverage is unavailable" in row["quantity_rule_limitations"]
    assert payload["execution_confirmed"] is False
