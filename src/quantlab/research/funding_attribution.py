"""Fixed same-cohort decomposition of the observed F4 association."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.funding_pilot import block_interval, rank_correlation
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/funding_attribution_v1.json"
SERIES = ("F4", "reversal_component", "F2", "F4_minus_reversal", "F2_within_momentum")


def conditional_ic(frame, minimum=30):
    """Five fixed past-momentum buckets; require every bucket, never select winners."""
    if not len(frame) or frame.momentum20.nunique() < 2:
        return None
    bins = np.floor(5 * (frame.momentum20.rank(method="average") - 1) / len(frame)).astype(int)
    if set(bins) != set(range(5)):
        return None
    weighted, total = 0.0, 0
    for bucket in range(5):
        part = frame[bins == bucket]
        value, count = rank_correlation(part.F2, part.label_5, minimum)
        if value is None:
            return None
        weighted += value * count
        total += count
    return weighted / total


def decompose(features, minimum=30):
    if (
        features.duplicated(["instrument_id", "trade_date"]).any()
        or features[["instrument_id", "trade_date"]].isna().any().any()
    ):
        raise DataValidationError("invalid attribution row identity")
    rows = []
    for day, g in features.groupby("trade_date", sort=True):
        common = (
            g[["F4", "F2", "momentum20", "label_5"]].replace([np.inf, -np.inf], np.nan).dropna()
        )
        f4, n = rank_correlation(common.F4, common.label_5, minimum)
        reversal, _ = rank_correlation(-common.momentum20, common.label_5, minimum)
        flow, _ = rank_correlation(common.F2, common.label_5, minimum)
        difference = f4 - reversal if f4 is not None and reversal is not None else None
        rows.append(
            {
                "trade_date": day,
                "pairs": n,
                "F4": f4,
                "reversal_component": reversal,
                "F2": flow,
                "F4_minus_reversal": difference,
                "F2_within_momentum": conditional_ic(common, minimum),
            }
        )
    return pd.DataFrame(rows)


def summarize(frame):
    periods = [(str(y), str(y), str(y)) for y in range(2020, 2025)]
    periods += [
        ("2020-2022", "2020", "2022"),
        ("2023-2024", "2023", "2024"),
        ("2020-2024", "2020", "2024"),
    ]
    result = []
    for name, start, end in periods:
        g = frame[frame.trade_date.between(start + "-01-01", end + "-12-31")]
        for series in SERIES:
            v = g[series].dropna()
            result.append(
                {
                    "period": name,
                    "series": series,
                    "calendar_sessions": len(g),
                    "valid_sessions": len(v),
                    "mean": float(v.mean()) if len(v) else None,
                    "moving_block_95_interval": block_interval(g[series]),
                }
            )
    return result


def load_base(root):
    config = json.loads((root / CONFIG).read_text())
    verify_entries(root, config["inputs"])
    report = sealed_read(root / config["pilot_report_path"])
    proof = sealed_read(root / config["pilot_proof_path"])
    if (
        report["fingerprint"] != config["pilot_report_fingerprint"]
        or proof["report_fingerprint"] != report["fingerprint"]
        or any(
            report.get(k) is not False
            for k in (
                "historical_pit_certified",
                "historical_candidate_selection_eligible",
                "execution_authority",
                "performance_evidence",
            )
        )
    ):
        raise DataValidationError("unverified funding pilot evidence")
    return config, report, proof


def run(root):
    config, report, proof = load_base(root)
    if (
        config["minimum_pairs"] != 30
        or config["momentum_buckets"] != 5
        or config["max_actual_attempts"] != 1
        or config["new_funding_feature_ids"] != []
        or config["provider_calls_authorized"] != 0
        or config["model_fits_authorized"] != 0
        or config["economic_paths_authorized"] != 0
        or config["output"] != "data/products/research_program/launch_20260912/funding_attribution"
    ):
        raise DataValidationError("unreviewed attribution contract")
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("attribution requires clean pushed source")
    out = root / config["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("attribution attempt already consumed or output unexplained")
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=64 * 1024**2, projected_memory=256 * 1024**2)
        intent = atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "parent_report_fingerprint": report["fingerprint"],
                "parent_proof_fingerprint": proof["fingerprint"],
                "config_sha256": _sha(root / CONFIG),
                "actual_attribution_attempt": 1,
                "new_funding_feature_ids": [],
                "economic_paths_used": 0,
                "model_fits_used": 0,
            },
        )
        try:
            with budget.watchdog():
                features = pd.read_parquet(root / config["features_path"], use_threads=False)
                daily = decompose(features)
                daily.to_parquet(out / "daily.parquet", index=False)
                daily.to_csv(out / "daily.csv", index=False)
                summary = summarize(daily)
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "parent_report_fingerprint": report["fingerprint"],
                        "summaries": summary,
                        "calendar_sessions": len(daily),
                        "actual_attribution_attempts": 1,
                        "new_funding_feature_ids": [],
                        "economic_paths_used": 0,
                        "model_fits_used": 0,
                        "historical_pit_certified": False,
                        "performance_evidence": False,
                        "execution_authority": False,
                        "candidate_promotion_eligible": False,
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
    """Only verified saved results; this reader cannot start providers or computation."""
    if not (root / CONFIG).exists():
        return None
    config = json.loads((root / CONFIG).read_text())
    if not (root / config["pilot_proof_path"]).exists():
        return None
    config, report, proof = load_base(root)
    verify_entries((root / config["pilot_report_path"]).parent, report["artifacts"])
    out = root / config["output"]
    attribution = None
    if (out / "report.json").exists() and (out / "independent_proof.json").exists():
        attribution = sealed_read(out / "report.json")
        companion = sealed_read(out / "independent_proof.json")
        if (
            attribution["parent_report_fingerprint"] != report["fingerprint"]
            or companion["report_fingerprint"] != attribution["fingerprint"]
            or any(
                attribution.get(k) is not False
                for k in (
                    "historical_pit_certified",
                    "execution_authority",
                    "performance_evidence",
                    "candidate_promotion_eligible",
                )
            )
        ):
            raise DataValidationError("unverified funding attribution")
        verify_entries(out, attribution["artifacts"])
    return {"pilot": report, "proof": proof, "attribution": attribution, "config": config}
