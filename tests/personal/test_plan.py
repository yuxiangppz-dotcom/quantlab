from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quantlab.data.models import DailyBar, Security
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import import_account_csv
from quantlab.personal.plan import build_reference_plan, load_latest_plan

HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)


def _seed_product(tmp_path: Path) -> tuple[Path, ParquetStorage]:
    product_root = tmp_path / "products"
    out = product_root / "2026-09-09"
    out.mkdir(parents=True)
    ranking = pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "name": "甲",
                "rank": 1,
                "selected": True,
                "target_weight": 0.5,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            },
            {
                "instrument_id": "600000.SH",
                "name": "乙",
                "rank": 2,
                "selected": True,
                "target_weight": 0.4999,
                "risk_context": "ST_CONTEXT_REPORTED",
            },
            {
                "instrument_id": "300001.SZ",
                "name": "旧持仓",
                "rank": 3,
                "selected": False,
                "target_weight": 0.0,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            },
            {
                "instrument_id": "688001.SH",
                "name": "高价目标",
                "rank": 4,
                "selected": True,
                "target_weight": 0.0001,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            },
        ]
    )
    ranking.to_csv(out / "ranking.csv", index=False)
    (out / "target_portfolio.csv").write_text("instrument_id,target_weight\n")
    (out / "report.html").write_text("ok")
    (out / "report.json").write_text(
        json.dumps(
            {
                "effective_as_of": "2026-09-09",
                "next_known_open_session": "2026-09-10",
                "content_fingerprint": "daily-fingerprint",
            }
        )
    )
    storage = ParquetStorage(tmp_path / "canonical")
    securities = [
        Security("000001.SZ", "000001", "甲", "SZSE", "SZ", "主板", "L", date(2000, 1, 1), None),
        Security("600000.SH", "600000", "乙", "SSE", "SH", "主板", "L", date(2000, 1, 1), None),
        Security(
            "300001.SZ", "300001", "旧持仓", "SZSE", "SZ", "创业板", "L", date(2000, 1, 1), None
        ),
        Security(
            "688001.SH",
            "688001",
            "高价目标",
            "SSE",
            "SH",
            "科创板",
            "L",
            date(2000, 1, 1),
            None,
        ),
    ]
    storage.save_securities(securities)
    storage.save_daily_bars_by_date(
        [
            DailyBar("000001.SZ", date(2026, 9, 9), 10, 10, 10, 10, 10, 1, 10),
            DailyBar("600000.SH", date(2026, 9, 9), 20, 20, 20, 20, 20, 1, 20),
            DailyBar("300001.SZ", date(2026, 9, 9), 5, 5, 5, 5, 5, 1, 5),
            DailyBar("688001.SH", date(2026, 9, 9), 100, 100, 100, 100, 100, 1, 100),
        ],
        date(2026, 9, 9),
    )
    return product_root, storage


