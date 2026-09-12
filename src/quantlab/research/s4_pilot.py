"""Preregistered S4 signal associations, separate from portfolio economics."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.funding_pilot import RAW, block_interval, nullable_mean, rank_correlation
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/s4_conditional_reversal_diagnostics_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/s4_conditional_reversal"
VARIANTS = ("S4-A", "S4-B", "S4-C")
KEYS = ["instrument_id", "trade_date"]
FLAGS = (
    "historical_pit_certified",
    "performance_evidence",
    "execution_authority",
    "candidate_promotion_eligible",
    "historical_candidate_selection_eligible",
)


def exact_grid(frame, sessions, codes, *, allow_missing=False):
    g = frame.copy()
    g["trade_date"] = pd.to_datetime(g.trade_date, errors="raise")
    days = pd.DatetimeIndex(sessions)
    expected = pd.MultiIndex.from_product([codes, days], names=KEYS)
    if (
        not len(expected)
        or expected.has_duplicates
        or not expected.is_monotonic_increasing
        or days.tz is not None
        or not days.normalize().equals(days)
        or g[KEYS].isna().any().any()
        or g.duplicated(KEYS).any()
        or not g.instrument_id.isin(codes).all()
        or not g.trade_date.isin(days).all()
    ):
        raise DataValidationError("S4 input must retain its fixed code/session grid")
    indexed = g.set_index(KEYS).sort_index()
    if not allow_missing and not indexed.index.equals(expected):
        raise DataValidationError("S4 parent grid is incomplete")
    return indexed.reindex(expected).reset_index()


def adjusted_price(frame):
    numeric = frame[[*RAW, "adj_factor"]]
    if any(
        not pd.api.types.is_numeric_dtype(numeric[k]) or pd.api.types.is_bool_dtype(numeric[k])
        for k in numeric
    ):
        raise DataValidationError("S4 prices must be numeric observations")
    valid = (
        np.isfinite(numeric).all(axis=1)
        & numeric.gt(0).all(axis=1)
        & frame.low.le(frame.high)
        & frame.open.between(frame.low, frame.high)
        & frame.close.between(frame.low, frame.high)
    )
    price = frame.close * frame.adj_factor
    return price.where(valid & np.isfinite(price) & price.gt(0))


def signal_values(parent, warmup, sessions, warmup_sessions, codes):
    """Only trailing prices/F1 enter signals; labels are carried through unchanged."""
    g = exact_grid(parent, sessions, codes)
    warm = exact_grid(warmup, warmup_sessions, codes, allow_missing=True)
    if pd.Timestamp(warmup_sessions[-1]) >= pd.Timestamp(sessions[0]):
        raise DataValidationError("warmup must strictly precede the signal calendar")
    price = adjusted_price(g)
    saved = g.adjusted_close
    if not np.allclose(price.to_numpy(), saved.to_numpy(), rtol=1e-12, atol=0, equal_nan=True):
        raise DataValidationError("S4 parent adjusted research prices changed")
    for key in ("F1", "momentum20", "label_5"):
        if not pd.api.types.is_numeric_dtype(g[key]) or pd.api.types.is_bool_dtype(g[key]):
            raise DataValidationError("S4 saved measures must remain numeric")
    prices = pd.concat(
        [warm[KEYS].assign(price=adjusted_price(warm)), g[KEYS].assign(price=price)],
        ignore_index=True,
    ).sort_values(KEYS)
    for window in (3, 60):
        prices[f"change{window}"] = prices.groupby("instrument_id", sort=False).price.transform(
            lambda p, n=window: (p / p.shift(n) - 1).where(
                p.rolling(n + 1, min_periods=n + 1).count().eq(n + 1)
            )
        )
    changes = prices[prices.trade_date.isin(pd.DatetimeIndex(sessions))]
    g = g.merge(changes[[*KEYS, "change3", "change60"]], on=KEYS, validate="one_to_one")
    g[["change3", "change60"]] = g[["change3", "change60"]].replace([np.inf, -np.inf], np.nan)
    median = g.groupby("trade_date", sort=False).change3.transform("median")
    g["S4-A"] = median - g.change3
    g["condition_A"] = pd.Series(True, index=g.index, dtype="boolean").where(g["S4-A"].notna())
    g["condition_B"] = g.change60.gt(0).astype("boolean").where(g.change60.notna())
    previous_flow = g.groupby("instrument_id", sort=False).F1.shift(1)
    known_flow = np.isfinite(g.F1) & np.isfinite(previous_flow)
    g["condition_C"] = (g.F1.gt(0) & previous_flow.le(0)).astype("boolean").where(known_flow)
    g["S4-B"] = g["S4-A"].where(g.condition_B.fillna(False))
    g["S4-C"] = g["S4-A"].where(g.condition_C.fillna(False))
    g["price_valid"] = price.notna().to_numpy()
    return g


def validate_saved_labels(frame, sessions):
    days = pd.DatetimeIndex(sessions)
    mapping = {d: i for i, d in enumerate(days)}
    for row in frame.loc[
        frame.label_5.notna(), ["trade_date", "label_entry_date", "label_exit_date"]
    ].itertuples(index=False):
        day, entry, exit_day = map(pd.Timestamp, row)
        i = mapping[day]
        if (
            i + 6 >= len(days)
            or entry != days[i + 1]
            or exit_day != days[i + 6]
            or not day.year == entry.year == exit_day.year
        ):
            raise DataValidationError("S4 labels must retain year-contained t+1/t+6 chronology")


def daily_metrics(frame, minimum=30):
    rows = []
    for day, group in frame.groupby("trade_date", sort=True):
        for name in VARIANTS:
            condition = group[f"condition_{name[-1]}"]
            common = (
                group[[name, "momentum20", "label_5"]].replace([np.inf, -np.inf], np.nan).dropna()
            )
            ic, pairs = rank_correlation(common[name], common.label_5, minimum)
            reference, _ = rank_correlation(-common.momentum20, common.label_5, minimum)
            if ic is None or reference is None:
                ic, reference = None, None
            size, _ = rank_correlation(
                group[name], np.log(group.circ_mv.where(group.circ_mv.gt(0))), minimum
            )
            turnover, _ = rank_correlation(group[name], group.turnover_rate, minimum)
            reversal, _ = rank_correlation(group[name], -group.momentum20, minimum)
            rows.append(
                {
                    "trade_date": day,
                    "variant": name,
                    "grid_count": len(group),
                    "base_signal_count": int(group["S4-A"].notna().sum()),
                    "condition_pass_count": int(condition.eq(True).sum()),
                    "condition_fail_count": int(condition.eq(False).sum()),
                    "condition_unknown_count": int(condition.isna().sum()),
                    "retained_signal_count": int(group[name].notna().sum()),
                    "label_complete_count": int(
                        (np.isfinite(group[name]) & np.isfinite(group.label_5)).sum()
                    ),
                    "pairs": pairs,
                    "rank_ic": ic,
                    "reversal_rank_ic": reference,
                    "paired_ic_difference": ic - reference if ic is not None else None,
                    "size_rank_correlation": size,
                    "turnover_rank_correlation": turnover,
                    "reversal_rank_correlation": reversal,
                }
            )
    return pd.DataFrame(rows)


def summarize(metrics):
    periods = [(str(y), str(y), str(y)) for y in range(2020, 2025)] + [
        ("2020-2022", "2020", "2022"),
        ("2023-2024", "2023", "2024"),
        ("2020-2024", "2020", "2024"),
    ]
    columns = [c for c in metrics if c not in ("trade_date", "variant", "pairs")]
    output = []
    for period, start, end in periods:
        for variant in VARIANTS:
            g = metrics[
                (metrics.variant == variant)
                & metrics.trade_date.between(start + "-01-01", end + "-12-31")
            ]
            output.append(
                {
                    "period": period,
                    "variant": variant,
                    "calendar_sessions": len(g),
                    "valid_ic_sessions": int(g.rank_ic.notna().sum()),
                    "paired_observations": int(g.pairs.sum()),
                    **{f"mean_{k}": nullable_mean(g[k]) for k in columns},
                    "ic_block_95_interval": block_interval(g.rank_ic),
                    "difference_block_95_interval": block_interval(g.paired_ic_difference),
                }
            )
    return output


def load_inputs(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "s4_conditional_reversal_diagnostics_v1",
        "output": OUTPUT,
        "variant_ids": list(VARIANTS),
        "minimum_pairs": 30,
        "max_actual_attempts": 1,
        "provider_calls_authorized": 0,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
        "condition_windows": {"relative_reversal": 3, "positive_trend": 60, "funding_cross_lag": 1},
    }
    if any(config.get(k) != v or type(config.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed S4 signal contract")
    verify_entries(root, config["inputs"])
    parent = sealed_read(root / config["parent_report_path"])
    proof = sealed_read(root / config["parent_proof_path"])
    manifest = sealed_read(root / config["calendar_manifest_path"])
    if (
        parent["fingerprint"] != config["parent_report_fingerprint"]
        or proof["report_fingerprint"] != parent["fingerprint"]
        or any(parent.get(k) is not False for k in FLAGS[:3])
    ):
        raise DataValidationError("unverified S4 parent evidence")
    verify_entries((root / config["parent_report_path"]).parent, parent["artifacts"])
    expected_days = [d for d in manifest["sessions"] if d < "2020-01-01"]
    expected_paths = [
        f"data/canonical/{kind}/year={day[:4]}/month={day[5:7]}/{day}.parquet"
        for day in expected_days
        for kind in ("daily", "adj_factor")
    ]
    if (
        len(expected_days) != 61
        or config["warmup_sessions"] != expected_days
        or config["warmup_paths"] != expected_paths
        or any(config["inputs"][p] != manifest["inputs"][p] for p in expected_paths)
    ):
        raise DataValidationError("S4 price warmup changed")
    return config, parent, manifest


def load_warmup(root, config, codes):
    pieces = {"daily": [], "adj_factor": []}
    for name in config["warmup_paths"]:
        kind = "adj_factor" if "/adj_factor/" in name else "daily"
        columns = [*KEYS, *(RAW if kind == "daily" else ["adj_factor"])]
        f = pd.read_parquet(root / name, columns=columns, use_threads=False)
        day = name.rsplit("/", 1)[-1].removesuffix(".parquet")
        if (
            f[KEYS].isna().any().any()
            or f.duplicated(KEYS).any()
            or not pd.to_datetime(f.trade_date).eq(pd.Timestamp(day)).all()
        ):
            raise DataValidationError("invalid warmup partition keys or date")
        pieces[kind].append(f[f.instrument_id.isin(codes)])
    return pd.concat(pieces["daily"]).merge(
        pd.concat(pieces["adj_factor"]), on=KEYS, how="outer", validate="one_to_one"
    )


def start_attempt(out, head, parent, config_hash, binding):
    if any(p.name != "worker.lock" for p in out.iterdir()):
        raise DataValidationError("S4 actual signal attempt already consumed or unexplained output")
    return atomic_seal(
        out / "started.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "source_head": head,
            "code_inputs": binding,
            "parent_report_fingerprint": parent,
            "config_sha256": config_hash,
            "actual_attempt": 1,
            "variant_ids": list(VARIANTS),
            "candidate_identities_used": 3,
            "economic_paths_used": 0,
            "model_fits_used": 0,
        },
    )


def run(root):
    config, parent, manifest = load_inputs(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("S4 pilot requires clean pushed source")
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=128 * 1024**2, projected_memory=512 * 1024**2)
        intent = start_attempt(
            out, head, parent["fingerprint"], _sha(root / CONFIG), binding.entries
        )
        try:
            with budget.watchdog():
                frame = pd.read_parquet(root / config["features_path"], use_threads=False)
                selection = sealed_read(root / config["selection_path"])
                codes = selection["instrument_ids"]
                sessions = [d for d in manifest["sessions"] if "2020-01-01" <= d <= "2024-12-31"]
                validate_saved_labels(frame, sessions)
                warmup = load_warmup(root, config, codes)
                values = signal_values(frame, warmup, sessions, config["warmup_sessions"], codes)
                kept = [
                    *KEYS,
                    "change3",
                    "change60",
                    "price_valid",
                    *VARIANTS,
                    "condition_A",
                    "condition_B",
                    "condition_C",
                ]
                values[kept].to_parquet(out / "signals.parquet", index=False)
                metrics = daily_metrics(values)
                metrics.to_parquet(out / "daily.parquet", index=False)
                metrics.to_csv(out / "daily.csv", index=False)
                summary = summarize(metrics)
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "schema": "s4_signal_report_v1",
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "parent_report_fingerprint": parent["fingerprint"],
                        "code_inputs": binding.entries,
                        "variant_ids": list(VARIANTS),
                        "candidate_identities_used": 3,
                        "actual_attempts": 1,
                        "grid_rows": len(values),
                        "cohort_count": len(codes),
                        "warmup_sessions": len(config["warmup_sessions"]),
                        "signal_valid_counts": {k: int(values[k].notna().sum()) for k in VARIANTS},
                        "condition_counts": {
                            k: {
                                "pass": int(values[k].eq(True).sum()),
                                "fail": int(values[k].eq(False).sum()),
                                "unknown": int(values[k].isna().sum()),
                            }
                            for k in ("condition_A", "condition_B", "condition_C")
                        },
                        "summaries": summary,
                        **dict.fromkeys(FLAGS, False),
                        "provider_calls_used": 0,
                        "economic_paths_used": 0,
                        "model_fits_used": 0,
                        "new_alpha_formula_ids": [],
                        "new_funding_feature_ids": [],
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
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {"error_type": type(exc).__name__, "intent_fingerprint": intent["fingerprint"]},
            )
            raise


def read_progress(root):
    if not (root / CONFIG).exists():
        return None
    config = json.loads((root / CONFIG).read_text())
    out = (root / config["output"]).resolve()
    if not out.is_relative_to(root.resolve()):
        raise DataValidationError("S4 output leaves the workspace")
    if not (out / "report.json").exists() or not (out / "independent_proof.json").exists():
        return None
    config, parent, _ = load_inputs(root)
    report, proof, intent = (
        sealed_read(out / name)
        for name in ("report.json", "independent_proof.json", "started.json")
    )
    if (
        report["parent_report_fingerprint"] != parent["fingerprint"]
        or report["intent_fingerprint"] != intent["fingerprint"]
        or proof["report_fingerprint"] != report["fingerprint"]
        or intent["config_sha256"] != _sha(root / CONFIG)
        or report["variant_ids"] != list(VARIANTS)
        or report["actual_attempts"] != 1
        or report["candidate_identities_used"] != 3
        or any(p.get(k) is not False for p in (report, proof) for k in FLAGS)
        or any(
            report.get(k) != 0
            for k in ("provider_calls_used", "economic_paths_used", "model_fits_used")
        )
        or any(
            proof.get(k) is not True
            for k in (
                "all_signal_rows_verified",
                "all_daily_pairs_verified",
                "summaries_and_intervals_verified",
                "input_hashes_verified",
            )
        )
    ):
        raise DataValidationError("unverified S4 signal evidence")
    verify_entries(out, report["artifacts"])
    verify_entries(out, proof["artifacts"])
    return report
