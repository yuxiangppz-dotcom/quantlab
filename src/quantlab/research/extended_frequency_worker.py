"""Bounded new-model fitting or immutable old-model prediction reuse, never promotion."""

from __future__ import annotations

import gc
import hashlib
import json
import pickle
import time
from datetime import datetime
from functools import partial

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import init_qlib
from quantlab.research.alpha158_rolling_data import batches, fit_scaler_inplace, transform
from quantlab.research.alpha158_rolling_protocol import runtime_manifest
from quantlab.research.alpha158_store import Budget, atomic_seal, peak_rss_bytes
from quantlab.research.extended_frequency_protocol import OLD, OUTPUT, folder_for, now, verify_locks
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.weekly_pilot_data import matrix_for_week, prediction_mask, update_membership
from quantlab.research.weekly_pilot_protocol import invoke_once

META_COLUMNS = [
    "instrument_id",
    "trade_date",
    "future_return_5d",
    "label_end_date",
    "complete_features",
    "label_reason",
]


def model_path(root, spec, folder):
    return (
        root / OLD / "fits" / spec["reuse_slot"] / "model.pkl"
        if spec["reuse_slot"] is not None
        else folder / "model.pkl"
    )


def predict_union(root, config, spec, folder, model, scaler, metadata, *, batch_observer=None):
    from quantlab.research.alpha158_rolling_models import ArrayDataset

    path = model_path(root, spec, folder)
    sha = _sha(path)
    if spec["reuse_slot"] is not None and sha != spec["model_sha256"]:
        raise DataValidationError("original model bytes changed")
    # New pickles come from this locked worker; reuse pickles are bound to the verified old source.
    with path.open("rb") as stream:
        restored = pickle.load(stream)
    if _sha(path) != sha:
        raise DataValidationError("model changed during prediction reload")
    old = None
    if spec["reuse_slot"] is not None:
        source = root / OLD / "fits" / spec["reuse_slot"] / "predictions.parquet"
        if _sha(source) != spec["original_summary"]["prediction_file_sha256"]:
            raise DataValidationError("original predictions changed")
        old = pd.read_parquet(source, use_threads=False).set_index(["instrument_id", "trade_date"])
        if not old.index.is_unique:
            raise DataValidationError("original prediction identities duplicated")
    target = folder / "predictions.parquet"
    if target.exists():
        raise DataValidationError("extended predictions cannot be overwritten")
    writer, total, reused, computed, digest = None, 0, 0, 0, hashlib.sha256()
    try:
        for ordinal, (meta, values) in enumerate(
            batches(
                root / config["history_root"],
                root / config["metadata_root"],
                metadata,
                config["features"],
            )
        ):
            selected = prediction_mask(meta, spec["prediction_sessions"]).to_numpy()
            if not selected.any():
                continue
            part = meta.loc[selected, META_COLUMNS].copy()
            update_membership(digest, part)
            keys = pd.MultiIndex.from_frame(part[["instrument_id", "trade_date"]])
            index = pd.MultiIndex.from_frame(part[["trade_date", "instrument_id"]]).set_names(
                ["datetime", "instrument"]
            )
            x = transform(values[selected], scaler)
            existing = np.zeros(len(part), dtype=bool) if old is None else keys.isin(old.index)
            score = np.empty(len(part), dtype="float64")
            if existing.any():
                prior = old.loc[keys[existing]].reset_index()
                if (
                    not prior[META_COLUMNS]
                    .reset_index(drop=True)
                    .equals(part.loc[existing, META_COLUMNS].reset_index(drop=True))
                ):
                    raise DataValidationError(
                        "reused prediction members/labels differ from current frozen metadata"
                    )
                score[existing] = prior.score.to_numpy()
                reused += int(existing.sum())
            missing = ~existing
            if missing.any():
                score[missing] = model.predict(
                    ArrayDataset(x[missing], index=index[missing])
                ).to_numpy()
                computed += int(missing.sum())
            # Float32 matrix products may depend on batch geometry. Reproduce the
            # original saved-row batch and the new-row batch separately, retaining
            # exact comparison for every row instead of relaxing the tolerance.
            replay = np.empty(len(part), dtype="float64")
            for group in (existing, missing):
                if group.any():
                    replay[group] = restored.predict(
                        ArrayDataset(x[group], index=index[group])
                    ).to_numpy()
            if not np.isfinite(score).all() or not np.array_equal(score, replay):
                raise DataValidationError("extended saved model fails exact union replay")
            if batch_observer is not None:
                batch_observer(ordinal, part, existing, x)
            part["score"] = score
            table = pa.Table.from_pandas(part, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(target, table.schema)
            writer.write_table(table)
            total += len(part)
    finally:
        if writer is not None:
            writer.close()
    if (
        total != spec["prediction_rows"]
        or digest.hexdigest() != spec["prediction_sha256"]
        or reused + computed != total
    ):
        raise DataValidationError("extended prediction union identities/count changed")
    return {
        "prediction_rows": total,
        "prediction_sha256": digest.hexdigest(),
        "saved_model_sha256": sha,
        "prediction_file_sha256": _sha(target),
        "saved_model_max_prediction_difference": 0.0,
        "reused_prediction_rows": reused,
        "newly_scored_rows": computed,
        "replay_verification_rows": total,
    }


def fit_new(root, plan, spec, folder, metadata):
    from qlib.workflow import R

    from quantlab.research.alpha158_rolling_models import ArrayDataset, make_model

    config = plan["config"]
    x, y = matrix_for_week(
        root / config["history_root"],
        root / config["metadata_root"],
        metadata,
        config["features"],
        spec,
        spec["train_sha256"],
    )
    scaler = fit_scaler_inplace(x) if spec["model"] == "ridge" else None
    atomic_seal(
        folder / "preprocessing.json",
        {
            "identity": plan["fingerprint"],
            "slot": spec["slot"],
            "train_start": spec["train_start"],
            "train_end": spec["train_end"],
            "train_rows": len(y),
            "train_membership_sha256": spec["train_sha256"],
            "scaler": scaler,
            "features": config["features"],
            "matrix_dtype": "float32",
            "label_dtype": "float32",
            "label_transform": "none",
            "fitted_on": "mature_training_only",
        },
    )
    model = make_model(spec["model"], config["models"])
    with R.start(
        experiment_name="alpha158_extended_frequency", recorder_name=spec["slot"]
    ) as recorder:
        R.log_params(
            slot=spec["slot"],
            plan_fingerprint=plan["fingerprint"],
            source_head=plan["code_head"],
            train_rows=len(y),
            training_cutoff=spec["train_end"],
            fixed_model=json.dumps(config["models"][spec["model"]], sort_keys=True),
        )
        before = time.monotonic()
        invoke_once(
            folder,
            plan["fingerprint"],
            spec["slot"],
            partial(
                model.fit,
                ArrayDataset(x, y),
                **({"verbose_eval": 0} if spec["model"] == "lightgbm" else {}),
            ),
        )
        seconds, trained_at = time.monotonic() - before, now()
        with (folder / "model.pkl").open("xb") as stream:
            pickle.dump(model, stream, protocol=5)
        R.save_objects(local_path=str(folder / "model.pkl"))
        recorder_id = recorder.id
    del x, y
    gc.collect()
    return (
        model,
        scaler,
        {
            "fit_seconds": seconds,
            "trained_at_utc": trained_at,
            "qlib_recorder_id": recorder_id,
            "new_fit_invocations": 1,
            "original_model_fingerprint": None,
        },
    )


def load_reuse(root, plan, spec, folder):
    original = root / OLD / "fits" / spec["reuse_slot"]
    prep = sealed_read(original / "preprocessing.json")
    if (
        prep["fingerprint"] != spec["original_preprocessing_fingerprint"]
        or _sha(original / "model.pkl") != spec["model_sha256"]
    ):
        raise DataValidationError("extended original preprocessing/model source changed")
    atomic_seal(
        folder / "preprocessing.json",
        {
            **{k: v for k, v in prep.items() if k not in ("identity", "slot", "fingerprint")},
            "identity": plan["fingerprint"],
            "slot": spec["slot"],
            "original_preprocessing_fingerprint": prep["fingerprint"],
        },
    )
    atomic_seal(
        folder / "model_reference.json",
        {
            "identity": plan["fingerprint"],
            "slot": spec["slot"],
            "source_head": spec["source_head"],
            "reuse_slot": spec["reuse_slot"],
            "sha256": spec["model_sha256"],
        },
    )
    with (original / "model.pkl").open("rb") as stream:
        model = pickle.load(stream)
    return (
        model,
        prep["scaler"],
        {
            "fit_seconds": 0.0,
            "trained_at_utc": spec["original_summary"]["trained_at_utc"],
            "qlib_recorder_id": spec["original_summary"]["qlib_recorder_id"],
            "new_fit_invocations": 0,
            "original_model_fingerprint": spec["original_summary"]["fingerprint"],
        },
    )


def worker(root, slot, descriptors):
    from threadpoolctl import threadpool_limits

    verify_locks(root, descriptors)
    plan = sealed_read(root / OUTPUT / "plan.json")
    config = plan["config"]
    folder = folder_for(root, config, slot)
    spec = config["model_specs"][slot]
    start = sealed_read(folder / "started.json")
    expected_mode = "reuse" if spec["reuse_slot"] is not None else "fits"
    if (
        start["identity"] != plan["fingerprint"]
        or start["slot"] != slot
        or start["mode"] != expected_mode
    ):
        raise DataValidationError("extended worker differs from reserved slot")
    if any(
        (folder / name).exists()
        for name in (
            "model.pkl",
            "model_reference.json",
            "invoked.json",
            "preprocessing.json",
            "worker_result.json",
            "result.json",
            "predictions.parquet",
        )
    ):
        raise DataValidationError("consumed extended worker cannot run again")
    verify_entries(root, plan["code_files"])
    verify_entries(root, plan["sources"]["inputs"])
    if runtime_manifest() != plan["sources"]["runtime"]:
        raise DataValidationError("extended worker runtime changed")
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    metadata = sealed_read(root / config["metadata_root"] / "metadata.json")
    budget = Budget(root / OUTPUT, config)
    init_qlib(folder / "qlib", experiment_name="alpha158_extended_frequency")
    with budget.watchdog(), threadpool_limits(limits=2):
        model, scaler, fitted = (
            load_reuse(root, plan, spec, folder)
            if expected_mode == "reuse"
            else fit_new(root, plan, spec, folder, metadata)
        )
        prediction = predict_union(root, config, spec, folder, model, scaler, metadata)
        verify_entries(root, plan["code_files"])
        verify_entries(root, plan["sources"]["inputs"])
        atomic_seal(
            folder / "worker_result.json",
            {
                "identity": plan["fingerprint"],
                "slot": slot,
                "mode": expected_mode,
                "kind": spec["model"],
                "train_start": spec["train_start"],
                "train_end": spec["train_end"],
                "train_rows": spec["train_rows"],
                "train_sha256": spec["train_sha256"],
                "replay_prediction_start": spec["replay_prediction_start"],
                "derived_at_utc": now(),
                "peak_rss_bytes": peak_rss_bytes(),
                "ridge_n_iter": getattr(model, "n_iter_", None),
                "boosted_rounds": model.model.current_iteration()
                if spec["model"] == "lightgbm"
                else None,
                "history_already_observed": True,
                "performance_evidence": False,
                "execution_authority": False,
                **fitted,
                **prediction,
            },
        )


def utc(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise DataValidationError("extended worker requires actual UTC timestamps")
    return stamp


def validated_worker(root, plan, slot):
    return validate_saved_result(root, plan, slot, folder_for(root, plan["config"], slot))


def validate_saved_result(root, plan, slot, folder):
    """Validate an explicitly located artifact without redirecting the original worker."""
    config = plan["config"]
    spec = config["model_specs"][slot]
    summary, start, prep = (
        sealed_read(folder / name)
        for name in ("worker_result.json", "started.json", "preprocessing.json")
    )
    mode = "reuse" if spec["reuse_slot"] is not None else "fits"
    if any(
        v.get("identity") != plan["fingerprint"] or v.get("slot") != slot
        for v in (summary, start, prep)
    ):
        raise DataValidationError("extended worker identity changed")
    if (
        summary.get("mode") != mode
        or start.get("mode") != mode
        or summary.get("kind") != spec["model"]
    ):
        raise DataValidationError("extended worker mode changed")
    for key in (
        "train_start",
        "train_end",
        "train_rows",
        "train_sha256",
        "prediction_rows",
        "prediction_sha256",
        "replay_prediction_start",
    ):
        if summary.get(key) != spec[key]:
            raise DataValidationError("extended worker training/prediction contract changed")
    expected_prep = {
        "train_start": spec["train_start"],
        "train_end": spec["train_end"],
        "train_rows": spec["train_rows"],
        "train_membership_sha256": spec["train_sha256"],
        "features": config["features"],
        "matrix_dtype": "float32",
        "label_dtype": "float32",
        "label_transform": "none",
        "fitted_on": "mature_training_only",
    }
    if any(prep.get(key) != value for key, value in expected_prep.items()):
        raise DataValidationError("extended preprocessing membership/semantics changed")
    scaler = prep["scaler"]
    if spec["model"] == "ridge":
        if (
            scaler is None
            or scaler["rows"] != spec["train_rows"]
            or scaler["ddof"] != 0
            or any(
                len(scaler[key]) != len(config["features"]) or not np.isfinite(scaler[key]).all()
                for key in ("mean", "scale")
            )
            or not (np.array(scaler["scale"]) > 0).all()
        ):
            raise DataValidationError("extended Ridge scaler contract changed")
        iterations = summary["ridge_n_iter"]
        if (
            len(iterations) != 1
            or type(iterations[0]) is not int
            or not 0 < iterations[0] <= config["models"]["ridge"]["max_iter"]
        ):
            raise DataValidationError("extended Ridge iteration contract changed")
    elif (
        scaler is not None
        or summary["boosted_rounds"] != config["models"]["lightgbm"]["num_boost_round"]
    ):
        raise DataValidationError("extended LightGBM preprocessing/rounds changed")
    started, trained, derived = (
        utc(start["started_at"]),
        utc(summary["trained_at_utc"]),
        utc(summary["derived_at_utc"]),
    )
    if mode == "fits":
        invoked = sealed_read(folder / "invoked.json")
        if (
            invoked["identity"] != plan["fingerprint"]
            or invoked["slot"] != slot
            or not started <= utc(invoked["at"]) <= trained <= derived
            or summary["new_fit_invocations"] != 1
            or summary["original_model_fingerprint"] is not None
        ):
            raise DataValidationError("extended fit invocation/chronology changed")
    else:
        original_prep = sealed_read(root / OLD / "fits" / spec["reuse_slot"] / "preprocessing.json")
        reference = sealed_read(folder / "model_reference.json")
        expected_reference = {
            "identity": plan["fingerprint"],
            "slot": slot,
            "source_head": spec["source_head"],
            "reuse_slot": spec["reuse_slot"],
            "sha256": spec["model_sha256"],
        }
        if (
            (folder / "invoked.json").exists()
            or (folder / "model.pkl").exists()
            or summary["new_fit_invocations"] != 0
            or summary["fit_seconds"] != 0.0
            or not trained <= started <= derived
            or summary["trained_at_utc"] != spec["original_summary"]["trained_at_utc"]
            or summary["original_model_fingerprint"] != spec["original_summary"]["fingerprint"]
            or original_prep["fingerprint"] != spec["original_preprocessing_fingerprint"]
            or prep["scaler"] != original_prep["scaler"]
            or prep.get("original_preprocessing_fingerprint") != original_prep["fingerprint"]
            or any(reference.get(key) != value for key, value in expected_reference.items())
            or summary["saved_model_sha256"] != spec["model_sha256"]
            or summary["qlib_recorder_id"] != spec["original_summary"]["qlib_recorder_id"]
        ):
            raise DataValidationError(
                "extended reuse gained fit authority or changed original source"
            )
    for key in (
        "new_fit_invocations",
        "prediction_rows",
        "reused_prediction_rows",
        "newly_scored_rows",
        "replay_verification_rows",
    ):
        if type(summary[key]) is not int or summary[key] < 0:
            raise DataValidationError("extended prediction/action counter type changed")
    if (
        summary["reused_prediction_rows"] + summary["newly_scored_rows"] != spec["prediction_rows"]
        or summary["replay_verification_rows"] != spec["prediction_rows"]
        or summary["saved_model_max_prediction_difference"] != 0.0
        or (mode == "fits" and summary["reused_prediction_rows"] != 0)
    ):
        raise DataValidationError("extended saved prediction coverage/replay changed")
    if summary.get("history_already_observed") is not True or any(
        summary.get(k) is not False for k in ("performance_evidence", "execution_authority")
    ):
        raise DataValidationError("extended worker gained financial authority")
    if (
        _sha(model_path(root, spec, folder)) != summary["saved_model_sha256"]
        or _sha(folder / "predictions.parquet") != summary["prediction_file_sha256"]
    ):
        raise DataValidationError("extended model/prediction bytes changed")
    file = pq.ParquetFile(folder / "predictions.parquet")
    if file.metadata.num_rows != spec["prediction_rows"] or set(file.schema_arrow.names) != {
        *META_COLUMNS,
        "score",
    }:
        raise DataValidationError("extended prediction file population changed")
    return summary