def test_plan_uses_raw_close_current_cash_and_blocks_st_buy(tmp_path: Path) -> None:
    product_root, storage = _seed_product(tmp_path)
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            HEADER + "mine,manual_tracking,2026-09-10T08:00:00+08:00,100000.00,"
            "300001.SZ,50,0,4.00,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    json_path, csv_path, payload = build_reference_plan(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    rows = {item["instrument_id"]: item for item in payload["rows"]}
    assert rows["000001.SZ"]["action"] == "BUY"
    assert rows["000001.SZ"]["reference_price_cny"] == "10"
    assert rows["600000.SH"]["action"] == "NO_TRADE"
    assert rows["600000.SH"]["reason"] == "ST_CONTEXT_REQUIRES_USER_REVIEW"
    assert rows["300001.SZ"]["action"] == "HOLD"
    assert rows["300001.SZ"]["reason"] == "T1_OR_QUANTITY_GRID_PREVENTS_SELL"
    assert rows["688001.SH"]["action"] == "NO_TRADE"
    assert rows["688001.SH"]["reason"] == "TARGET_BUDGET_BELOW_MINIMUM_QUANTITY"
    assert payload["execution_confirmed"] is False
    assert payload["sell_proceeds_fund_buys"] is False
    assert json_path.exists() and csv_path.exists()
    json_before = json_path.read_bytes()
    csv_before = csv_path.read_bytes()
    second_json, second_csv, second = build_reference_plan(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert (second_json, second_csv, second["plan_id"]) == (
        json_path,
        csv_path,
        payload["plan_id"],
    )
    assert second_json.read_bytes() == json_before
    assert second_csv.read_bytes() == csv_before
    assert load_latest_plan(
        "mine",
        account_root=account_root,
        account_fingerprint=payload["account_fingerprint"],
    ) == (json_path, payload)
    csv_path.write_text(csv_path.read_text() + "tampered")
    with pytest.raises(ValueError, match="CSV fingerprint mismatch"):
        load_latest_plan(
            "mine",
            account_root=account_root,
            account_fingerprint=payload["account_fingerprint"],
        )


def test_missing_price_for_existing_position_fails_complete_valuation(tmp_path: Path) -> None:
    product_root, storage = _seed_product(tmp_path)
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            HEADER + "mine,manual_tracking,2026-09-10T08:00:00+08:00,1,"
            "688999.SH,200,200,10.00,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    try:
        build_reference_plan(
            "mine", account_root=account_root, product_root=product_root, storage=storage
        )
    except ValueError as exc:
        assert "missing prices: 688999.SH" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing held price must fail")


def test_cash_constrained_plan_allocates_by_alpha_rank_not_security_code(tmp_path: Path) -> None:
    product_root, storage = _seed_product(tmp_path)
    ranking_path = product_root / "2026-09-09" / "ranking.csv"
    ranking = pd.read_csv(ranking_path)
    ranking.loc[ranking["instrument_id"] == "600000.SH", ["rank", "risk_context"]] = [
        1,
        "NO_CONTEXT_EVENT_REPORTED",
    ]
    ranking.loc[ranking["instrument_id"] == "000001.SZ", "rank"] = 2
    ranking.loc[ranking["instrument_id"] == "688001.SH", "selected"] = False
    ranking.loc[ranking["instrument_id"].isin(["000001.SZ", "600000.SH"]), "target_weight"] = 0.5
    # Reverse the input rows to prove the allocation is independent of input order.
    ranking.iloc[::-1].to_csv(ranking_path, index=False)
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            HEADER + "mine,manual_tracking,2026-09-10T08:00:00+08:00,20000.00,,0,0,,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    _, _, payload = build_reference_plan(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    rows = {item["instrument_id"]: item for item in payload["rows"]}
    assert rows["600000.SH"]["action"] == "BUY"
    assert rows["600000.SH"]["target_alpha_rank"] == 1
    assert rows["000001.SZ"]["action"] == "NO_TRADE"
    assert rows["000001.SZ"]["reason"] == "INSUFFICIENT_CURRENT_CASH_NO_SELL_FUNDING"
    assert payload["sell_proceeds_fund_buys"] is False


def test_candidate_plan_is_explicitly_not_promoted(tmp_path: Path) -> None:
    product_root, storage = _seed_product(tmp_path)
    report_path = product_root / "2026-09-09" / "report.json"
    report = json.loads(report_path.read_text())
    report["model"] = {"model_status": "candidate_not_promoted_no_cost_control_closure"}
    report_path.write_text(json.dumps(report))
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            HEADER
            + "mine,manual_tracking,2026-09-10T08:00:00+08:00,200000.00,,0,0,,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    _, _, payload = build_reference_plan(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert payload["candidate_warning"] == "RESEARCH_CANDIDATE_NOT_PROMOTED"
    assert "NEXT_SESSION_PRICE_LIMIT_NOT_YET_OBSERVED" in payload["rows"][0]["pending_checks"]
