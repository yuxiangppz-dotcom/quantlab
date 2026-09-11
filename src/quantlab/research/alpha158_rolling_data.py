"""Labels and bounded matrices from sealed Alpha158 partitions, never live metadata."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_native import KEYS
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.input_audit import _sha
from quantlab.research.returns import calculate_forward_returns
from quantlab.research.round2_dataset import sealed_read, verify_entries


def labels_from_raw(raw, grid, inventory):
    """Exact five-market-session endpoints, same-day adjustment, inclusive lifecycle."""
    raw = raw.copy()
    raw["trade_date"] = pd.to_datetime(raw.trade_date)
    if raw.duplicated(KEYS).any() or grid.duplicated(KEYS).any():
        raise DataValidationError("duplicate sealed label identity")
    listed = pd.to_datetime(raw.instrument_id.map(inventory["list_dates"]))
    delisted = pd.to_datetime(raw.instrument_id.map(inventory["delist_dates"]))
    active = listed.notna() & raw.trade_date.ge(listed)
    active &= delisted.isna() | raw.trade_date.le(delisted)
    price = raw.close * raw.adj_factor
    valid = active & raw.close.gt(0) & raw.adj_factor.gt(0) & np.isfinite(price)
    frame = raw[KEYS].assign(adj_close=price.where(valid))
    sessions = [date.fromisoformat(d) for d in inventory["sessions"]]
    result = calculate_forward_returns(frame, sessions, (5,))
    result["trade_date"] = pd.to_datetime(result.trade_date)
    result = grid[KEYS].merge(
        result[[*KEYS, "future_return_5d"]], on=KEYS, how="left", validate="one_to_one"
    )
    endpoints = dict(zip(pd.to_datetime(sessions[:-5]), pd.to_datetime(sessions[5:]), strict=True))
    result["label_end_date"] = result.trade_date.map(endpoints)
    result["label_reason"] = np.select(
        [result.label_end_date.isna(), ~np.isfinite(result.future_return_5d)],
        ["unknown_end_session", "missing_or_invalid_endpoint"],
        default="available",
    )
    return result


def contained_mask(meta, bounds):
    return (
        meta.trade_date.between(*bounds)
        & meta.complete_features
        & meta.label_end_date.le(bounds[1])
        & np.isfinite(meta.future_return_5d)
    )


def prediction_mask(meta, bounds):
    # Deliberately never reads label columns.
    return meta.trade_date.between(*bounds) & meta.complete_features


def receipt_file(history, receipt, relative):
    if relative not in receipt["artifacts"]:
        raise DataValidationError("unbound historical partition")
    verify_entries(history, {relative: receipt["artifacts"][relative]})
    return history / relative


def feature_path(history, number):
    receipt = sealed_read(history / "receipts" / f"features-{number:04d}.json")
    matches = [p for p in receipt["artifacts"] if p.endswith("/usable_features.parquet")]
    if len(matches) != 1:
        raise DataValidationError("ambiguous usable feature partition")
    return receipt_file(history, receipt, matches[0])


def stage_metadata(history, out, plan, names, progress=print):
    path = out / "metadata.json"
    if path.exists():
        result = sealed_read(path)
        if result["identity"] != plan["fingerprint"]:
            raise DataValidationError("metadata identity changed")
        verify_entries(out, result["artifacts"])
        return result
    inventory = sealed_read(history / "inventory.json")
    old_plan = sealed_read(history / "plan.json")
    raws = [sealed_read(p) for p in sorted((history / "receipts").glob("raw-*.json"))]
    folder = out / "metadata"
    folder.mkdir(exist_ok=True)
    artifacts, counts = {}, []
    for number, _codes in enumerate(old_plan["batches"]):
        target = folder / f"{number:04d}.parquet"
        receipt_path = folder / f"{number:04d}.json"
        if receipt_path.exists():
            receipt = sealed_read(receipt_path)
            if receipt["identity"] != plan["fingerprint"]:
                raise DataValidationError("metadata batch identity changed")
            verify_entries(out, receipt["artifacts"])
        else:
            if target.exists():
                raise DataValidationError("interrupted metadata partition requires inspection")
            features = pd.read_parquet(feature_path(history, number), use_threads=False)
            raw_parts = []
            for r in raws:
                if str(number) not in r["result"]["batch_rows"]:
                    continue
                suffix = f"/batch-{number:04d}.parquet"
                matching = [p for p in r["artifacts"] if p.endswith(suffix)]
                if len(matching) != 1:
                    raise DataValidationError("ambiguous sealed raw partition")
                raw_parts.append(
                    pd.read_parquet(
                        receipt_file(history, r, matching[0]),
                        columns=[*KEYS, "close", "adj_factor"],
                        use_threads=False,
                    )
                )
            meta = labels_from_raw(pd.concat(raw_parts, ignore_index=True), features, inventory)
            meta["complete_features"] = np.isfinite(features[names]).all(axis=1).to_numpy()
            meta.to_parquet(target, index=False)
            count = {"batch": number, "rows": len(meta)}
            for fold in plan["config"]["folds"]:
                count[f"train_{fold['id']}"] = int(contained_mask(meta, fold["train"]).sum())
            receipt = atomic_seal(
                receipt_path,
                {
                    "identity": plan["fingerprint"],
                    "counts": count,
                    "artifacts": {
                        target.relative_to(out).as_posix(): {
                            "sha256": _sha(target),
                            "bytes": target.stat().st_size,
                        }
                    },
                },
            )
        counts.append(receipt["counts"])
        artifacts.update(receipt["artifacts"])
        if number % 20 == 0:
            progress(f"sealed label metadata {number + 1}/{len(old_plan['batches'])}", flush=True)
    return atomic_seal(
        path,
        {
            "identity": plan["fingerprint"],
            "artifacts": artifacts,
            "counts": counts,
            "label_horizon_sessions": 5,
            "new_fit_attempts": 0,
        },
    )


def batches(history, out, metadata, names):
    for entry in metadata["counts"]:
        number = entry["batch"]
        relative = f"metadata/{number:04d}.parquet"
        verify_entries(out, {relative: metadata["artifacts"][relative]})
        meta = pd.read_parquet(out / relative, use_threads=False)
        feature = pd.read_parquet(feature_path(history, number), use_threads=False)
        if not feature[KEYS].equals(meta[KEYS]):
            raise DataValidationError("feature/label positional identities differ")
        matrix = feature[names].to_numpy(dtype="float32")
        if not np.array_equal(np.isfinite(matrix).all(axis=1), meta.complete_features):
            raise DataValidationError("feature eligibility changed")
        yield meta, matrix


def training_matrix(history, out, metadata, names, fold):
    count = sum(r[f"train_{fold['id']}"] for r in metadata["counts"])
    if not count:
        raise DataValidationError("empty fixed training fold")
    # F order lets LightGBM pass a flattened view, avoiding a second dense matrix.
    x = np.empty((count, len(names)), dtype="float32", order="F")
    y = np.empty(count, dtype="float32")
    offset = 0
    for meta, values in batches(history, out, metadata, names):
        mask = contained_mask(meta, fold["train"]).to_numpy()
        size = int(mask.sum())
        x[offset : offset + size] = values[mask]
        y[offset : offset + size] = meta.loc[mask, "future_return_5d"]
        offset += size
    if offset != count or not np.isfinite(y).all():
        raise DataValidationError("training row count or labels changed")
    return x, y


def fit_scaler_inplace(x):
    """Population moments on purged training only; float64 reductions, float32 matrix."""
    means, scales = [], []
    for column in range(x.shape[1]):
        values = x[:, column]
        mean = float(np.mean(values, dtype="float64"))
        scale = float(np.std(values, dtype="float64"))
        if not np.isfinite(mean) or not np.isfinite(scale):
            raise DataValidationError("nonfinite training preprocessing")
        scale = scale if scale > 0 else 1.0
        values[:] = (values.astype("float64") - mean) / scale
        means.append(mean)
        scales.append(scale)
    return {"mean": means, "scale": scales, "ddof": 0, "rows": len(x)}


def transform(x, scaler):
    if scaler is None:
        return x
    return ((x.astype("float64") - scaler["mean"]) / scaler["scale"]).astype("float32")
