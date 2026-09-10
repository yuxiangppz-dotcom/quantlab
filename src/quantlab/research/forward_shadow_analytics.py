"""Read-only diagnostics over immutable Forward Shadow evidence.

These summaries deliberately treat matured forward labels as overlapping
research diagnostics, not as a self-financing portfolio NAV. A 20-session label
recorded every day cannot be compounded into a valid daily equity curve.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from quantlab.research.shadow_timing import prediction_timing


@dataclass(frozen=True)
class ShadowDiagnosticSummary:
    model_id: str
    model_version: str
    prediction_count: int
    complete_evaluation_count: int
    incomplete_evaluation_count: int
    pending_prediction_count: int
    first_signal_date: str | None
    last_signal_date: str | None
    mean_weighted_target_return: float | None
    median_weighted_target_return: float | None
    positive_rate: float | None
    excluded_prediction_count: int = 0


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_prediction(path: Path) -> dict:
    from quantlab.research.forward_shadow import _validate_existing

    payload = json.loads(path.read_text(encoding="utf-8"))
    fingerprint = payload.get("prediction_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(f"invalid forward-shadow prediction fingerprint: {path}")
    _validate_existing(path.parent, fingerprint)
    return payload


def _load_evaluation(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    fingerprint = payload.get("evaluation_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(f"invalid forward-shadow evaluation fingerprint: {path}")
    core = {key: value for key, value in payload.items() if key != "evaluation_fingerprint"}
    if _canonical_hash(core) != fingerprint:
        raise ValueError(f"forward-shadow evaluation fingerprint mismatch: {path}")
    return payload


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _valid_return(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("complete forward-shadow evaluation has invalid return")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("complete forward-shadow evaluation has invalid return")
    return result


def _load_prediction_index(shadow_root: Path) -> tuple[
    dict[tuple[str, str], list[dict]],
    dict[tuple[str, str, str], str],
]:
    predictions_by_model: dict[tuple[str, str], list[dict]] = defaultdict(list)
    prediction_keys: dict[tuple[str, str, str], str] = {}
    for path in sorted(shadow_root.glob("*/*/*/*/prediction.json")):
        payload = _load_prediction(path)
        model = payload.get("model") or {}
        model_id = model.get("model_id")
        version = model.get("version")
        signal_date = payload.get("trade_date")
        fingerprint = payload.get("prediction_fingerprint")
        if not all(isinstance(value, str) and value for value in (model_id, version, signal_date)):
            raise ValueError(f"forward-shadow prediction identity is incomplete: {path}")
        identity = (model_id, version, signal_date)
        predictions_by_model[(model_id, version)].append(payload)
        if not prediction_timing(payload)["forward_eligible"]:
            continue
        previous = prediction_keys.get(identity)
        if previous is not None and previous != fingerprint:
            raise ValueError(
                "multiple immutable predictions exist for the same model/version/signal date"
            )
        prediction_keys[identity] = fingerprint
    return predictions_by_model, prediction_keys


def _load_evaluation_index(
    evaluation_root: Path,
    known_prediction_fingerprints: set[str],
) -> dict[str, dict]:
    evaluations_by_prediction: dict[str, dict] = {}
    if not evaluation_root.exists():
        return evaluations_by_prediction
    for path in sorted(evaluation_root.glob("*/*/*/*/*.json")):
        payload = _load_evaluation(path)
        prediction_fingerprint = payload.get("prediction_fingerprint")
        if not isinstance(prediction_fingerprint, str) or not prediction_fingerprint:
            raise ValueError(f"evaluation is not bound to a prediction: {path}")
        if prediction_fingerprint not in known_prediction_fingerprints:
            raise ValueError("forward-shadow evaluation references an unknown prediction")
        previous = evaluations_by_prediction.get(prediction_fingerprint)
        if previous is not None and previous != payload:
            raise ValueError("multiple different evaluations exist for one prediction")
        evaluations_by_prediction[prediction_fingerprint] = payload
    return evaluations_by_prediction


def summarize_forward_shadow(
    shadow_root: Path,
    *,
    evaluation_root: Path | None = None,
) -> list[ShadowDiagnosticSummary]:
    """Summarize prediction/evaluation coverage without inventing a NAV series."""
    evaluation_root = evaluation_root or shadow_root / "evaluations"
    predictions_by_model, prediction_keys = _load_prediction_index(shadow_root)
    evaluations_by_prediction = _load_evaluation_index(
        evaluation_root,
        {
            item["prediction_fingerprint"]
            for group in predictions_by_model.values() for item in group
        },
    )

    summaries = []
    for (model_id, version), predictions in sorted(predictions_by_model.items()):
        dates = sorted(str(item["trade_date"]) for item in predictions)
        complete_returns: list[float] = []
        incomplete = 0
        pending = 0
        excluded = 0
        for prediction in predictions:
            if not prediction_timing(prediction)["forward_eligible"]:
                excluded += 1
                continue
            evaluation = evaluations_by_prediction.get(prediction["prediction_fingerprint"])
            if evaluation is None:
                pending += 1
                continue
            if (
                evaluation.get("model_id") != model_id
                or evaluation.get("model_version") != version
                or evaluation.get("signal_date") != prediction.get("trade_date")
            ):
                raise ValueError("forward-shadow evaluation identity does not match prediction")
            status = evaluation.get("status")
            if status == "complete":
                complete_returns.append(_valid_return(evaluation.get("weighted_target_return")))
            elif status == "incomplete_missing_target_label":
                incomplete += 1
            else:
                raise ValueError(f"unsupported forward-shadow evaluation status: {status!r}")

        summaries.append(
            ShadowDiagnosticSummary(
                model_id=model_id,
                model_version=version,
                prediction_count=len(predictions),
                complete_evaluation_count=len(complete_returns),
                incomplete_evaluation_count=incomplete,
                pending_prediction_count=pending,
                first_signal_date=dates[0] if dates else None,
                last_signal_date=dates[-1] if dates else None,
                mean_weighted_target_return=(
                    sum(complete_returns) / len(complete_returns) if complete_returns else None
                ),
                median_weighted_target_return=(
                    _median(complete_returns) if complete_returns else None
                ),
                positive_rate=(
                    sum(value > 0 for value in complete_returns) / len(complete_returns)
                    if complete_returns
                    else None
                ),
                excluded_prediction_count=excluded,
            )
        )
    return summaries


def paired_shadow_diagnostics(
    shadow_root: Path,
    left_model: tuple[str, str],
    right_model: tuple[str, str],
    *,
    evaluation_root: Path | None = None,
) -> dict:
    """Compare complete label diagnostics on matching signal dates.

    The returned difference is a paired overlapping-label diagnostic only. It is
    not active return, information ratio, CAGR, or a tradable portfolio claim.
    """
    evaluation_root = evaluation_root or shadow_root / "evaluations"
    predictions_by_model, predictions = _load_prediction_index(shadow_root)
    all_predictions = {
        item["prediction_fingerprint"]: item
        for group in predictions_by_model.values() for item in group
    }
    evaluations = _load_evaluation_index(evaluation_root, set(all_predictions))

    complete_by_fingerprint = {}
    for fingerprint, payload in evaluations.items():
        prediction = all_predictions[fingerprint]
        if not prediction_timing(prediction)["forward_eligible"]:
            continue
        if (
            payload.get("model_id") != prediction["model"]["model_id"]
            or payload.get("model_version") != prediction["model"]["version"]
            or payload.get("signal_date") != prediction["trade_date"]
        ):
            raise ValueError("forward-shadow evaluation identity does not match prediction")
        status = payload.get("status")
        if status == "complete":
            complete_by_fingerprint[fingerprint] = _valid_return(
                payload.get("weighted_target_return")
            )
        elif status != "incomplete_missing_target_label":
            raise ValueError(f"unsupported forward-shadow evaluation status: {status!r}")

    left_dates = {
        signal_date: fingerprint
        for (model_id, version, signal_date), fingerprint in predictions.items()
        if (model_id, version) == left_model
    }
    right_dates = {
        signal_date: fingerprint
        for (model_id, version, signal_date), fingerprint in predictions.items()
        if (model_id, version) == right_model
    }
    rows = []
    for signal_date in sorted(set(left_dates) & set(right_dates)):
        left_value = complete_by_fingerprint.get(left_dates[signal_date])
        right_value = complete_by_fingerprint.get(right_dates[signal_date])
        if left_value is None or right_value is None:
            continue
        rows.append(
            {
                "signal_date": signal_date,
                "left_return": left_value,
                "right_return": right_value,
                "paired_difference": left_value - right_value,
            }
        )
    differences = [row["paired_difference"] for row in rows]
    return {
        "schema": "quantlab_forward_shadow_paired_diagnostic_v1",
        "left_model": {"model_id": left_model[0], "version": left_model[1]},
        "right_model": {"model_id": right_model[0], "version": right_model[1]},
        "paired_complete_count": len(rows),
        "mean_paired_difference": sum(differences) / len(differences) if differences else None,
        "left_win_rate": (
            sum(value > 0 for value in differences) / len(differences) if differences else None
        ),
        "rows": rows,
        "claim": "overlapping_label_diagnostic_not_portfolio_active_return",
    }
