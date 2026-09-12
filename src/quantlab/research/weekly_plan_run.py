"""Sealed local-only diagnostic and weekly pilot planning runner."""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime

import pyarrow.parquet as pq

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import load_report as load_rolling
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs
from quantlab.research.weekly_plan import monthly_summary, pilot_schedule, population_counts

CONFIG = "config/weekly_candidate_plan_v1.json"
OUTPUT = "data/products/weekly_candidate_plan/weekly_plan_20260912"


def run(root):
    config = json.loads((root / CONFIG).read_text())
    verify_entries(root, config["inputs"])
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("weekly plan requires clean pushed source")
    base = root / config["rolling_path"]
    old = load_rolling(base)
    old_plan = sealed_read(base / "plan.json")
    metadata = sealed_read(base / "metadata.json")
    inventory = sealed_read(root / config["inventory_path"])
    if (
        old["fingerprint"] != config["rolling_fingerprint"]
        or metadata["identity"] != old_plan["fingerprint"]
    ):
        raise DataValidationError("weekly planning source identity changed")
    verify_historical_inputs(
        root, {"code_head": old_plan["code_head"], "inputs": old_plan["code_files"]}
    )
    verify_entries(base, metadata["artifacts"])
    sessions = inventory["sessions"]
    schedule = pilot_schedule(sessions, config["cutoff"])
    if [row["week_monday"] for row in schedule] != config["expected_week_mondays"]:
        raise DataValidationError("pilot weeks differ from predeclared calendar rule")
    if schedule[-1]["evaluation_label_cutoff"] != config["evaluation_label_cutoff"]:
        raise DataValidationError("pilot maturity differs from fixed cutoff")
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        budget = Budget(out, config["resources"])
        plan = atomic_seal(
            out / "plan.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "code_head": head,
                "code_files": binding.entries,
                "config_sha256": _sha(root / CONFIG),
                "inputs": config["inputs"],
                "rolling_fingerprint": old["fingerprint"],
                "schedule": schedule,
                "new_fit_attempts": 0,
                "future_pilot_max_fit_attempts": 6,
            },
        )
        monthly, annual = [], []
        counts = [Counter() for _ in schedule]
        metadata_rows = 0
        seen_codes = set()
        with budget.watchdog():
            for item in config["daily_sources"]:
                frame = pq.read_table(root / item["path"], use_threads=False).to_pandas()
                expected = [day for day in sessions if item["start"] <= day <= item["end"]]
                summaries = monthly_summary(frame, expected)
                monthly.extend(
                    {"model": item["model"], "period": item["period"], **row} for row in summaries
                )
                annual.append(
                    {
                        "model": item["model"],
                        "period": item["period"],
                        "market_days": len(frame),
                        "score_rows": int(frame.score_rows.sum()),
                        "evaluation_rows": int(frame.evaluation_rows.sum()),
                        "rank_ic_days": int(frame.rank_ic.notna().sum()),
                        "mean_rank_ic": None
                        if frame.rank_ic.dropna().empty
                        else float(frame.rank_ic.mean()),
                    }
                )
            for name in metadata["artifacts"]:
                if not budget.can_start():
                    raise DataValidationError("weekly plan checkpoint required")
                budget.check(projected_bytes=1024**2, projected_memory=128 * 1024**2)
                frame = pq.read_table(base / name, use_threads=False).to_pandas()
                codes = set(frame.instrument_id)
                if codes & seen_codes:
                    raise DataValidationError("metadata code duplicated across partitions")
                seen_codes.update(codes)
                metadata_rows += len(frame)
                for target, row in zip(
                    counts, population_counts(frame, schedule, sessions), strict=True
                ):
                    target.update({key: value for key, value in row.items() if key != "week"})
        if metadata_rows != config["metadata_rows"] or len(seen_codes) != config["metadata_codes"]:
            raise DataValidationError("weekly plan lost source metadata")
        weekly = [{**week, **dict(count)} for week, count in zip(schedule, counts, strict=True)]
        for week in weekly:
            if not week["train_rows"] or not week["prediction_rows"]:
                raise DataValidationError("empty pilot population")
        binding.check()
        verify_entries(root, config["inputs"])
        monthly_artifact = atomic_seal(
            out / "monthly.json",
            {"plan_fingerprint": plan["fingerprint"], "rows": monthly, "annual": annual},
        )
        weekly_artifact = atomic_seal(
            out / "weekly.json",
            {
                "plan_fingerprint": plan["fingerprint"],
                "rows": weekly,
                "models": config["models"],
                "max_future_fit_attempts": 6,
                "actual_fit_attempts": 0,
            },
        )
        return atomic_seal(
            out / "report.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "plan_fingerprint": plan["fingerprint"],
                "artifacts": {
                    name: {"sha256": _sha(out / name)} for name in ("monthly.json", "weekly.json")
                },
                "monthly_fingerprint": monthly_artifact["fingerprint"],
                "weekly_fingerprint": weekly_artifact["fingerprint"],
                "metadata_rows": metadata_rows,
                "metadata_codes": len(seen_codes),
                "monthly_rows": len(monthly),
                "daily_rows": sum(row["market_days"] for row in annual),
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "bytes": budget.check(),
                },
                "new_fit_attempts": 0,
                "provider_calls": 0,
                "future_pilot_max_fit_attempts": 6,
                "historical_data_already_observed": True,
                "performance_evidence": False,
                "execution_authority": False,
                "automatic_promotion": False,
            },
        )


