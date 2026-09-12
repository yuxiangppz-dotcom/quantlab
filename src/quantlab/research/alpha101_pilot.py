"""Two preregistered price-volume associations; no fitted model or portfolio."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.funding_pilot import block_interval, nullable_mean, rank_correlation
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/alpha101_price_volume_pilot_v1.json"
FORMULAS = ("alpha101_12", "alpha101_101")
KEYS = ["instrument_id", "trade_date"]
FLAGS = (
    "historical_pit_certified",
    "performance_evidence",
    "execution_authority",
    "candidate_promotion_eligible",
)


def formula_values(frame, sessions, codes):
    """Raw-CNY literal formulas with the declared #12 corporate-action mask."""
    g = frame.copy()
    g["trade_date"] = pd.to_datetime(g.trade_date, errors="raise")
    expected = pd.MultiIndex.from_product([codes, pd.DatetimeIndex(sessions)], names=KEYS)
    if (
        not len(expected)
        or g[KEYS].isna().any().any()
        or g.duplicated(KEYS).any()
        or expected.has_duplicates
        or not expected.is_monotonic_increasing
        or not g.set_index(KEYS).sort_index().index.equals(expected)
    ):
        raise DataValidationError("formula grid must retain exact code/session population")
    g = g.sort_values(KEYS).reset_index(drop=True)
    numeric = g[["open", "high", "low", "close", "volume"]]
    if any(
        not pd.api.types.is_numeric_dtype(g[k]) or pd.api.types.is_bool_dtype(g[k])
        for k in [*numeric.columns, "adj_factor"]
    ):
        raise DataValidationError("formula inputs must be numeric observations")
    valid = (
        np.isfinite(numeric).all(axis=1)
        & numeric.gt(0).all(axis=1)
        & g.low.le(g.high)
        & g.open.between(g.low, g.high)
        & g.close.between(g.low, g.high)
    )
    g["formula_bar_valid"] = valid
    previous = g.groupby("instrument_id", sort=False).shift(1)
    stable_units = (
        np.isfinite(g.adj_factor) & g.adj_factor.gt(0) & g.adj_factor.eq(previous.adj_factor)
    )
    valid_pair = valid & previous.formula_bar_valid.eq(True) & stable_units
    g["alpha101_12"] = (-np.sign(g.volume - previous.volume) * (g.close - previous.close)).where(
        valid_pair
    )
    g["alpha101_101"] = ((g.close - g.open) / (g.high - g.low + 0.001)).where(valid)
    g["alpha101_12_pair_valid"] = valid_pair
    g[list(FORMULAS)] = g[list(FORMULAS)].replace([np.inf, -np.inf], np.nan)
    return g


def paired_metrics(frame, minimum=30):
    """Each formula and reversal use the very same label-complete population."""
    rows = []
    for day, group in frame.groupby("trade_date", sort=True):
        redundancy, _ = rank_correlation(group[FORMULAS[0]], group[FORMULAS[1]], minimum)
        for name in FORMULAS:
            common = (
                group[[name, "momentum20", "label_5"]].replace([np.inf, -np.inf], np.nan).dropna()
            )
            ic, pairs = rank_correlation(common[name], common.label_5, minimum)
            reversal, _ = rank_correlation(-common.momentum20, common.label_5, minimum)
            if ic is None or reversal is None:
                ic, reversal = None, None
            size, _ = rank_correlation(
                group[name], np.log(group.circ_mv.where(group.circ_mv.gt(0))), minimum
            )
            turnover, _ = rank_correlation(group[name], group.turnover_rate, minimum)
            overlap, _ = rank_correlation(group[name], -group.momentum20, minimum)
            rows.append(
                {
                    "trade_date": day,
                    "formula": name,
                    "pairs": pairs,
                    "rank_ic": ic,
                    "reversal_rank_ic": reversal,
                    "paired_ic_difference": ic - reversal
                    if ic is not None and reversal is not None
                    else None,
                    "size_rank_correlation": size,
                    "turnover_rank_correlation": turnover,
                    "reversal_rank_correlation": overlap,
                    "inter_formula_rank_correlation": redundancy,
                }
            )
    return pd.DataFrame(rows)


def summarize(metrics):
    periods = [(str(y), str(y), str(y)) for y in range(2020, 2025)]
    periods += [
        ("2020-2022", "2020", "2022"),
        ("2023-2024", "2023", "2024"),
        ("2020-2024", "2020", "2024"),
    ]
    columns = [c for c in metrics if c not in ["trade_date", "formula", "pairs"]]
    result = []
    for period, start, end in periods:
        for name in FORMULAS:
            g = metrics[
                (metrics.formula == name)
                & metrics.trade_date.between(start + "-01-01", end + "-12-31")
            ]
            result.append(
                {
                    "period": period,
                    "formula": name,
                    "calendar_sessions": len(g),
                    "valid_ic_sessions": int(g.rank_ic.notna().sum()),
                    "paired_observations": int(g.pairs.sum()),
                    **{f"mean_{key}": nullable_mean(g[key]) for key in columns},
                    "ic_block_95_interval": block_interval(g.rank_ic),
                    "difference_block_95_interval": block_interval(g.paired_ic_difference),
                }
            )
    return result


