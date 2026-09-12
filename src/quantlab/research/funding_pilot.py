"""Fixed retrospective F1--F4 diagnostics; no portfolio, fitted model or PIT claim."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/funding_pilot_v1.json"
FEATURES = ("F1", "F2", "F3", "F4")
KEYS = ["instrument_id", "trade_date"]
FLOW = ["buy_lg_amount", "buy_elg_amount", "sell_lg_amount", "sell_elg_amount"]
RAW = ["open", "high", "low", "close", "volume", "amount"]


def valid_numeric(series, *, positive=False):
    values = pd.to_numeric(series, errors="coerce")
    genuine = series.map(lambda x: isinstance(x, (int, float)) and not isinstance(x, bool))
    valid = genuine & np.isfinite(values) & (values.gt(0) if positive else values.ge(0))
    return values.where(valid)


def prepare_grid(frame, sessions, codes):
    """Keep missing code/session rows explicit; never fill across a trading gap."""
    required = {*KEYS, *RAW, *FLOW, "adj_factor", "circ_mv", "turnover_rate"}
    if not required.issubset(frame.columns):
        raise DataValidationError("missing funding pilot columns")
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame.trade_date, errors="raise")
    days = pd.DatetimeIndex(sessions)
    if (
        days.has_duplicates
        or not days.is_monotonic_increasing
        or list(codes) != sorted(set(codes))
        or not len(days)
        or not codes
        or days.tz is not None
        or not days.normalize().equals(days)
        or frame[KEYS].isna().any().any()
        or frame.duplicated(KEYS).any()
        or not frame.instrument_id.isin(codes).all()
        or not frame.trade_date.isin(days).all()
    ):
        raise DataValidationError("invalid calendar, identity or duplicate pilot keys")
    index = pd.MultiIndex.from_product([codes, days], names=KEYS)
    grid = frame.set_index(KEYS).reindex(index).reset_index()
    for name in [*RAW, *FLOW, "adj_factor", "circ_mv", "turnover_rate"]:
        grid[name] = valid_numeric(grid[name], positive=name not in FLOW + ["turnover_rate"])
    bar_valid = grid[RAW].notna().all(axis=1)
    bar_valid &= grid.low.le(grid.high) & grid.open.between(grid.low, grid.high)
    bar_valid &= grid.close.between(grid.low, grid.high)
    grid["valid_bar"] = bar_valid
    grid["valid_flow"] = grid[FLOW].notna().all(axis=1)
    adjusted = grid.close * grid.adj_factor
    grid["adjusted_close"] = adjusted.where(bar_valid & np.isfinite(adjusted) & adjusted.gt(0))
    net = (
        grid.buy_lg_amount + grid.buy_elg_amount - grid.sell_lg_amount - grid.sell_elg_amount
    ) * 10000.0
    grid["net_large_cny"] = net.where(grid.valid_flow & bar_valid & np.isfinite(net))
    return grid


def compute_features(frame, sessions, codes):
    """t-only features and a separate, exact-session, year-contained t+1/t+6 label."""
    grid = prepare_grid(frame, sessions, codes)
    parts = []
    for _, g in grid.groupby("instrument_id", sort=False):
        g = g.copy()
        net = g.net_large_cny
        amount = g.amount.where(net.notna())
        g["F1"] = net.rolling(5, min_periods=5).sum() / amount.rolling(5, min_periods=5).sum()
        g["F2"] = net.rolling(20, min_periods=20).sum() / amount.rolling(20, min_periods=20).sum()
        g["F3"] = net.gt(0).astype(float).where(net.notna()).rolling(20, min_periods=20).mean()
        price = g.adjusted_close
        g["momentum20"] = (price / price.shift(20) - 1).where(
            price.rolling(21, min_periods=21).count().eq(21)
        )
        g["label_entry_date"] = g.trade_date.shift(-1)
        g["label_exit_date"] = g.trade_date.shift(-6)
        g["label_5"] = (price.shift(-6) / price.shift(-1) - 1).where(
            price.notna().astype(int).rolling(6, min_periods=6).sum().shift(-6).eq(6)
            & g.label_entry_date.dt.year.eq(g.trade_date.dt.year)
            & g.label_exit_date.dt.year.eq(g.trade_date.dt.year)
        )
        parts.append(g)
    result = pd.concat(parts, ignore_index=True)
    # Rank on the same pairwise complete F2/momentum cohort; ties use average ranks.
    paired = result.F2.notna() & result.momentum20.notna()
    ranks = (
        result[["F2", "momentum20"]]
        .where(paired, axis=0)
        .groupby(result.trade_date)
        .rank(method="average", pct=True)
    )
    result["F4"] = ranks.F2 - ranks.momentum20
    return result.sort_values(KEYS).reset_index(drop=True)


def rank_correlation(x, y, minimum=30):
    paired = pd.DataFrame({"x": x, "y": y}).replace([np.inf, -np.inf], np.nan).dropna()
    n = len(paired)
    if n < minimum or paired.x.nunique() < 2 or paired.y.nunique() < 2:
        return None, n
    ranks = paired.rank(method="average")
    return float(ranks.x.corr(ranks.y)), n


def daily_metrics(features, minimum=30):
    records = []
    for day, g in features.groupby("trade_date", sort=True):
        for feature in FEATURES:
            value, count = rank_correlation(g[feature], g.label_5, minimum)
            size, _ = rank_correlation(g[feature], np.log(g.circ_mv), minimum)
            turnover, _ = rank_correlation(g[feature], g.turnover_rate, minimum)
            records.append(
                {
                    "trade_date": day,
                    "feature": feature,
                    "rank_ic": value,
                    "pairs": count,
                    "size_rank_correlation": size,
                    "turnover_rank_correlation": turnover,
                }
            )
    return pd.DataFrame(records)


def block_interval(values, block=20, samples=1000, seed=20260912):
    """Moving calendar-session blocks preserve gaps and short-range dependence."""
    values = np.asarray(values, dtype=float)
    n = len(values)
    if n < 2 * block or np.isfinite(values).sum() < 2 * block:
        return None
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(samples):
        starts = rng.integers(0, n - block + 1, size=(n + block - 1) // block)
        indices = (starts[:, None] + np.arange(block)).ravel()[:n]
        sample = values[indices]
        if np.isfinite(sample).any():
            draws.append(float(np.nanmean(sample)))
    return [float(x) for x in np.quantile(draws, [0.025, 0.975])] if draws else None


def summaries(metrics):
    periods = {str(y): (f"{y}-01-01", f"{y}-12-31") for y in range(2020, 2025)}
    periods.update(
        {
            "2020-2022": ("2020-01-01", "2022-12-31"),
            "2023-2024": ("2023-01-01", "2024-12-31"),
            "2020-2024": ("2020-01-01", "2024-12-31"),
        }
    )
    results = []
    for period, (start, end) in periods.items():
        subset = metrics[metrics.trade_date.between(start, end)]
        for name in FEATURES:
            g = subset[subset.feature == name]
            values = g.rank_ic.dropna()
            results.append(
                {
                    "period": period,
                    "feature": name,
                    "calendar_sessions": len(g),
                    "valid_ic_sessions": len(values),
                    "paired_observations": int(g.pairs.sum()),
                    "mean_rank_ic": float(values.mean()) if len(values) else None,
                    "positive_ic_fraction": float(values.gt(0).mean()) if len(values) else None,
                    "mean_size_rank_correlation": nullable_mean(g.size_rank_correlation),
                    "mean_turnover_rank_correlation": nullable_mean(g.turnover_rank_correlation),
                    "moving_block_95_interval": block_interval(g.rank_ic),
                }
            )
    return results


def nullable_mean(values):
    clean = values.dropna()
    return float(clean.mean()) if len(clean) else None


def feature_correlations(features):
    pairs = {(a, b): [] for i, a in enumerate(FEATURES) for b in FEATURES[i + 1 :]}
    for _, g in features.groupby("trade_date", sort=True):
        for (a, b), values in pairs.items():
            value, _ = rank_correlation(g[a], g[b])
            if value is not None:
                values.append(value)
    return [
        {
            "left": a,
            "right": b,
            "valid_sessions": len(v),
            "mean_rank_correlation": float(np.mean(v)) if v else None,
        }
        for (a, b), v in pairs.items()
    ]


def load_inputs(root, manifest, codes):
    """Read one saved market partition at a time, retaining the fixed cohort."""
    entries = manifest["inputs"]
    pieces = {}
    for kind, columns in (
        ("daily", [*KEYS, *RAW]),
        ("adj_factor", [*KEYS, "adj_factor"]),
        ("daily_basic", [*KEYS, "circ_mv", "turnover_rate"]),
    ):
        frames = []
        for day in manifest["sessions"]:
            name = f"data/canonical/{kind}/year={day[:4]}/month={day[5:7]}/{day}.parquet"
            raw = (root / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != entries[name]["sha256"]:
                raise DataValidationError("market partition changed while reading")
            f = pd.read_parquet(io.BytesIO(raw), columns=columns, use_threads=False)
            if not pd.to_datetime(f.trade_date).eq(pd.Timestamp(day)).all():
                raise DataValidationError("wrong source partition date")
            if f[KEYS].isna().any().any() or f.duplicated(KEYS).any():
                raise DataValidationError("invalid source market keys")
            frames.append(f[f.instrument_id.isin(codes)])
        pieces[kind] = pd.concat(frames, ignore_index=True)
    combined = pieces["daily"].merge(
        pieces["adj_factor"], on=KEYS, how="outer", validate="one_to_one"
    )
    combined = combined.merge(pieces["daily_basic"], on=KEYS, how="outer", validate="one_to_one")
    flows = []
    for name in sorted(entries):
        if "/attempts/flow_" not in name or not name.endswith("/response.body"):
            continue
        payload = json.loads((root / name).read_bytes())
        if payload["code"] != 0:
            raise DataValidationError("unavailable funding response")
        frame = pd.DataFrame(payload["data"]["items"], columns=payload["data"]["fields"])
        frame = frame.rename(columns={"ts_code": "instrument_id"})
        frame["trade_date"] = pd.to_datetime(frame.trade_date, format="%Y%m%d", errors="raise")
        flows.append(frame[[*KEYS, *FLOW]])
    flow = pd.concat(flows, ignore_index=True)
    return combined.merge(flow, on=KEYS, how="outer", validate="one_to_one")


def start_attempt(out, identity, head):
    if (out / "started.json").exists():
        raise DataValidationError("funding pilot actual attempt already consumed")
    if out.exists() and any(p.name != "worker.lock" for p in out.iterdir()):
        raise DataValidationError("unexplained prior funding pilot output")
    return atomic_seal(
        out / "started.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "identity": identity,
            "source_head": head,
            "actual_attempt": 1,
            "funding_feature_ids": list(FEATURES),
            "economic_paths_used": 0,
            "model_fits_used": 0,
        },
    )


def run(root: Path):
    config = json.loads((root / CONFIG).read_text())
    if (
        config["feature_ids"] != list(FEATURES)
        or config["feature_windows"] != [5, 20]
        or config["signal_start"] != "2020-01-01"
        or config["signal_end"] != "2024-12-31"
        or config["bootstrap_block_sessions"] != 20
        or config["bootstrap_samples"] != 1000
        or config["bootstrap_seed"] != 20260912
        or config["label_entry_lag"] != 1
        or config["label_exit_lag"] != 6
        or config["minimum_cross_section"] != 30
        or config["max_actual_attempts"] != 1
        or config["output"] != "data/products/research_program/launch_20260912/funding_diagnostics"
        or config["provider_calls_authorized"] != 0
        or config["economic_paths_authorized"] != 0
        or config["model_fits_authorized"] != 0
    ):
        raise DataValidationError("unreviewed funding pilot contract")
    manifest_path = root / config["input_manifest_path"]
    if _sha(manifest_path) != config["input_manifest_sha256"]:
        raise DataValidationError("funding input manifest changed")
    manifest = sealed_read(manifest_path)
    if manifest["fingerprint"] != config["input_manifest_fingerprint"]:
        raise DataValidationError("funding input identity changed")
    verify_entries(root, manifest["inputs"])
    selection = sealed_read(root / config["selection_path"])
    if selection["fingerprint"] != manifest["selection_fingerprint"]:
        raise DataValidationError("funding cohort changed")
    codes = selection["instrument_ids"]
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("funding processing requires clean pushed code")
    out = root / config["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=256 * 1024**2, projected_memory=512 * 1024**2)
        intent = start_attempt(out, manifest["fingerprint"], head)
        try:
            with budget.watchdog():
                inputs = load_inputs(root, manifest, codes)
                inputs.to_parquet(out / "joined_inputs.parquet", index=False)
                features = compute_features(inputs, manifest["sessions"], codes)
                features = features[features.trade_date.between("2020-01-01", "2024-12-31")]
                features.to_parquet(out / "features_and_labels.parquet", index=False)
                metrics = daily_metrics(features)
                metrics.to_parquet(out / "daily_metrics.parquet", index=False)
                metrics.to_csv(out / "daily_metrics.csv", index=False)
                summary = summaries(metrics)
                correlations = feature_correlations(features)
                budget.check()
                verify_entries(root, manifest["inputs"])
                binding.check()
                report = atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "code_inputs": binding.entries,
                        "input_manifest_fingerprint": manifest["fingerprint"],
                        "cohort_count": len(codes),
                        "grid_rows": len(features),
                        "feature_valid_counts": {
                            k: int(features[k].notna().sum()) for k in FEATURES
                        },
                        "valid_label_rows": int(features.label_5.notna().sum()),
                        "invalid_bar_rows": int((~features.valid_bar).sum()),
                        "missing_or_invalid_flow_rows": int((~features.valid_flow).sum()),
                        "summaries": summary,
                        "feature_correlations": correlations,
                        "historical_pit_certified": False,
                        "historical_candidate_selection_eligible": False,
                        "execution_authority": False,
                        "performance_evidence": False,
                        "actual_processing_attempts": 1,
                        "funding_features_used": list(FEATURES),
                        "economic_paths_used": 0,
                        "model_fits_used": 0,
                        "artifacts": {
                            p.name: {"sha256": _sha(p), "bytes": p.stat().st_size}
                            for p in sorted(out.iterdir())
                            if p.suffix in {".parquet", ".csv"}
                        },
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes": budget.check(),
                        },
                    },
                )
                return report
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {
                    "at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "intent_fingerprint": intent["fingerprint"],
                },
            )
            raise