def read_report(root):
    out = root / OUTPUT
    if not (out / "report.json").exists():
        return None
    r, plan = sealed_read(out / "report.json"), sealed_read(out / "plan.json")
    if r["plan_fingerprint"] != plan["fingerprint"] or plan["config_sha256"] != _sha(root / CONFIG):
        raise DataValidationError("weekly plan source changed")
    for key in ("performance_evidence", "execution_authority", "automatic_promotion"):
        if r.get(key) is not False:
            raise DataValidationError("weekly plan gained financial authority")
    if (
        r.get("new_fit_attempts") != 0
        or type(r.get("new_fit_attempts")) is not int
        or r.get("provider_calls") != 0
        or type(r.get("provider_calls")) is not int
        or r.get("future_pilot_max_fit_attempts") != 6
        or r.get("historical_data_already_observed") is not True
    ):
        raise DataValidationError("weekly plan action budget changed")
    verify_historical_inputs(root, {"code_head": plan["code_head"], "inputs": plan["code_files"]})
    verify_entries(root, plan["inputs"])
    verify_entries(out, r["artifacts"])
    monthly, weekly = sealed_read(out / "monthly.json"), sealed_read(out / "weekly.json")
    for value, key in ((monthly, "monthly_fingerprint"), (weekly, "weekly_fingerprint")):
        if value["fingerprint"] != r[key] or value["plan_fingerprint"] != plan["fingerprint"]:
            raise DataValidationError("weekly plan output identity changed")
    config = json.loads((root / CONFIG).read_text())
    if (
        r["metadata_rows"] != config["metadata_rows"]
        or r["metadata_codes"] != config["metadata_codes"]
        or r["monthly_rows"] != len(monthly["rows"])
        or r["daily_rows"] != sum(row["market_days"] for row in monthly["annual"])
        or weekly["actual_fit_attempts"] != 0
        or weekly["max_future_fit_attempts"] != 6
        or weekly["models"] != config["models"]
        or len(weekly["rows"]) != 3
    ):
        raise DataValidationError("weekly plan population or model contract changed")
    return r, monthly, weekly


def read_verification(root, report, weekly):
    path = root / OUTPUT / "independent_verification.json"
    if not path.exists():
        return None
    review = sealed_read(path)
    plan = sealed_read(root / OUTPUT / "plan.json")
    expected = {
        key: report[key]
        for key in ("metadata_rows", "metadata_codes", "daily_rows", "monthly_rows")
    }
    expected.update(
        report_fingerprint=report["fingerprint"],
        source_head=plan["code_head"],
        new_fit_attempts=0,
        provider_calls=0,
    )
    if any(
        type(review.get(key)) is not type(value) or review[key] != value
        for key, value in expected.items()
    ):
        raise DataValidationError("weekly independent review source or population mismatch")
    fields = (
        "train_feature_rows",
        "train_rows",
        "train_unmatured_or_unknown_end",
        "train_missing_label_rows",
        "prediction_rows",
        "evaluation_rows",
        "prediction_missing_label_rows",
        "prediction_unmatured_or_unknown_end",
    )
    expected_counts = [{key: row[key] for key in fields} for row in weekly["rows"]]
    if review.get("weekly_populations") != expected_counts or any(
        type(value) is not int or value < 0
        for row in review["weekly_populations"]
        for value in row.values()
    ):
        raise DataValidationError("weekly independent review counts changed")
    if review.get("six_future_fit_slots") is not True or any(
        review.get(key) is not False for key in ("performance_evidence", "execution_authority")
    ):
        raise DataValidationError("weekly independent review authority changed")
    return review
