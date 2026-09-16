"""Walk-forward baselines with mature labels and validation-only early stopping."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.data import date_weights, monthly_folds, transform_labels


def _training_rows(frame, mask, config):
    rows = frame.loc[mask].copy().sort_values(["trade_date", "instrument_id"])
    counts = rows.groupby("trade_date").instrument_id.transform("size")
    rows = rows.loc[counts.ge(config.min_cross_section)]
    if rows.empty:
        raise ValueError("empty train/validation after maturity and cross-section checks")
    return rows


def _ridge(train, valid, test, names, config, folder):
    # All fitted statistics come exclusively from purged training observations.
    raw = train[names].to_numpy(dtype="float64")
    usable = np.isfinite(raw).any(axis=0)
    if not usable.any():
        raise ValueError("all training features are missing")
    selected = np.array(names)[usable].tolist()
    raw = raw[:, usable]
    low, high = np.nanquantile(raw, [0.005, 0.995], axis=0)
    med = np.nanmedian(raw, axis=0)
    filled = np.clip(np.where(np.isfinite(raw), raw, med), low, high)
    mean, scale = filled.mean(axis=0), filled.std(axis=0)
    scale[scale == 0] = 1

    def transform(frame):
        array = frame[selected].to_numpy(dtype="float64")
        array = np.clip(np.where(np.isfinite(array), array, med), low, high)
        return np.column_stack([np.ones(len(frame)), (array - mean) / scale])

    x = transform(train)
    y = transform_labels(train, config).to_numpy()
    weights = date_weights(train)
    penalty = np.eye(x.shape[1]) * config.ridge_alpha
    penalty[0, 0] = 0
    coefficient = np.linalg.solve(x.T @ (weights[:, None] * x) + penalty, x.T @ (weights * y))
    prediction = transform(test) @ coefficient
    if folder is not None:
        np.savez(
            folder / "ridge.npz",
            features=np.array(selected),
            low=low,
            high=high,
            median=med,
            mean=mean,
            scale=scale,
            coefficient=coefficient,
        )
    return prediction, {"best_iteration": None, "fitted_features": len(selected)}


def _lightgbm(train, valid, test, names, config, kind, folder):
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise RuntimeError("install research runtime: uv sync --extra research") from exc

    params = {
        "objective": {"lightgbm": "regression", "binary": "binary", "lambdarank": "lambdarank"}[
            kind
        ],
        "metric": {"lightgbm": "l2", "binary": "binary_logloss", "lambdarank": "ndcg"}[kind],
        "learning_rate": config.learning_rate,
        "num_leaves": config.num_leaves,
        "max_depth": config.max_depth,
        "min_data_in_leaf": config.min_data_in_leaf,
        "feature_fraction": config.feature_fraction,
        "bagging_fraction": config.bagging_fraction,
        "bagging_freq": 1,
        "lambda_l2": config.lambda_l2,
        "seed": config.seed,
        "num_threads": config.num_threads,
        "deterministic": True,
        "force_col_wise": True,
        "verbosity": -1,
    }
    if kind == "lambdarank":
        params.update(label_gain=list(range(10)), eval_at=[20, 50])

    def dataset(frame, reference=None):
        return lgb.Dataset(
            frame[names].to_numpy(dtype="float32"),
            label=transform_labels(frame, config, kind).to_numpy(),
            weight=date_weights(frame),
            feature_name=names,
            reference=reference,
            group=(
                frame.groupby("trade_date", sort=False).size().to_numpy()
                if kind == "lambdarank"
                else None
            ),
        )

    train_set = dataset(train)
    model = lgb.train(
        params,
        train_set,
        num_boost_round=config.max_rounds,
        valid_sets=[dataset(valid, train_set)],
        valid_names=["validation"],
        callbacks=[
            lgb.early_stopping(config.early_stopping_rounds, first_metric_only=True, verbose=False)
        ],
    )
    prediction = model.predict(
        test[names].to_numpy(dtype="float32"), num_iteration=model.best_iteration
    )
    if folder is not None:
        model.save_model(str(folder / f"{kind}.txt"), num_iteration=model.best_iteration)
        restored = lgb.Booster(model_file=str(folder / f"{kind}.txt"))
        reproduced = restored.predict(test[names].to_numpy(dtype="float32"))
        if not np.allclose(prediction, reproduced, rtol=1e-12, atol=1e-12):
            raise ValueError("saved LightGBM model did not reproduce predictions")
    return prediction, {"best_iteration": model.best_iteration, "parameters": params}


def walk_forward(frame, names, sessions, start, end, config: MLConfig, output: Path | None = None):
    """Fit once per model/month. Never select parameters using prediction months."""
    scores, fits = [], []
    for fold in monthly_folds(sessions, start, end, config):
        masks = fold.masks(frame)
        train, valid = (_training_rows(frame, m, config) for m in masks[:2])
        test = frame.loc[masks[2]].copy()
        # Bound peak dense work, including float64 Ridge copies/linear algebra.
        estimate = (len(train) + len(valid) + len(test)) * (len(names) + 1) * 8 * 6
        if estimate > config.max_matrix_bytes:
            raise MemoryError(f"estimated dense training workspace {estimate} exceeds budget")
        if test.empty:
            raise ValueError(f"{fold.name}: no prediction rows; investigate coverage")
        for kind in config.models:
            folder = output / fold.name / kind if output is not None else None
            if folder is not None:
                folder.mkdir(parents=True, exist_ok=False)
            if kind == "ridge":
                prediction, details = _ridge(train, valid, test, names, config, folder)
            else:
                prediction, details = _lightgbm(train, valid, test, names, config, kind, folder)
            if not np.isfinite(prediction).all():
                raise ValueError("model generated nonfinite prediction")
            part = test[["trade_date", "instrument_id", "raw_label", "label_end"]].copy()
            part["score"], part["model"], part["fold"] = prediction, kind, fold.name
            part["fit_asof"] = fold.fit_asof
            scores.append(part)
            record = {
                "feature_names": names,
                "fold": fold.name,
                "model": kind,
                "train_start": str(fold.train_start.date()),
                "validation_start": str(fold.validation_start.date()),
                "fit_asof": str(fold.fit_asof.date()),
                "test_start": str(fold.test_start.date()),
                "test_end": str(fold.test_end.date()),
                "train_rows": len(train),
                "validation_rows": len(valid),
                "prediction_rows": len(test),
                "train_label_max": str(train.label_end.max().date()),
                "validation_label_max": str(valid.label_end.max().date()),
                "date_weighting": "equal_session_total",
                **details,
            }
            fits.append(record)
            if folder is not None:
                (folder / "fit.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return pd.concat(scores, ignore_index=True), fits
