"""Deterministic change diagnostics between validated Daily snapshots.

This module compares two already-materialized Daily targets. It does not call a
provider, infer tradability, or claim that target turnover equals executed
turnover. Both inputs must pass the same byte and portfolio-semantic integrity
gates used by reference planning and Forward Shadow.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from quantlab.daily.integrity import validate_daily_snapshot_semantics
from quantlab.daily.service import DailySnapshot
from quantlab.data.models import DataValidationError

_CONTRACT_FIELDS = (
    "strategy_id",
    "score_definition",
    "score_direction",
    "target_count",
    "max_weight_per_name",
    "gross_exposure",
    "tie_policy",
    "allowed_boards",
)


@dataclass(frozen=True)
class DailyRankChange:
    instrument_id: str
    name: str | None
    previous_rank: int
    current_rank: int
    rank_improvement: int


@dataclass(frozen=True)
class DailySelectionChange:
    instrument_id: str
    name: str | None
    rank: int
    target_weight: float


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _date(snapshot: DailySnapshot) -> date:
    try:
        return date.fromisoformat(str(snapshot.report["effective_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataValidationError("Daily snapshot effective_as_of is invalid") from exc


def _contract(snapshot: DailySnapshot) -> dict:
    model = snapshot.report.get("model")
    if not isinstance(model, dict):
        raise DataValidationError("Daily snapshot model config is missing")
    return {field: model.get(field) for field in _CONTRACT_FIELDS}


def _ranking(snapshot: DailySnapshot) -> pd.DataFrame:
    frame = pd.read_csv(snapshot.ranking_path)
    required = {"instrument_id", "rank", "selected", "target_weight"}
    missing = required - set(frame.columns)
    if missing:
        raise DataValidationError(f"Daily ranking missing change columns: {sorted(missing)}")
    frame = frame.copy()
    frame["rank"] = pd.to_numeric(frame["rank"], errors="coerce")
    frame["target_weight"] = pd.to_numeric(frame["target_weight"], errors="coerce")
    if frame["target_weight"].isna().any() or not frame["target_weight"].map(math.isfinite).all():
        raise DataValidationError("Daily ranking target_weight is invalid")
    if pd.api.types.is_bool_dtype(frame["selected"]):
        frame["selected"] = frame["selected"].astype(bool)
    else:
        normalized = frame["selected"].astype(str).str.strip().str.lower()
        if not normalized.isin({"true", "false"}).all():
            raise DataValidationError("Daily ranking selected is invalid")
        frame["selected"] = normalized.eq("true")
    if "name" not in frame.columns:
        frame["name"] = None
    return frame


def _selected_rows(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        str(row.instrument_id): row
        for _, row in frame.loc[frame["selected"]].iterrows()
    }


def _selection_change(row: pd.Series) -> DailySelectionChange:
    rank = row["rank"]
    if pd.isna(rank):
        raise DataValidationError("selected Daily instrument is missing rank")
    name = row.get("name")
    return DailySelectionChange(
        instrument_id=str(row["instrument_id"]),
        name=None if pd.isna(name) else str(name),
        rank=int(rank),
        target_weight=float(row["target_weight"]),
    )


def compare_daily_snapshots(previous: DailySnapshot, current: DailySnapshot) -> dict:
    """Compare two validated Daily snapshots using target-to-target semantics.

    ``one_way_target_turnover`` is the half-L1 distance between the two complete
    target portfolios, including cash. It is a desired-target change metric, not
    an estimate of fills, fees, market impact, or executable turnover.
    """
    validate_daily_snapshot_semantics(previous)
    validate_daily_snapshot_semantics(current)
    previous_date = _date(previous)
    current_date = _date(current)
    if current_date <= previous_date:
        raise DataValidationError(
            "Daily change comparison requires current effective_as_of after previous"
        )

    previous_frame = _ranking(previous)
    current_frame = _ranking(current)
    previous_selected = _selected_rows(previous_frame)
    current_selected = _selected_rows(current_frame)
    previous_ids = set(previous_selected)
    current_ids = set(current_selected)

    entries = sorted(
        (_selection_change(current_selected[item]) for item in current_ids - previous_ids),
        key=lambda item: (item.rank, item.instrument_id),
    )
    exits = sorted(
        (_selection_change(previous_selected[item]) for item in previous_ids - current_ids),
        key=lambda item: (item.rank, item.instrument_id),
    )

    previous_ranked = previous_frame.dropna(subset=["rank"]).set_index("instrument_id")
    current_ranked = current_frame.dropna(subset=["rank"]).set_index("instrument_id")
    common_ranked = set(previous_ranked.index) & set(current_ranked.index)
    rank_changes = []
    for instrument_id in common_ranked:
        previous_rank = int(previous_ranked.at[instrument_id, "rank"])
        current_rank = int(current_ranked.at[instrument_id, "rank"])
        name = current_ranked.at[instrument_id, "name"]
        rank_changes.append(
            DailyRankChange(
                instrument_id=str(instrument_id),
                name=None if pd.isna(name) else str(name),
                previous_rank=previous_rank,
                current_rank=current_rank,
                rank_improvement=previous_rank - current_rank,
            )
        )
    rank_changes.sort(key=lambda item: (-item.rank_improvement, item.instrument_id))

    previous_weights = dict(
        zip(previous_frame["instrument_id"], previous_frame["target_weight"], strict=True)
    )
    current_weights = dict(
        zip(current_frame["instrument_id"], current_frame["target_weight"], strict=True)
    )
    instruments = set(previous_weights) | set(current_weights)
    stock_abs_change = sum(
        abs(float(current_weights.get(item, 0.0)) - float(previous_weights.get(item, 0.0)))
        for item in instruments
    )
    previous_cash = float(previous.report["target"]["cash_weight"])
    current_cash = float(current.report["target"]["cash_weight"])
    if not all(math.isfinite(value) for value in (previous_cash, current_cash)):
        raise DataValidationError("Daily target cash weight is invalid")
    one_way_turnover = 0.5 * (stock_abs_change + abs(current_cash - previous_cash))

    overlap = previous_ids & current_ids
    union = previous_ids | current_ids
    previous_contract = _contract(previous)
    current_contract = _contract(current)
    same_contract = previous_contract == current_contract
    core = {
        "schema": "quantlab_daily_change_v1",
        "previous_effective_as_of": previous_date.isoformat(),
        "current_effective_as_of": current_date.isoformat(),
        "previous_content_fingerprint": previous.report["content_fingerprint"],
        "current_content_fingerprint": current.report["content_fingerprint"],
        "portfolio_contract_same": same_contract,
        "change_interpretation": (
            "same_contract_signal_movement"
            if same_contract
            else "portfolio_contract_changed_not_pure_signal_movement"
        ),
        "selected": {
            "previous_count": len(previous_ids),
            "current_count": len(current_ids),
            "retained_count": len(overlap),
            "new_entry_count": len(entries),
            "exit_count": len(exits),
            "overlap_jaccard": len(overlap) / len(union) if union else 1.0,
        },
        "new_entries": [asdict(item) for item in entries],
        "exits": [asdict(item) for item in exits],
        "rank_changes": [asdict(item) for item in rank_changes],
        "target_change": {
            "stock_absolute_weight_change": stock_abs_change,
            "cash_weight_change": current_cash - previous_cash,
            "one_way_target_turnover": one_way_turnover,
            "meaning": "half_l1_target_distance_including_cash_not_executed_turnover",
        },
        "claims": {
            "performance_claim": False,
            "execution_claim": False,
            "fill_claim": False,
        },
    }
    return {**core, "comparison_fingerprint": _canonical_hash(core)}
