from __future__ import annotations

import hashlib
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


def _write_semantic_snapshot(
    out: Path,
    ranking: pd.DataFrame,
    *,
    target_count: int,
    model_status: str = "baseline",
) -> None:
    ranking = ranking.copy()
    ranking["trade_date"] = "2026-09-09"
    ranking = ranking.sort_values(
        ["alpha_score", "instrument_id"],
        ascending=[False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    ranking["rank"] = range(1, len(ranking) + 1)
    selected_count = min(target_count, len(ranking))
    ranking["selected"] = ranking["rank"] <= selected_count
    per_name = 1.0 / selected_count if selected_count else 0.0
    ranking["target_weight"] = ranking["selected"].map({True: per_name, False: 0.0})
    target = ranking.loc[
        ranking["selected"],
        ["instrument_id", "rank", "alpha_score", "target_weight"],
    ]
    ranking.to_csv(out / "ranking.csv", index=False)
    target.to_csv(out / "target_portfolio.csv", index=False)
    (out / "report.html").write_text("ok", encoding="utf-8")
    report = {
        "effective_as_of": "2026-09-09",
        "next_known_open_session": "2026-09-10",
        "model": {
            "config_id": "test",
            "strategy_id": "test_strategy",
            "model_status": model_status,
            "score_definition": "transparent_combo_v1",
            "score_direction": "higher_is_better",
            "target_count": target_count,
            "max_weight_per_name": 1.0,
            "gross_exposure": 1.0,
            "tie_policy": "alpha_score_then_instrument_id",
        },
        "ranking": {
            "universe_rows": len(ranking),
            "valid_score_rows": len(ranking),
            "selected_rows": selected_count,
            "tie_policy": "alpha_score_then_instrument_id",
        },
        "target": {
            "position_weight": per_name,
            "position_weight_sum": per_name * selected_count,
            "cash_weight": 1.0 - per_name * selected_count,
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


def _base_ranking() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "name": "甲",
                "alpha_score": 4.0,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            },
            {
                "instrument_id": "600000.SH",
                "name": "乙",
                "alpha_score": 3.0,
                "risk_context": "ST_CONTEXT_REPORTED",
            },
            {
                "instrument_id": "688001.SH",
                "name": "高价目标",
                "alpha_score": 2.0,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            },
            {
                "instrument_id": "300001.SZ",
                "name": "旧持仓",
                "alpha_score": 1.0,
                "risk_context": "NO_CONTEXT_EVENT_REPORTED",
            },
        ]
    )


def _seed_product(tmp_path: Path) -> tuple[Path, ParquetStorage]:
    product_root = tmp_path / "products"
    out = product_root / "2026-09-09"
    out.mkdir(parents=True)
    _write_semantic_snapshot(out, _base_ranking(), target_count=3)
    storage = ParquetStorage(tmp_path / "canonical")
    securities = [
        Security("000001.SZ", "000001", "甲", "SZSE", "SZ", "主板", "L", date(2000, 1, 1), None),
        Security("600000.SH", "600000", "乙", "SSE", "SH", "主板", "L", date(2000, 1, 1), None),
        Security(
            "300001.SZ",
            "300001",
            "旧持仓",
            "SZSE",
            "SZ",
            "创业板",
            "L",
            date(2000, 1, 1),
            None,
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
            DailyBar("688001.SH", date(2026, 9, 9), 200, 200, 200, 200, 200, 1, 200),
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
    out = product_root / "2026-09-09"
    ranking = _base_ranking()
    ranking.loc[ranking["instrument_id"] == "600000.SH", ["alpha_score", "risk_context"]] = [
        4.0,
        "NO_CONTEXT_EVENT_REPORTED",
    ]
    ranking.loc[ranking["instrument_id"] == "000001.SZ", "alpha_score"] = 3.0
    ranking.loc[ranking["instrument_id"] == "688001.SH", "alpha_score"] = 1.0
    ranking.loc[ranking["instrument_id"] == "300001.SZ", "alpha_score"] = 0.0
    _write_semantic_snapshot(out, ranking.iloc[::-1], target_count=2)
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
    out = product_root / "2026-09-09"
    _write_semantic_snapshot(
        out,
        _base_ranking(),
        target_count=3,
        model_status="candidate_not_promoted_no_cost_control_closure",
    )
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


def test_reference_plan_rejects_tampered_daily_ranking(tmp_path: Path) -> None:
    product_root, storage = _seed_product(tmp_path)
    ranking_path = product_root / "2026-09-09" / "ranking.csv"
    ranking_path.write_text(ranking_path.read_text() + "tampered\n", encoding="utf-8")
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            HEADER
            + "mine,manual_tracking,2026-09-10T08:00:00+08:00,200000.00,,0,0,,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    with pytest.raises(ValueError, match="content fingerprint mismatch"):
        build_reference_plan(
            "mine", account_root=account_root, product_root=product_root, storage=storage
        )