def load_inputs(root):
    config = json.loads((root / CONFIG).read_text())
    verify_entries(root, config["inputs"])
    parent = sealed_read(root / config["parent_report_path"])
    proof = sealed_read(root / config["parent_proof_path"])
    if (
        parent["fingerprint"] != config["parent_report_fingerprint"]
        or proof["report_fingerprint"] != parent["fingerprint"]
        or any(
            parent.get(k) is not False
            for k in ("historical_pit_certified", "performance_evidence", "execution_authority")
        )
    ):
        raise DataValidationError("unverified parent price-volume evidence")
    verify_entries((root / config["parent_report_path"]).parent, parent["artifacts"])
    return config, parent


def start_attempt(out, head, parent, config_hash):
    if any(p.name != "worker.lock" for p in out.iterdir()):
        raise DataValidationError("Alpha101 actual attempt already consumed or unexplained output")
    return atomic_seal(
        out / "started.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "source_head": head,
            "parent_report_fingerprint": parent,
            "config_sha256": config_hash,
            "actual_attempt": 1,
            "formula_ids": list(FORMULAS),
            "economic_paths_used": 0,
            "model_fits_used": 0,
        },
    )


def run(root):
    config, parent = load_inputs(root)
    if (
        config["schema"] != "alpha101_price_volume_pilot_v1"
        or config["formula_ids"] != list(FORMULAS)
        or config["minimum_pairs"] != 30
        or config["max_actual_attempts"] != 1
        or config["output"]
        != "data/products/research_program/launch_20260912/alpha101_price_volume"
        or any(
            config[k] != 0
            for k in (
                "provider_calls_authorized",
                "economic_paths_authorized",
                "model_fits_authorized",
            )
        )
    ):
        raise DataValidationError("unreviewed Alpha101 pilot contract")
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("Alpha101 pilot requires clean pushed source")
    out = root / config["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=128 * 1024**2, projected_memory=512 * 1024**2)
        intent = start_attempt(out, head, parent["fingerprint"], _sha(root / CONFIG))
        try:
            with budget.watchdog():
                frame = pd.read_parquet(root / config["features_path"], use_threads=False)
                manifest = sealed_read(root / config["calendar_manifest_path"])
                selection = sealed_read(root / config["selection_path"])
                sessions = [d for d in manifest["sessions"] if "2020-01-01" <= d <= "2024-12-31"]
                values = formula_values(frame, sessions, selection["instrument_ids"])
                selected = [*KEYS, *FORMULAS, "formula_bar_valid", "alpha101_12_pair_valid"]
                values[selected].to_parquet(out / "formulas.parquet", index=False)
                metrics = paired_metrics(values)
                metrics.to_parquet(out / "daily.parquet", index=False)
                metrics.to_csv(out / "daily.csv", index=False)
                summary = summarize(metrics)
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "schema": "alpha101_price_volume_report_v1",
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "parent_report_fingerprint": parent["fingerprint"],
                        "code_inputs": binding.entries,
                        "formula_ids": list(FORMULAS),
                        "new_formula_identities_used": 2,
                        "actual_attempts": 1,
                        "grid_rows": len(values),
                        "cohort_count": len(selection["instrument_ids"]),
                        "feature_valid_counts": {k: int(values[k].notna().sum()) for k in FORMULAS},
                        "summaries": summary,
                        **dict.fromkeys(FLAGS, False),
                        "historical_candidate_selection_eligible": False,
                        "provider_calls_used": 0,
                        "economic_paths_used": 0,
                        "model_fits_used": 0,
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
        raise DataValidationError("Alpha101 output leaves the workspace")
    if not (out / "report.json").exists() or not (out / "independent_proof.json").exists():
        return None
    config, parent = load_inputs(root)
    report = sealed_read(out / "report.json")
    proof = sealed_read(out / "independent_proof.json")
    intent = sealed_read(out / "started.json")
    if (
        report["parent_report_fingerprint"] != parent["fingerprint"]
        or report["intent_fingerprint"] != intent["fingerprint"]
        or proof["report_fingerprint"] != report["fingerprint"]
        or intent["config_sha256"] != _sha(root / CONFIG)
        or report["formula_ids"] != list(FORMULAS)
        or report["actual_attempts"] != 1
        or report["new_formula_identities_used"] != 2
        or any(payload.get(key) is not False for payload in (report, proof) for key in FLAGS)
        or any(
            report.get(key) != 0
            for key in ("provider_calls_used", "economic_paths_used", "model_fits_used")
        )
        or any(
            proof.get(key) is not True
            for key in (
                "all_formula_rows_verified",
                "all_daily_pairs_verified",
                "summaries_and_intervals_verified",
                "input_hashes_verified",
            )
        )
    ):
        raise DataValidationError("unverified Alpha101 pilot evidence")
    verify_entries(out, report["artifacts"])
    verify_entries(out, proof["artifacts"])
    return report
