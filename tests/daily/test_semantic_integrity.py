from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quantlab.daily import integrity
from quantlab.daily.service import DailySnapshot
from quantlab.data.models import DataValidationError


def _snapshot(tmp_path: Path) -> DailySnapshot:
    out = tmp_path / "snapshot"
    out.mkdir()
    ranking = pd.DataFrame(
        [
            {
                "instrument_id": "000003.SZ",
                "trade_date": "2026-09-09",
                "alpha_score": 3.0,
                "rank": 1,
                "selected": True,
                "target_weight": 0.4,
            },
            {
                "instrument_id": "000002.SZ",
                "trade_date": "2026-09-09",
                "alpha_score": 2.0,
                "rank": 2,
                "selected": True,
                "target_weight": 0.4,
            },
            {
                "instrument_id": "000001.SZ",
                "trade_date": "2026-09-09",
                "alpha_score": 1.0,
                "rank": 3,
                "selected": False,
                "target_weight": 0.0,
            },
        ]
    )
    target = ranking.loc[ranking["selected"], ["instrument_id", "rank", "alpha_score", "target_weight"]]
    ranking_path = out / "ranking.csv"
    target_path = out / "target_portfolio.csv"
    ranking.to_csv(ranking_path, index=False)
    target.to_csv(target_path, index=False)
    report = {
        "effective_as_of": "2026-09-09",
        "content_fingerprint": "bundle-tested-separately",
        "model": {
            "target_count": 2,
            "score_direction": "higher_is_better",
            "gross_exposure": 1.0,
            "max_weight_per_name": 0.4,
            "tie_policy": "alpha_score_then_instrument_id",
        },
        "ranking": {
            "universe_rows": 3,
            "valid_score_rows": 3,
            "selected_rows": 2,
            "tie_policy": "alpha_score_then_instrument_id",
        },
        "target": {
            "position_weight": 0.4,
            "position_weight_sum": 0.8,
            "cash_weight": 0.2,
        },
    }
    report_path = out / "report.json"
    html_path = out / "report.html"
    report_path.write_text("{}", encoding="utf-8")
    html_path.write_text("ok", encoding="utf-8")
    return DailySnapshot(
        report_path=report_path,
        ranking_path=ranking_path,
        target_path=target_path,
        html_path=html_path,
        report=report,
        reused=True,
    )


def _skip_bundle_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrity, "validate_daily_snapshot_bundle", lambda snapshot: "ok")


def test_valid_semantics_recompute_core_fixed_count_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    _skip_bundle_check(monkeypatch)
    integrity.validate_daily_snapshot_semantics(snapshot)


def test_semantics_rejects_selection_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    _skip_bundle_check(monkeypatch)
    ranking = pd.read_csv(snapshot.ranking_path)
    ranking["selected"] = [False, True, True]
    ranking["target_weight"] = [0.0, 0.4, 0.4]
    ranking.to_csv(snapshot.ranking_path, index=False)

    with pytest.raises(DataValidationError, match="selected instruments"):
        integrity.validate_daily_snapshot_semantics(snapshot)


def test_semantics_rejects_rank_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(tmp_path)
    _skip_bundle_check(monkeypatch)
    ranking = pd.read_csv(snapshot.ranking_path)
    ranking.loc[0, "rank"] = 2
    ranking.to_csv(snapshot.ranking_path, index=False)

    with pytest.raises(DataValidationError, match="alpha rank"):
        integrity.validate_daily_snapshot_semantics(snapshot)


def test_semantics_rejects_target_csv_weight_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    _skip_bundle_check(monkeypatch)
    target = pd.read_csv(snapshot.target_path)
    target.loc[0, "target_weight"] = 0.5
    target.to_csv(snapshot.target_path, index=False)

    with pytest.raises(DataValidationError, match="target CSV target_weight"):
        integrity.validate_daily_snapshot_semantics(snapshot)


def test_semantics_rejects_report_cash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    _skip_bundle_check(monkeypatch)
    snapshot.report["target"]["cash_weight"] = 0.0

    with pytest.raises(DataValidationError, match="target.cash_weight"):
        integrity.validate_daily_snapshot_semantics(snapshot)


def test_semantics_rejects_tie_policy_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    _skip_bundle_check(monkeypatch)
    snapshot.report["model"]["tie_policy"] = "keep_all_cutoff_ties"

    with pytest.raises(DataValidationError, match="tie_policy"):
        integrity.validate_daily_snapshot_semantics(snapshot)


def test_validated_loader_runs_semantic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    called = []
    monkeypatch.setattr(integrity, "load_latest_snapshot", lambda product_root: snapshot)
    monkeypatch.setattr(
        integrity,
        "validate_daily_snapshot_semantics",
        lambda item: called.append(item),
    )

    result = integrity.load_validated_latest_snapshot(tmp_path)

    assert result is snapshot
    assert called == [snapshot]
