from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quantlab.daily.changes import compare_daily_snapshots
from quantlab.daily.service import DailySnapshot
from quantlab.data.models import DataValidationError
from quantlab.portfolio.product import construct_daily_fixed_count_portfolio


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(
    root: Path,
    trade_date: date,
    scores: dict[str, float],
    *,
    target_count: int = 2,
) -> DailySnapshot:
    out = root / trade_date.isoformat()
    out.mkdir(parents=True)
    model = {
        "strategy_id": "test_strategy",
        "score_definition": "test_score",
        "score_direction": "higher_is_better",
        "target_count": target_count,
        "max_weight_per_name": 0.4,
        "gross_exposure": 1.0,
        "tie_policy": "alpha_score_then_instrument_id",
        "allowed_boards": ["主板", "创业板", "科创板"],
    }
    alpha = pd.DataFrame(
        [
            {
                "instrument_id": instrument_id,
                "trade_date": trade_date,
                "alpha_score": score,
            }
            for instrument_id, score in scores.items()
        ]
    )
    ordered = alpha.sort_values(
        ["alpha_score", "instrument_id"],
        ascending=[False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    ordered["rank"] = range(1, len(ordered) + 1)
    portfolio = construct_daily_fixed_count_portfolio(alpha, trade_date, model)
    weights = {item.instrument_id: item.target_weight for item in portfolio.positions}
    ordered["selected"] = ordered["instrument_id"].isin(weights)
    ordered["target_weight"] = ordered["instrument_id"].map(weights).fillna(0.0)
    ordered["name"] = ordered["instrument_id"].map(lambda value: f"name-{value}")
    ranking = ordered[
        [
            "instrument_id",
            "name",
            "trade_date",
            "alpha_score",
            "rank",
            "selected",
            "target_weight",
        ]
    ]
    target = ranking.loc[
        ranking["selected"],
        ["instrument_id", "rank", "alpha_score", "target_weight"],
    ]
    ranking_path = out / "ranking.csv"
    target_path = out / "target_portfolio.csv"
    html_path = out / "report.html"
    report_path = out / "report.json"
    ranking.to_csv(ranking_path, index=False, lineterminator="\n")
    target.to_csv(target_path, index=False, lineterminator="\n")
    html_path.write_text("ok", encoding="utf-8")
    selected_weights = list(weights.values())
    report_core = {
        "schema": "quantlab_daily_v1",
        "effective_as_of": trade_date.isoformat(),
        "model": model,
        "ranking": {
            "universe_rows": len(ranking),
            "valid_score_rows": len(ranking),
            "selected_rows": len(target),
            "tie_policy": model["tie_policy"],
        },
        "target": {
            "position_weight": selected_weights[0] if selected_weights else 0.0,
            "position_weight_sum": sum(selected_weights),
            "cash_weight": portfolio.cash_weight,
        },
    }
    fingerprint = _hash(
        {
            "report": report_core,
            "ranking_sha256": hashlib.sha256(ranking_path.read_bytes()).hexdigest(),
            "target_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
        }
    )
    report = {
        **report_core,
        "generated_at": f"{trade_date.isoformat()}T18:00:00+08:00",
        "content_fingerprint": fingerprint,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return DailySnapshot(
        report_path=report_path,
        ranking_path=ranking_path,
        target_path=target_path,
        html_path=html_path,
        report=report,
        reused=True,
    )


def test_compare_daily_snapshots_reports_entries_exits_ranks_and_turnover(tmp_path: Path) -> None:
    previous = _snapshot(
        tmp_path,
        date(2026, 9, 8),
        {"000001.SZ": 3.0, "000002.SZ": 2.0, "000003.SZ": 1.0},
    )
    current = _snapshot(
        tmp_path,
        date(2026, 9, 9),
        {"000001.SZ": 1.0, "000002.SZ": 4.0, "000003.SZ": 3.0},
    )

    result = compare_daily_snapshots(previous, current)

    assert result["portfolio_contract_same"] is True
    assert result["change_interpretation"] == "same_contract_signal_movement"
    assert result["selected"] == {
        "previous_count": 2,
        "current_count": 2,
        "retained_count": 1,
        "new_entry_count": 1,
        "exit_count": 1,
        "overlap_jaccard": pytest.approx(1 / 3),
    }
    assert [item["instrument_id"] for item in result["new_entries"]] == ["000003.SZ"]
    assert [item["instrument_id"] for item in result["exits"]] == ["000001.SZ"]
    rank_changes = {item["instrument_id"]: item for item in result["rank_changes"]}
    assert rank_changes["000002.SZ"]["rank_improvement"] == 1
    assert rank_changes["000001.SZ"]["rank_improvement"] == -2
    assert result["target_change"]["stock_absolute_weight_change"] == pytest.approx(0.8)
    assert result["target_change"]["cash_weight_change"] == pytest.approx(0.0)
    assert result["target_change"]["one_way_target_turnover"] == pytest.approx(0.4)
    assert result["claims"] == {
        "performance_claim": False,
        "execution_claim": False,
        "fill_claim": False,
    }


def test_contract_change_is_visible_and_not_described_as_pure_signal_movement(
    tmp_path: Path,
) -> None:
    previous = _snapshot(
        tmp_path,
        date(2026, 9, 8),
        {"000001.SZ": 3.0, "000002.SZ": 2.0},
        target_count=2,
    )
    current = _snapshot(
        tmp_path,
        date(2026, 9, 9),
        {"000001.SZ": 3.0, "000002.SZ": 2.0},
        target_count=1,
    )

    result = compare_daily_snapshots(previous, current)

    assert result["portfolio_contract_same"] is False
    assert result["change_interpretation"] == (
        "portfolio_contract_changed_not_pure_signal_movement"
    )
    assert result["target_change"]["one_way_target_turnover"] == pytest.approx(0.4)


def test_comparison_is_order_invariant_and_fingerprint_is_deterministic(tmp_path: Path) -> None:
    previous = _snapshot(
        tmp_path,
        date(2026, 9, 8),
        {"000003.SZ": 1.0, "000001.SZ": 3.0, "000002.SZ": 2.0},
    )
    current = _snapshot(
        tmp_path,
        date(2026, 9, 9),
        {"000002.SZ": 4.0, "000003.SZ": 3.0, "000001.SZ": 1.0},
    )

    first = compare_daily_snapshots(previous, current)
    second = compare_daily_snapshots(previous, current)

    assert first == second
    assert len(first["comparison_fingerprint"]) == 64


def test_comparison_rejects_non_forward_dates_and_tampered_inputs(tmp_path: Path) -> None:
    previous = _snapshot(tmp_path, date(2026, 9, 8), {"000001.SZ": 1.0})
    same_day = _snapshot(tmp_path / "same", date(2026, 9, 8), {"000001.SZ": 2.0})
    with pytest.raises(DataValidationError, match="current effective_as_of after previous"):
        compare_daily_snapshots(previous, same_day)

    current = _snapshot(tmp_path, date(2026, 9, 9), {"000001.SZ": 2.0})
    current.ranking_path.write_text(
        current.ranking_path.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="content fingerprint mismatch"):
        compare_daily_snapshots(previous, current)
