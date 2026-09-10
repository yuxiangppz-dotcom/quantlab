"""Immutable, non-trading forward predictions and matured-label diagnostics."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.daily.integrity import load_validated_latest_snapshot
from quantlab.daily.service import DEFAULT_PRODUCT_ROOT, PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.data.storage import ParquetStorage
from quantlab.portfolio.product import (
    DAILY_FIXED_COUNT_TIE_POLICY,
    construct_daily_fixed_count_portfolio,
)
from quantlab.research.dataset import build_research_dataset
from quantlab.research.shadow_timing import (
    PREDICTION_V1,
    PREDICTION_V2,
    aware_timestamp,
    prediction_timing,
    temporal_admission,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "forward_shadow_v1.json"
DEFAULT_SHADOW_ROOT = PROJECT_ROOT / "data" / "predictions" / "forward_shadow"


@dataclass(frozen=True)
class ShadowResult:
    model_id: str
    prediction_dir: Path
    prediction_fingerprint: str
    reused: bool


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return _sha256_bytes(encoded)


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, lineterminator="\n")
    return buffer.getvalue().encode()


def _load_config(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "quantlab_forward_shadow_config_v1":
        raise DataValidationError("unsupported forward-shadow config schema")
    if not payload.get("models"):
        raise DataValidationError("forward-shadow config has no models")
    seen: set[tuple[str, str]] = set()
    for model in payload["models"]:
        required = {"model_id", "version", "source_column", "direction", "status"}
        if required - set(model):
            raise DataValidationError("forward-shadow model definition is incomplete")
        if model["direction"] not in {"lower_is_better", "higher_is_better"}:
            raise DataValidationError("forward-shadow direction is invalid")
        key = (model["model_id"], model["version"])
        if key in seen:
            raise DataValidationError("duplicate forward-shadow model version")
        seen.add(key)
    count = payload.get("target_count")
    cap = payload.get("max_weight_per_name")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise DataValidationError("forward-shadow target_count must be positive")
    if not isinstance(cap, int | float) or not 0 < cap <= 1:
        raise DataValidationError("forward-shadow max_weight_per_name is invalid")
    horizon = payload.get("label_horizon_sessions")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise DataValidationError("forward-shadow label horizon must be a positive integer")
    return payload


def _validate_existing(path: Path, fingerprint: str) -> None:
    manifest_path = path / "prediction.json"
    if not manifest_path.exists():
        raise DataValidationError("forward-shadow destination exists without prediction marker")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("prediction_fingerprint") != fingerprint:
        raise DataValidationError("forward-shadow destination fingerprint mismatch")
    excluded = {"prediction_fingerprint", "files_sha256", "claims"}
    if manifest.get("schema") == PREDICTION_V1:
        excluded.add("created_at")
    core = {
        key: value
        for key, value in manifest.items()
        if key not in excluded
    }
    if _canonical_hash({"core": core, "files_sha256": manifest.get("files_sha256")}) != fingerprint:
        raise DataValidationError("immutable forward-shadow manifest content mismatch")
    prediction_timing(manifest)
    if manifest.get("schema") == PREDICTION_V2 and (
        path.name != fingerprint
        or path.parent.name != manifest["trade_date"]
        or path.parent.parent.name != manifest["model"]["version"]
        or path.parent.parent.parent.name != manifest["model"]["model_id"]
    ):
        raise DataValidationError("forward-shadow path identity mismatch")
    if manifest.get("claims") != {
        "broker_order": False,
        "fill": False,
        "performance": False,
        "forward_evidence_matured": False,
    }:
        raise DataValidationError("forward-shadow claims are invalid")
    for name in ("scores.csv", "target_portfolio.csv"):
        file_path = path / name
        if not file_path.exists() or _sha256_file(file_path) != manifest["files_sha256"][name]:
            raise DataValidationError(f"immutable forward-shadow file mismatch: {name}")


def _publish_prediction(
    root: Path,
    core: dict,
    scores: pd.DataFrame,
    target: pd.DataFrame,
    created_at: datetime,
) -> ShadowResult:
    scores_bytes = _csv_bytes(scores)
    target_bytes = _csv_bytes(target)
    files_sha = {
        "scores.csv": _sha256_bytes(scores_bytes),
        "target_portfolio.csv": _sha256_bytes(target_bytes),
    }
    # Reuse the first bound observation time on retry, including a later-day
    # retry. A changed configuration/code/score for the same model/date is a
    # conflict, never another opportunity to select a favorable prediction.
    identity_dir = root / core["model"]["model_id"] / core["model"]["version"] / core["trade_date"]
    existing_v2 = []
    for marker in sorted(identity_dir.glob("*/prediction.json")):
        prior = json.loads(marker.read_text(encoding="utf-8"))
        _validate_existing(marker.parent, prior["prediction_fingerprint"])
        if prior.get("schema") == PREDICTION_V2:
            existing_v2.append((marker, prior))
    if len(existing_v2) > 1:
        raise DataValidationError(
            "multiple bound predictions exist for the same model/version/date"
        )
    if existing_v2:
        marker, prior = existing_v2[0]
        retry_core = {
            key: value for key, value in prior.items()
            if key not in {
                "prediction_fingerprint", "files_sha256", "claims", "created_at",
                "temporal_admission",
            }
        }
        if retry_core != core or prior["files_sha256"] != files_sha:
            raise DataValidationError(
                "forward-shadow model/version/date already frozen with other inputs"
            )
        if created_at < aware_timestamp(prior["created_at"], "created_at"):
            raise DataValidationError("forward-shadow retry predates the frozen prediction")
        return ShadowResult(
            core["model"]["model_id"], marker.parent, prior["prediction_fingerprint"], True
        )
    created_text = created_at.astimezone(SHANGHAI).isoformat()
    core = {
        **core,
        "created_at": created_text,
        "temporal_admission": temporal_admission(
            core["trade_date"], created_text, core["source_daily_generated_at"],
        ),
    }
    fingerprint = _canonical_hash({"core": core, "files_sha256": files_sha})
    path = (
        root
        / core["model"]["model_id"]
        / core["model"]["version"]
        / core["trade_date"]
        / fingerprint
    )
    if path.exists():
        _validate_existing(path, fingerprint)
        return ShadowResult(core["model"]["model_id"], path, fingerprint, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{fingerprint}.tmp-", dir=path.parent))
    try:
        (staging / "scores.csv").write_bytes(scores_bytes)
        (staging / "target_portfolio.csv").write_bytes(target_bytes)
        manifest = {
            **core,
            "prediction_fingerprint": fingerprint,
            "files_sha256": files_sha,
            "claims": {
                "broker_order": False,
                "fill": False,
                "performance": False,
                "forward_evidence_matured": False,
            },
        }
        (staging / "prediction.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ShadowResult(core["model"]["model_id"], path, fingerprint, False)


def generate_forward_shadow(
    *,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    shadow_root: Path = DEFAULT_SHADOW_ROOT,
    config_path: Path = DEFAULT_CONFIG_PATH,
    now: datetime | None = None,
) -> list[ShadowResult]:
    """Freeze configured model scores from the active completed Daily snapshot."""
    created_at = now or datetime.now(SHANGHAI)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    snapshot = load_validated_latest_snapshot(product_root)
    if snapshot is None:
        raise FileNotFoundError("no active Daily snapshot; run `quantlab daily` first")
    ranking_bytes = snapshot.ranking_path.read_bytes()
    report_bytes = snapshot.report_path.read_bytes()
    if json.loads(report_bytes) != snapshot.report:
        raise DataValidationError("Daily source changed before forward-shadow capture")
    # Bind the bytes consumed below, not a later reread of a mutable path.
    from quantlab.daily.integrity import validate_daily_snapshot_bundle

    validate_daily_snapshot_bundle(snapshot)
    if (
        snapshot.ranking_path.read_bytes() != ranking_bytes
        or snapshot.report_path.read_bytes() != report_bytes
    ):
        raise DataValidationError("Daily source changed during forward-shadow capture")
    ranking = pd.read_csv(io.BytesIO(ranking_bytes))
    config = _load_config(config_path)
    head = _git_head()
    config_fingerprint = _sha256_file(config_path)
    signal_date = date.fromisoformat(snapshot.report["effective_as_of"])
    results = []
    for model in config["models"]:
        source = model["source_column"]
        if source not in ranking:
            raise DataValidationError(f"Daily ranking missing shadow source column: {source}")
        scored = ranking[["instrument_id", "name", "board", "trade_date", source]].copy()
        scored = scored.rename(columns={source: "alpha_score"})
        scored["alpha_score"] = pd.to_numeric(scored["alpha_score"], errors="coerce")
        scored = scored[scored["alpha_score"].notna()].copy()

        product_config = {
            "target_count": config["target_count"],
            "score_direction": model["direction"],
            "gross_exposure": 1.0,
            "max_weight_per_name": config["max_weight_per_name"],
            "tie_policy": DAILY_FIXED_COUNT_TIE_POLICY,
        }
        portfolio = construct_daily_fixed_count_portfolio(
            scored[["instrument_id", "trade_date", "alpha_score"]],
            signal_date,
            product_config,
        )
        target_weights = {
            position.instrument_id: position.target_weight for position in portfolio.positions
        }

        ascending = model["direction"] == "lower_is_better"
        scored = scored.sort_values(
            ["alpha_score", "instrument_id"],
            ascending=[ascending, True],
            kind="mergesort",
        ).reset_index(drop=True)
        scored["rank"] = range(1, len(scored) + 1)
        scored["selected"] = scored["instrument_id"].isin(target_weights)
        scored["target_weight"] = (
            scored["instrument_id"].map(target_weights).fillna(0.0).astype(float)
        )
        target = scored[scored["selected"]][
            ["instrument_id", "name", "rank", "alpha_score", "target_weight"]
        ].copy()
        selected = len(portfolio.positions)
        core = {
            "schema": PREDICTION_V2,
            "trade_date": snapshot.report["effective_as_of"],
            "source_daily_generated_at": snapshot.report["generated_at"],
            "daily_report_sha256": _sha256_bytes(report_bytes),
            "model": model,
            "config_id": config["config_id"],
            "config_fingerprint": config_fingerprint,
            "daily_content_fingerprint": snapshot.report["content_fingerprint"],
            "daily_ranking_sha256": _sha256_bytes(ranking_bytes),
            "universe": config["universe"],
            "universe_rows": len(scored),
            "target_count": selected,
            "target_weight_sum": round(
                sum(position.target_weight for position in portfolio.positions), 12
            ),
            "cash_weight": round(portfolio.cash_weight, 12),
            "tie_policy": f"{model['direction']}_alpha_score_then_instrument_id",
            "portfolio_contract": {
                "constructor": "fixed_count_v1",
                "requested_target_count": config["target_count"],
                "score_direction": model["direction"],
                "gross_exposure": 1.0,
                "max_weight_per_name": config["max_weight_per_name"],
                "tie_policy": DAILY_FIXED_COUNT_TIE_POLICY,
            },
            "code_head": head,
            "label": {
                "name": f"diagnostic_future_return_{config['label_horizon_sessions']}d",
                "horizon_sessions": config["label_horizon_sessions"],
                "status": "pending",
            },
        }
        results.append(_publish_prediction(shadow_root, core, scored, target, created_at))
    return results


def _open_sessions(storage: ParquetStorage) -> list[date]:
    return sorted({item.trade_date for item in storage.load_trading_calendar() if item.is_open})


def evaluate_matured_forward_shadows(
    *,
    storage: ParquetStorage | None = None,
    shadow_root: Path = DEFAULT_SHADOW_ROOT,
    evaluation_root: Path | None = None,
) -> list[Path]:
    """Append diagnostics for predictions with a fully available 20-session label.

    Evaluations live outside prediction directories. Missing target labels make
    the diagnostic incomplete rather than silently dropping the instrument.
    Every prediction is integrity-checked before any maturity or return
    calculation so a mutated manifest, score file, or target portfolio can never
    become evaluation evidence.
    """
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    evaluation_root = evaluation_root or shadow_root / "evaluations"
    sessions = _open_sessions(storage)
    manifests = sorted(shadow_root.glob("*/*/*/*/prediction.json"))
    written: list[Path] = []
    dataset_cache: dict[tuple[date, int], pd.DataFrame] = {}
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fingerprint = manifest.get("prediction_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise DataValidationError("forward-shadow prediction fingerprint is missing or invalid")
        _validate_existing(manifest_path.parent, fingerprint)
        if not prediction_timing(manifest)["forward_eligible"]:
            continue

        signal_date = date.fromisoformat(manifest["trade_date"])
        horizon = int(manifest["label"]["horizon_sessions"])
        if signal_date not in sessions:
            continue
        index = sessions.index(signal_date)
        if index + horizon >= len(sessions):
            continue
        label_date = sessions[index + horizon]
        if not storage.daily_bars_exists(label_date) or not storage.adj_factor_exists(label_date):
            continue
        cache_key = (signal_date, horizon)
        if cache_key not in dataset_cache:
            dataset_cache[cache_key] = build_research_dataset(
                storage,
                signal_date,
                signal_date,
                return_horizons=(),
                forward_horizons=(horizon,),
            )
        labels = dataset_cache[cache_key][["instrument_id", f"future_return_{horizon}d"]]
        target_path = manifest_path.parent / "target_portfolio.csv"
        target = pd.read_csv(target_path)
        joined = target.merge(labels, on="instrument_id", how="left", validate="one_to_one")
        missing = sorted(joined.loc[joined[f"future_return_{horizon}d"].isna(), "instrument_id"])
        used_sessions = sessions[index : index + horizon + 1]
        label_inputs = {
            day.isoformat(): {
                "daily": _sha256_file(storage.daily_bars_path(day)),
                "adj_factor": _sha256_file(storage.adj_factor_path(day)),
            }
            for day in used_sessions
        }
        status = "complete" if not missing else "incomplete_missing_target_label"
        weighted_return = None
        if not missing:
            weighted_return = float(
                (joined[f"future_return_{horizon}d"] * joined["target_weight"]).sum()
            )
        core = {
            "schema": "quantlab_forward_shadow_evaluation_v1",
            "prediction_fingerprint": manifest["prediction_fingerprint"],
            "model_id": manifest["model"]["model_id"],
            "model_version": manifest["model"]["version"],
            "signal_date": signal_date.isoformat(),
            "label_date": label_date.isoformat(),
            "label": f"diagnostic_future_return_{horizon}d",
            "status": status,
            "target_count": len(target),
            "missing_target_count": len(missing),
            "missing_target_sample": missing[:10],
            "weighted_target_return": weighted_return,
            "label_input_sha256": label_inputs,
            "not_a_backtest_or_execution_claim": True,
        }
        evaluation_fingerprint = _canonical_hash(core)
        out = (
            evaluation_root
            / manifest["model"]["model_id"]
            / manifest["model"]["version"]
            / signal_date.isoformat()
            / manifest["prediction_fingerprint"]
            / f"{evaluation_fingerprint}.json"
        )
        if out.exists():
            if json.loads(out.read_text(encoding="utf-8")) != {
                **core,
                "evaluation_fingerprint": evaluation_fingerprint,
            }:
                raise DataValidationError("immutable forward-shadow evaluation mismatch")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {**core, "evaluation_fingerprint": evaluation_fingerprint}
        descriptor, temp_name = tempfile.mkstemp(dir=out.parent, prefix=out.name, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, out)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        written.append(out)
    return written


def latest_forward_shadow(
    shadow_root: Path = DEFAULT_SHADOW_ROOT,
) -> list[dict]:
    """Load the newest valid prediction for each configured model/version."""
    rows = []
    for manifest_path in shadow_root.glob("*/*/*/*/prediction.json"):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_existing(manifest_path.parent, payload["prediction_fingerprint"])
        rows.append({
            **payload,
            "temporal_admission": prediction_timing(payload),
            "prediction_dir": str(manifest_path.parent),
        })
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["model"]["model_id"], row["model"]["version"])
        if key not in latest or (row["trade_date"], row["created_at"]) > (
            latest[key]["trade_date"],
            latest[key]["created_at"],
        ):
            latest[key] = row
    return sorted(latest.values(), key=lambda item: item["model"]["model_id"])
