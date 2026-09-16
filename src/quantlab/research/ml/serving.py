"""Immutable model registration, explicit activation, and label-free daily inference.

This registry grants research/shadow authority only. It never places broker orders.
Actual registration/activation timestamps cannot be backdated by CLI arguments.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pandas as pd

from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import (
    complete,
    fingerprint,
    publish_ready,
    verify_completed,
    verify_publication,
    write_frame,
)
from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.data import calendar_index, validate_features
from quantlab.research.ml.io import research_output, sha256, write_json


def now():
    return datetime.now(UTC)


def predict_model(folder, kind, names, frame):
    if kind == "ridge":
        with np.load(folder / "ridge.npz", allow_pickle=False) as saved:
            selected = saved["features"].tolist()
            array = frame[selected].to_numpy(dtype="float64")
            array = np.clip(
                np.where(np.isfinite(array), array, saved["median"]), saved["low"], saved["high"]
            )
            x = np.column_stack([np.ones(len(frame)), (array - saved["mean"]) / saved["scale"]])
            return x @ saved["coefficient"]
    import lightgbm as lgb

    model = lgb.Booster(model_file=str(folder / f"{kind}.txt"))
    if model.feature_name() != names:
        raise ValueError("saved model feature schema mismatch")
    return model.predict(frame[names].to_numpy(dtype="float32"))


def register_model(run, fold, kind, registry):
    research_output(registry)
    verify_completed(run)
    intent = json.loads((run / "intent.json").read_text())
    if kind not in intent["config"]["models"] or fold not in {
        p.name for p in (run / "models").iterdir()
    }:
        raise ValueError("model/fold is not part of the completed training run")
    source = run / "models" / fold / kind
    fit = json.loads((source / "fit.json").read_text())
    identifier = fingerprint({"run": sha256(run / "completed.json"), "fold": fold, "kind": kind})[
        :24
    ]
    with exclusive_job(registry):
        target = registry / "models" / identifier
        if target.exists():
            verify_completed(target)
            return json.loads((target / "registration.json").read_text())
        work = registry / ".work" / uuid4().hex
        shutil.copytree(source, work / "model")
        registration = {
            "model_id": identifier,
            "kind": kind,
            "fold": fold,
            "fit": fit,
            "config": intent["config"],
            "registered_at": now().isoformat(),
            "source_run_sha256": sha256(run / "completed.json"),
            "feature_names": fit["feature_names"],
            "feature_contract_sha256": intent["inputs"]["files"].get("feature_contract.json"),
            "execution_authority": False,
        }
        write_json(work / "registration.json", registration)
        complete(work)
        target.parent.mkdir(parents=True, exist_ok=True)
        work.rename(target)
        return registration


def _model_folder(registry, identifier):
    if len(identifier) != 24 or set(identifier) - set("0123456789abcdef"):
        raise ValueError("invalid model id")
    folder = registry / "models" / identifier
    verify_completed(folder)
    return folder


def activate_model(registry, identifier, effective_from):
    research_output(registry)
    with exclusive_job(registry):
        folder = _model_folder(registry, identifier)
        registration = json.loads((folder / "registration.json").read_text())
        clock = pd.Timestamp(now())
        if effective_from < clock.tz_convert("Asia/Shanghai").date():
            raise ValueError("activation cannot be backdated")
        if effective_from <= pd.Timestamp(registration["fit"]["fit_asof"]).date():
            raise ValueError("model cannot activate before its training cutoff")
        event = {
            "model_id": identifier,
            "effective_from": str(effective_from),
            "recorded_at": clock.isoformat(),
            "execution_authority": False,
        }
        write_json(registry / "activations" / f"{uuid4().hex}.json", event)
        return event


def selected_model(registry, asof):
    cutoff = pd.Timestamp(asof).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
    events = [json.loads(p.read_text()) for p in sorted((registry / "activations").glob("*.json"))]
    events = [
        e
        for e in events
        if pd.Timestamp(e["effective_from"]).date() <= asof
        and pd.Timestamp(e["recorded_at"]) <= cutoff
    ]
    if not events:
        raise ValueError("no model activated and known by the decision cutoff")
    event = max(events, key=lambda e: (e["effective_from"], e["recorded_at"]))
    folder = _model_folder(registry, event["model_id"])
    registration = json.loads((folder / "registration.json").read_text())
    if pd.Timestamp(registration["registered_at"]) > cutoff:
        raise ValueError("model artifact was unavailable at decision time")
    return folder, registration


def predict_day(registry, features_path, calendar_path, asof, output, *, code):
    research_output(output)
    folder, registration = selected_model(registry, asof)
    payload = dict(registration["config"])
    payload["models"] = tuple(payload["models"])
    config = MLConfig(**payload)
    sessions = calendar_index(json.loads(calendar_path.read_text()))
    source_hash, calendar_hash = sha256(features_path), sha256(calendar_path)
    import pyarrow.parquet as pq

    from quantlab.research.ml.panel import read_range

    raw = read_range(features_path, asof, asof, max_bytes=config.max_matrix_bytes)
    if pq.ParquetFile(features_path).metadata.num_rows != len(raw):
        raise ValueError("daily inference input must contain exactly one session")
    if not pd.to_datetime(raw.trade_date).eq(pd.Timestamp(asof)).all() or raw.empty:
        raise ValueError("daily inference requires a nonempty single-session feature snapshot")
    names = registration["feature_names"]
    frame = validate_features(raw, names, sessions, config)
    timestamp = pd.Timestamp(now())
    if pd.to_datetime(frame.feature_available_at, utc=True).gt(timestamp).any():
        raise ValueError("feature availability is later than actual prediction time")
    valid = frame.loc[frame.feature_ok & frame.eligible.fillna(False)].copy()
    if len(valid) < config.min_cross_section:
        raise ValueError("insufficient daily eligible feature coverage")
    scores = valid[["trade_date", "instrument_id"]].copy()
    scores["score"] = predict_model(folder / "model", registration["kind"], names, valid)
    if not np.isfinite(scores.score).all():
        raise ValueError("nonfinite daily predictions")
    scores["fit_asof"] = pd.Timestamp(registration["fit"]["fit_asof"])
    scores["model_id"] = registration["model_id"]
    scores["model"] = registration["kind"]
    cutoff = pd.Timestamp(asof).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
    end_clock = pd.Timestamp(now())
    forward = end_clock <= cutoff and end_clock.tz_convert("Asia/Shanghai").date() == asof
    reference = registration["fit"].get("feature_reference", {})
    drift = {}
    for name in names:
        current = pd.to_numeric(frame[name], errors="coerce")
        base = reference.get(name, {})
        scale = base.get("std")
        shift = (
            (float(current.mean()) - base["mean"]) / scale
            if scale and pd.notna(current.mean())
            else None
        )
        drift[name] = {
            "missing_fraction": float(current.isna().mean()),
            "mean_shift_training_std": shift,
            "alert": bool(shift is not None and abs(shift) > 3),
        }
    manifest = {
        "asof": str(asof),
        "model_id": registration["model_id"],
        "model_completion_sha256": sha256(folder / "completed.json"),
        "features_sha256": source_hash,
        "calendar_sha256": calendar_hash,
        "code": code,
        "generated_at": end_clock.isoformat(),
        "forward_eligible": bool(forward),
        "mode": "forward_shadow" if forward else "late_recomputation_not_forward",
        "input_rows": len(frame),
        "prediction_rows": len(scores),
        "eligible_fraction": len(valid) / len(frame),
        "drift": drift,
        "execution_authority": False,
    }
    if sha256(features_path) != source_hash or sha256(calendar_path) != calendar_hash:
        raise ValueError("daily inputs changed during inference")
    verify_completed(folder)
    with exclusive_job(output.parent):
        if output.exists():
            raise FileExistsError("daily signal snapshot already exists; never overwrite it")
        work = output.parent / ".prediction-work" / uuid4().hex
        work.mkdir(parents=True)
        write_frame(work / "scores.parquet", scores)
        write_json(work / "prediction.json", manifest)
        publication = publish_ready(work, output, asof, now)
    return {
        **manifest,
        **publication,
        "mode": "forward_shadow"
        if publication["forward_eligible"]
        else "late_recomputation_not_forward",
    }


def archived_signals(root, decision_sessions):
    frames, bindings = [], []
    for day in decision_sessions:
        folder = root / str(day)
        verify_publication(folder, day)
        meta = json.loads((folder / "prediction.json").read_text())
        cutoff = pd.Timestamp(day).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
        if (
            meta["asof"] != str(day)
            or meta.get("forward_eligible") is not True
            or pd.Timestamp(meta["generated_at"]) > cutoff
        ):
            raise ValueError("shadow requires genuinely archived forward signals for every session")
        scores = pd.read_parquet(folder / "scores.parquet")
        if (
            not pd.to_datetime(scores.trade_date).eq(pd.Timestamp(day)).all()
            or not scores.model_id.eq(meta["model_id"]).all()
        ):
            raise ValueError("archived signal identity mismatch")
        scores["model"] = "shadow"
        frames.append(scores)
        bindings.append(
            {"session": str(day), "completion_sha256": sha256(folder / "completed.json")}
        )
    return pd.concat(frames, ignore_index=True), bindings
