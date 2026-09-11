"""Frozen-model retrospective scores, with no future-label-dependent membership."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.dataset import _build_delist_dates, _build_list_dates
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import (
    KEYS,
    InputBinding,
    code_binding,
    sealed_read,
    sealed_write,
    verify_entries,
)
from quantlab.research.round2_diagnostics import ROUND_DIRECTORY, load_report, model_matrix
from quantlab.research.round2_features import AUGMENTED_FEATURES, BASELINE_FEATURES
from quantlab.research.universe import is_v1_a_share

CONFIG_PATH = "config/research_signal_feasibility_v1.json"
CONFIG_SHA = "6b6e17b2cfce1b0b613f5c3494cda30a4f01f0eb08ed4f30293adc8acb2a4324"
OUTPUT_DIRECTORY = "data/products/signal_feasibility/signal_feasibility_20260911"
FEATURE_COLUMNS = [*AUGMENTED_FEATURES, "transparent_combo_v1"]
READ_COLUMNS = [*KEYS, *FEATURE_COLUMNS]
SCORE_COLUMNS = [
    f"{group}_{horizon}d"
    for group in ("lightgbm_baseline", "transparent_combo_v1")
    for horizon in (5, 10, 20)
]


def load_contract(root: Path = PROJECT_ROOT) -> dict:
    raw = (root / CONFIG_PATH).read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONFIG_SHA:
        raise DataValidationError("predeclared feasibility contract changed")
    return json.loads(raw)


def score_cohort(frame, contract, lists, delists):
    """Only date, identity and contemporaneous finite features may choose rows."""
    data = frame[READ_COLUMNS].copy()
    dates = pd.to_datetime(data.trade_date)
    if dates.isna().any() or not dates.eq(dates.dt.normalize()).all():
        raise DataValidationError("invalid signal session dates")
    data["trade_date"] = dates
    if data[KEYS].isna().any().any() or data.duplicated(KEYS).any():
        raise DataValidationError("invalid or duplicate signal keys")
    data = data.loc[dates.between(contract["signal_start"], contract["signal_end"])].copy()
    if not set(data.instrument_id).issubset(lists):
        raise DataValidationError("signal identity missing from bound lifecycle")
    start = pd.to_datetime(data.instrument_id.astype(str).map(lists))
    end = pd.to_datetime(data.instrument_id.astype(str).map(delists))
    active = data.trade_date.ge(start) & (end.isna() | data.trade_date.le(end))
    scope = data.instrument_id.astype(str).map(is_v1_a_share)
    finite = np.isfinite(data[FEATURE_COLUMNS]).all(axis=1)
    reasons = np.select(
        [~active, ~scope, ~finite],
        ["inactive_code_or_lifecycle", "outside_v1_scope", "incomplete_features"],
        default="score_available",
    )
    excluded = data.loc[reasons != "score_available", KEYS].copy()
    excluded["reason"] = reasons[reasons != "score_available"]
    excluded["missing_features"] = data.loc[excluded.index, FEATURE_COLUMNS].apply(
        lambda row: ",".join(row.index[~np.isfinite(row)]), axis=1
    )
    selected = data.loc[reasons == "score_available"].sort_values(KEYS).reset_index(drop=True)
    return selected, excluded.reset_index(drop=True)


def validate_model_metadata(intent, prep, result, stage_fingerprint, horizon, contract):
    if (
        result["status"] != "complete"
        or intent["group"] != "lightgbm_baseline"
        or intent["horizon"] != horizon
        or intent["features"] != list(BASELINE_FEATURES)
        or intent["dataset_fingerprint"] != stage_fingerprint
        or intent["target"] != f"future_return_{horizon}d"
        or prep["fitted_period"] != "discovery"
        or prep["dtype"] != "float32"
        or prep["label_transform"] != "none"
    ):
        raise DataValidationError("saved model metadata mismatch")
    signal_max = pd.Timestamp(prep["max_signal_date"])
    label_max = pd.Timestamp(prep["max_label_end_date"])
    if (
        not signal_max
        <= label_max
        <= pd.Timestamp("2022-12-31")
        < pd.Timestamp(contract["signal_start"])
    ):
        raise DataValidationError("model training boundary overlaps exported signals")
    medians = prep["medians"]
    if set(medians) != set(BASELINE_FEATURES) or not np.isfinite(list(medians.values())).all():
        raise DataValidationError("invalid saved training medians")
    # A real creation time is retained separately from hypothetical signal dates.
    created = datetime.fromisoformat(result["finished_at"])
    if created.tzinfo is None:
        raise DataValidationError("model creation time must be aware")


def load_saved_models(source, stage, contract):
    from lightgbm import Booster

    models, identities = {}, {}
    for horizon in contract["horizons"]:
        folder = source / f"results/models/lightgbm_baseline_{horizon}d"
        intent = sealed_read(folder / "intent.json")
        prep = sealed_read(folder / "preprocessing.json")
        result = sealed_read(folder / "result.json")
        verify_entries(folder, result["artifacts"])
        validate_model_metadata(intent, prep, result, stage["fingerprint"], horizon, contract)
        model = Booster(model_file=str(folder / "model.txt"))
        if model.feature_name() != list(BASELINE_FEATURES):
            raise DataValidationError("native saved model feature order differs")
        models[horizon] = (model, pd.Series(prep["medians"]))
        identities[str(horizon)] = {
            "intent": intent["fingerprint"],
            "preprocessing": prep["fingerprint"],
            "model_sha256": _sha(folder / "model.txt"),
            "trained_at": result["finished_at"],
            "max_training_signal": prep["max_signal_date"],
            "max_training_label_end": prep["max_label_end_date"],
        }
    return models, identities


def predict_scores(selected, models):
    scores = selected[KEYS].copy()
    for horizon in (5, 10, 20):
        model, medians = models[horizon]
        scores[f"lightgbm_baseline_{horizon}d"] = (
            model.predict(model_matrix(selected, BASELINE_FEATURES, medians), num_threads=2)
            if len(selected)
            else np.empty(0)
        )
        scores[f"transparent_combo_v1_{horizon}d"] = selected.transparent_combo_v1.to_numpy()
    if not np.isfinite(scores[SCORE_COLUMNS]).all().all():
        raise DataValidationError("saved model returned nonfinite scores")
    scores["research_status"] = "retrospective_score_only"
    scores["execution_eligible"] = False
    return scores


def run_export(root: Path = PROJECT_ROOT, *, progress=print):
    from quantlab.research.signal_portfolio_audit import audit_portfolio_inputs

    contract = load_contract(root)
    source, out = root / ROUND_DIRECTORY, root / OUTPUT_DIRECTORY
    binding = InputBinding(root)
    head = code_binding(root, binding)
    original = load_report(source, full_verify=True)
    stage = sealed_read(source / "stage/manifest.json")
    if (
        stage["fingerprint"] != contract["stage_fingerprint"]
        or original["fingerprint"] != contract["diagnostic_report_fingerprint"]
    ):
        raise DataValidationError("source round is not the predeclared frozen round")
    # Original input code is still unchanged at this implementation/run part.
    # Later presentation edits do not rewrite the original sealed round.
    verify_entries(root, stage["inputs"])
    for path in (source / "stage/manifest.json", source / "results/report.json"):
        binding.read(path)
    models, identities = load_saved_models(source, stage, contract)
    out.mkdir(parents=True, exist_ok=False)
    sealed_write(
        out / "started.json",
        {
            "started_at": datetime.now(UTC).isoformat(),
            "code_head": head,
            "contract": contract,
            "model_fit_budget": 0,
            "models": identities,
        },
    )
    try:
        storage = ParquetStorage(root / "data/canonical")
        securities = storage.load_securities()
        changes = load_security_code_changes(root / "config/security_code_changes.csv")
        lists, delists = (
            _build_list_dates(securities, changes),
            _build_delist_dates(securities, changes),
        )
        # Physical column projection means labels cannot even reach the score path.
        frame = pd.read_parquet(source / "stage/dataset.parquet", columns=READ_COLUMNS)
        selected, excluded = score_cohort(frame, contract, lists, delists)
        del frame
        excluded.to_parquet(out / "exclusions.parquet", index=False)
        progress(
            f"label-independent cohort: {len(selected)}; excluded: {len(excluded)}", flush=True
        )
        score_dir = out / "scores"
        score_dir.mkdir()
        all_scores = []
        for year, part in selected.groupby(selected.trade_date.dt.year):
            scores = predict_scores(part, models)
            scores.to_parquet(score_dir / f"{year}.parquet", index=False)
            all_scores.append(scores)
            progress(f"exported scores: {year}, {len(scores)} rows; zero fits", flush=True)
        del selected
        scores = pd.concat(all_scores, ignore_index=True)
        audit = audit_portfolio_inputs(root, out, scores, contract, binding, progress=progress)
        verify_entries(root, stage["inputs"])
        load_report(source, full_verify=True)
        binding.check()
        artifacts = {
            p.relative_to(out).as_posix(): {"sha256": _sha(p)}
            for p in sorted(out.rglob("*"))
            if p.is_file()
        }
        report = {
            "schema_version": 1,
            "status": "complete_with_portfolio_blockers",
            "code_head": head,
            "completed_at": datetime.now(UTC).isoformat(),
            "contract": contract,
            "source_stage": stage["fingerprint"],
            "source_report": original["fingerprint"],
            "models": identities,
            "new_fit_attempts": 0,
            "source_round_fit_attempts": original["fit_attempts"],
            "score_rows_per_candidate": len(scores),
            "candidate_count": 6,
            "score_dates": scores.trade_date.nunique(),
            "score_date_range": [
                str(scores.trade_date.min().date()),
                str(scores.trade_date.max().date()),
            ],
            "excluded_rows": len(excluded),
            "exclusion_counts": excluded.reason.value_counts().to_dict(),
            "source_scope_exclusions": {
                "location": f"{ROUND_DIRECTORY}/stage/raw",
                "binding": stage["fingerprint"],
                "note": (
                    "source raw partitions retain inactive/out-of-scope identities; "
                    "no absent bars synthesized"
                ),
            },
            "audit": audit,
            "inputs": binding.entries,
            "artifacts": artifacts,
            "performance_eligible": False,
            "execution_authority": False,
            "fresh_forward_evidence": False,
            "net_return_assessed": False,
            "drawdown_assessed": False,
            "strategy_promoted": False,
        }
        return sealed_write(out / "report.json", report)
    except BaseException as exc:
        sealed_write(
            out / "failed.json",
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "at": datetime.now(UTC).isoformat(),
                "new_fit_attempts": 0,
            },
        )
        raise


def load_feasibility_report(out: Path, *, full_verify=False):
    report = sealed_read(out / "report.json")
    if report["contract"] != load_contract():
        raise DataValidationError("feasibility report contract mismatch")
    for flag in (
        "performance_eligible",
        "execution_authority",
        "fresh_forward_evidence",
        "net_return_assessed",
        "drawdown_assessed",
        "strategy_promoted",
    ):
        if report.get(flag) is not False:
            raise DataValidationError("feasibility report cannot gain financial authority")
    if report.get("new_fit_attempts") != 0:
        raise DataValidationError("feasibility cannot refit models")
    if full_verify:
        verify_entries(out, report["artifacts"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "verify"))
    args = parser.parse_args()
    report = (
        run_export()
        if args.action == "run"
        else load_feasibility_report(PROJECT_ROOT / OUTPUT_DIRECTORY, full_verify=True)
    )
    print(json.dumps({"action": args.action, "fingerprint": report["fingerprint"]}), flush=True)


if __name__ == "__main__":
    main()
