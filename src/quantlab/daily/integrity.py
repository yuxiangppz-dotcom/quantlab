"""Integrity checks for materialized Daily product snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.daily.service import (
    DEFAULT_PRODUCT_ROOT,
    DailySnapshot,
    load_latest_snapshot,
)
from quantlab.data.models import DataValidationError
from quantlab.portfolio.product import (
    construct_daily_fixed_count_portfolio,
    fixed_count_config_from_daily,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _fingerprint_report(report: dict) -> dict:
    core = {
        key: value
        for key, value in report.items()
        if key not in {"generated_at", "content_fingerprint"}
    }
    data_status = core.get("data_status")
    if isinstance(data_status, dict):
        core["data_status"] = {
            key: value for key, value in data_status.items() if key != "inspected_at"
        }
    return core


def validate_daily_snapshot_bundle(snapshot: DailySnapshot) -> str:
    """Validate report-to-ranking/target byte binding for a materialized snapshot."""
    required_paths = {
        "report": snapshot.report_path,
        "ranking": snapshot.ranking_path,
        "target": snapshot.target_path,
        "html": snapshot.html_path,
    }
    missing = [name for name, path in required_paths.items() if not path.is_file()]
    if missing:
        raise DataValidationError(f"Daily snapshot bundle missing files: {sorted(missing)}")

    try:
        disk_report = json.loads(snapshot.report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataValidationError("Daily snapshot report is unreadable or invalid JSON") from exc
    if disk_report != snapshot.report:
        raise DataValidationError("Daily snapshot in-memory report differs from report.json")

    fingerprint = disk_report.get("content_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise DataValidationError("Daily snapshot content fingerprint is missing or invalid")

    ranking_sha = _sha256_bytes(snapshot.ranking_path.read_bytes())
    target_sha = _sha256_bytes(snapshot.target_path.read_bytes())
    actual = _canonical_hash(
        {
            "report": _fingerprint_report(disk_report),
            "ranking_sha256": ranking_sha,
            "target_sha256": target_sha,
        }
    )
    if actual != fingerprint:
        raise DataValidationError("Daily snapshot content fingerprint mismatch")
    return fingerprint


def _boolean_series(series: pd.Series, field: str) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.astype(bool)
    normalized = series.astype(str).str.strip().str.lower()
    if not normalized.isin({"true", "false"}).all():
        raise DataValidationError(f"Daily {field} must contain only booleans")
    return normalized.eq("true")


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DataValidationError(f"Daily {field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise DataValidationError(f"Daily {field} must be finite")
    return result


def _assert_close(actual: float, expected: float, field: str) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
        raise DataValidationError(
            f"Daily {field} does not match core portfolio contract: {actual} != {expected}"
        )


def validate_daily_snapshot_semantics(snapshot: DailySnapshot) -> None:
    """Recompute the Daily portfolio contract from stored scores and compare outputs.

    Byte integrity alone proves that files were not mutated after publication; it
    does not prove that the materialized ``selected``, ``rank`` and target weights
    obey the product's canonical fixed-count constructor. This check closes that
    gap without changing the frozen research backtest semantics.
    """
    validate_daily_snapshot_bundle(snapshot)
    try:
        effective = date.fromisoformat(str(snapshot.report["effective_as_of"]))
        model = snapshot.report["model"]
        report_ranking = snapshot.report["ranking"]
        report_target = snapshot.report["target"]
    except (KeyError, TypeError, ValueError) as exc:
        raise DataValidationError("Daily report is missing portfolio semantic metadata") from exc
    if not isinstance(model, dict) or not isinstance(report_ranking, dict) or not isinstance(
        report_target, dict
    ):
        raise DataValidationError("Daily report portfolio semantic metadata must be objects")
    portfolio_config = fixed_count_config_from_daily(model)

    try:
        ranking = pd.read_csv(snapshot.ranking_path)
        target = pd.read_csv(snapshot.target_path)
    except (OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise DataValidationError("Daily ranking or target CSV is unreadable") from exc

    ranking_required = {
        "instrument_id",
        "trade_date",
        "alpha_score",
        "rank",
        "selected",
        "target_weight",
    }
    target_required = {"instrument_id", "rank", "alpha_score", "target_weight"}
    if missing := ranking_required - set(ranking.columns):
        raise DataValidationError(f"Daily ranking missing semantic columns: {sorted(missing)}")
    if missing := target_required - set(target.columns):
        raise DataValidationError(f"Daily target missing semantic columns: {sorted(missing)}")
    if ranking["instrument_id"].duplicated().any():
        raise DataValidationError("Daily ranking contains duplicate instruments")
    if target["instrument_id"].duplicated().any():
        raise DataValidationError("Daily target contains duplicate instruments")

    try:
        trade_dates = pd.to_datetime(ranking["trade_date"], errors="raise").dt.date
    except (TypeError, ValueError) as exc:
        raise DataValidationError("Daily ranking trade_date is invalid") from exc
    if len(ranking) and not trade_dates.eq(effective).all():
        raise DataValidationError("Daily ranking contains a trade_date outside effective_as_of")

    ranking = ranking.copy()
    ranking["trade_date"] = trade_dates
    ranking["alpha_score"] = pd.to_numeric(ranking["alpha_score"], errors="coerce")
    selected = _boolean_series(ranking["selected"], "selected")
    weights = pd.to_numeric(ranking["target_weight"], errors="coerce")
    if weights.isna().any() or (~weights.map(math.isfinite)).any() or (weights < 0).any():
        raise DataValidationError("Daily ranking target_weight must be finite and non-negative")

    valid = ranking[ranking["alpha_score"].notna()].copy()
    ascending = portfolio_config.score_direction == "lower_is_better"
    ordered = valid.sort_values(
        ["alpha_score", "instrument_id"],
        ascending=[ascending, True],
        kind="mergesort",
    )
    expected_ranks = {
        instrument_id: rank
        for rank, instrument_id in enumerate(ordered["instrument_id"], start=1)
    }
    ranks = pd.to_numeric(ranking["rank"], errors="coerce")
    for instrument_id, score, rank in zip(
        ranking["instrument_id"], ranking["alpha_score"], ranks, strict=True
    ):
        if pd.isna(score):
            if not pd.isna(rank):
                raise DataValidationError("Daily unscored instrument must not have an alpha rank")
            continue
        expected_rank = expected_ranks[instrument_id]
        if pd.isna(rank) or float(rank) != expected_rank:
            raise DataValidationError("Daily alpha rank does not match deterministic score ordering")

    if ranking.empty:
        expected_weights: dict[str, float] = {}
        expected_cash = 1.0
    else:
        portfolio = construct_daily_fixed_count_portfolio(
            ranking[["instrument_id", "trade_date", "alpha_score"]],
            effective,
            model,
        )
        expected_weights = {
            item.instrument_id: item.target_weight for item in portfolio.positions
        }
        expected_cash = portfolio.cash_weight

    expected_ids = set(expected_weights)
    selected_ids = set(ranking.loc[selected, "instrument_id"])
    if selected_ids != expected_ids:
        raise DataValidationError("Daily selected instruments do not match core portfolio contract")
    for instrument_id, weight in zip(ranking["instrument_id"], weights, strict=True):
        _assert_close(float(weight), expected_weights.get(instrument_id, 0.0), "ranking target_weight")

    target_ids = set(target["instrument_id"])
    if target_ids != expected_ids or len(target) != len(expected_ids):
        raise DataValidationError("Daily target CSV does not match selected core portfolio instruments")
    target_weights = pd.to_numeric(target["target_weight"], errors="coerce")
    target_ranks = pd.to_numeric(target["rank"], errors="coerce")
    target_scores = pd.to_numeric(target["alpha_score"], errors="coerce")
    if (
        target_weights.isna().any()
        or (~target_weights.map(math.isfinite)).any()
        or (target_weights < 0).any()
        or target_ranks.isna().any()
        or target_scores.isna().any()
    ):
        raise DataValidationError("Daily target contains invalid semantic values")
    ranking_by_id = ranking.set_index("instrument_id")
    for instrument_id, rank, score, weight in zip(
        target["instrument_id"],
        target_ranks,
        target_scores,
        target_weights,
        strict=True,
    ):
        _assert_close(float(weight), expected_weights[instrument_id], "target CSV target_weight")
        if float(rank) != expected_ranks[instrument_id]:
            raise DataValidationError("Daily target rank does not match ranking")
        _assert_close(
            float(score),
            float(ranking_by_id.at[instrument_id, "alpha_score"]),
            "target CSV alpha_score",
        )

    if report_ranking.get("tie_policy") != model.get("tie_policy"):
        raise DataValidationError("Daily report ranking tie policy differs from model config")
    if report_ranking.get("universe_rows") != len(ranking):
        raise DataValidationError("Daily report universe_rows does not match ranking")
    if report_ranking.get("selected_rows") != len(expected_ids):
        raise DataValidationError("Daily report selected_rows does not match core portfolio")
    if report_ranking.get("valid_score_rows") != len(valid):
        raise DataValidationError("Daily report valid_score_rows does not match ranking")

    expected_weight_sum = sum(expected_weights.values())
    _assert_close(
        _finite_float(report_target.get("position_weight_sum"), "target.position_weight_sum"),
        expected_weight_sum,
        "target.position_weight_sum",
    )
    _assert_close(
        _finite_float(report_target.get("cash_weight"), "target.cash_weight"),
        expected_cash,
        "target.cash_weight",
    )
    expected_per_name = next(iter(expected_weights.values()), 0.0)
    _assert_close(
        _finite_float(report_target.get("position_weight"), "target.position_weight"),
        expected_per_name,
        "target.position_weight",
    )


def load_validated_latest_snapshot(
    product_root: Path = DEFAULT_PRODUCT_ROOT,
) -> DailySnapshot | None:
    """Load the active/newest Daily snapshot and fail closed on byte/semantic drift."""
    snapshot = load_latest_snapshot(product_root)
    if snapshot is not None:
        validate_daily_snapshot_semantics(snapshot)
    return snapshot
