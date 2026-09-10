from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quantlab.daily.materialization import commit_staged_daily_snapshot
from quantlab.daily.service import DailySnapshot, load_latest_snapshot
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


def _staged(
    root: Path,
    *,
    scores: dict[str, float],
    generated_at: str,
) -> DailySnapshot:
    trade_date = date(2026, 9, 9)
    config_version = "daily_mvp_v1-configsha"
    out = root / trade_date.isoformat() / config_version
    out.mkdir(parents=True)
    model = {
        "config_id": "daily_mvp_v1",
        "strategy_id": "test",
        "model_status": "baseline",
        "score_definition": "test_score",
        "score_direction": "higher_is_better",
        "target_count": 2,
        "max_weight_per_name": 0.4,
        "gross_exposure": 1.0,
        "tie_policy": "alpha_score_then_instrument_id",
        "allowed_boards": ["主板", "创业板", "科创板"],
        "test_observed": True,
        "performance_claim": False,
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
    ranking = ordered[
        ["instrument_id", "trade_date", "alpha_score", "rank", "selected", "target_weight"]
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
    html_path.write_text("<html>staged</html>", encoding="utf-8")
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
        "generated_at": generated_at,
        "content_fingerprint": fingerprint,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return DailySnapshot(
        report_path=report_path,
        ranking_path=ranking_path,
        target_path=target_path,
        html_path=html_path,
        report=report,
        reused=False,
    )


def test_same_content_reuses_content_addressed_bundle_without_rewrite(tmp_path: Path) -> None:
    product_root = tmp_path / "products"
    first_stage = _staged(
        tmp_path / "stage-one",
        scores={"000001.SZ": 3.0, "000002.SZ": 2.0, "000003.SZ": 1.0},
        generated_at="2026-09-09T18:00:00+08:00",
    )
    first = commit_staged_daily_snapshot(first_stage, product_root)
    assert first.reused is False
    assert first.report_path.parent.name == first.report["content_fingerprint"]
    report_before = first.report_path.read_bytes()

    second_stage = _staged(
        tmp_path / "stage-two",
        scores={"000003.SZ": 1.0, "000001.SZ": 3.0, "000002.SZ": 2.0},
        generated_at="2026-09-10T18:00:00+08:00",
    )
    second = commit_staged_daily_snapshot(second_stage, product_root)

    assert second.reused is True
    assert second.report_path == first.report_path
    assert second.report_path.read_bytes() == report_before
    assert second.report["generated_at"] == "2026-09-09T18:00:00+08:00"
    active = load_latest_snapshot(product_root)
    assert active is not None
    assert active.report_path == first.report_path


def test_changed_content_creates_sibling_and_preserves_previous_bundle(tmp_path: Path) -> None:
    product_root = tmp_path / "products"
    first_stage = _staged(
        tmp_path / "stage-one",
        scores={"000001.SZ": 3.0, "000002.SZ": 2.0, "000003.SZ": 1.0},
        generated_at="2026-09-09T18:00:00+08:00",
    )
    first = commit_staged_daily_snapshot(first_stage, product_root)
    first_bytes = {path.name: path.read_bytes() for path in first.report_path.parent.iterdir()}

    second_stage = _staged(
        tmp_path / "stage-two",
        scores={"000001.SZ": 1.0, "000002.SZ": 2.0, "000003.SZ": 3.0},
        generated_at="2026-09-10T18:00:00+08:00",
    )
    second = commit_staged_daily_snapshot(second_stage, product_root)

    assert second.reused is False
    assert second.report_path.parent != first.report_path.parent
    assert second.report["content_fingerprint"] != first.report["content_fingerprint"]
    assert {path.name: path.read_bytes() for path in first.report_path.parent.iterdir()} == first_bytes
    active = load_latest_snapshot(product_root)
    assert active is not None
    assert active.report_path == second.report_path
    siblings = list(first.report_path.parent.parent.iterdir())
    assert {item.name for item in siblings} == {
        first.report["content_fingerprint"],
        second.report["content_fingerprint"],
    }


def test_existing_content_addressed_bundle_is_never_repaired_in_place(tmp_path: Path) -> None:
    product_root = tmp_path / "products"
    staged = _staged(
        tmp_path / "stage",
        scores={"000001.SZ": 3.0, "000002.SZ": 2.0},
        generated_at="2026-09-09T18:00:00+08:00",
    )
    published = commit_staged_daily_snapshot(staged, product_root)
    published.ranking_path.write_text(
        published.ranking_path.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )
    tampered = published.ranking_path.read_bytes()

    with pytest.raises(DataValidationError, match="content fingerprint mismatch"):
        commit_staged_daily_snapshot(staged, product_root)

    assert published.ranking_path.read_bytes() == tampered


def test_legacy_loader_remains_compatible_with_nested_active_pointer(tmp_path: Path) -> None:
    product_root = tmp_path / "products"
    staged = _staged(
        tmp_path / "stage",
        scores={"000001.SZ": 3.0, "000002.SZ": 2.0},
        generated_at="2026-09-09T18:00:00+08:00",
    )
    published = commit_staged_daily_snapshot(staged, product_root)
    active_payload = json.loads((product_root / "ACTIVE.json").read_text(encoding="utf-8"))

    assert active_payload["report_path"].endswith(
        f"/{published.report['content_fingerprint']}/report.json"
    )
    loaded = load_latest_snapshot(product_root)
    assert loaded is not None
    assert loaded.report["content_fingerprint"] == published.report["content_fingerprint"]
