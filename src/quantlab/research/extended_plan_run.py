"""Seal a longer replay schedule; the planner cannot fit models or create scores."""

import hashlib
import json
import subprocess
import time

import pandas as pd
import pyarrow.parquet as pq

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, peak_rss_bytes
from quantlab.research.extended_plan import (
    attach_populations,
    daily_population,
    fit_inventory,
    schedule,
)
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs
from quantlab.research.weekly_pilot import load_report as load_pilot
from quantlab.research.weekly_pilot_data import train_mask, update_membership
from quantlab.research.weekly_pilot_protocol import (
    HEAVY,
    inherited_locks,
    now,
)
from quantlab.research.weekly_pilot_protocol import (
    OUTPUT as PILOT_OUTPUT,
)
from quantlab.research.weekly_pilot_review import read_verification as read_pilot_verification

CONFIG = "config/extended_frequency_plan_v1.json"
OUTPUT = "data/products/extended_frequency_plan/extended_plan_20260912"


def run(root):
    out = root / OUTPUT
    if (out / "report.json").exists():
        return read_report(root)
    config = json.loads((root / CONFIG).read_text())
    with inherited_locks([root / HEAVY, out]):
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=2 * 1024**2, projected_memory=256 * 1024**2)
        binding = InputBinding(root)
        head = code_binding(root, binding)
        if (
            head
            != subprocess.check_output(
                ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
            ).strip()
        ):
            raise DataValidationError("extended planner source must be pushed")
        verify_entries(root, config["inputs"])
        pilot = load_pilot(root)
        review = read_pilot_verification(root, pilot)
        old_plan = sealed_read(root / PILOT_OUTPUT / "plan.json")
        if (
            pilot["fingerprint"] != config["pilot_fingerprint"]
            or review["fingerprint"] != config["pilot_verification_fingerprint"]
        ):
            raise DataValidationError("extended planner pilot source changed")
        sessions = sealed_read(root / config["inventory_path"])["sessions"]
        rows = schedule(sessions, config["cutoff"])
        if (
            rows[0]["prediction_start"] != "2025-09-01"
            or rows[-1]["prediction_end"] != "2026-08-31"
        ):
            raise DataValidationError("extended fixed signal range changed")
        if rows[-1]["evaluation_label_cutoff"] != "2026-09-07":
            raise DataValidationError("extended label coverage changed")
        plan = atomic_seal(
            out / "plan.json",
            {
                "at": now(),
                "code_head": head,
                "code_files": binding.entries,
                "config_sha256": _sha(root / CONFIG),
                "inputs": config["inputs"],
                "schedule": rows,
                "new_fit_attempts": 0,
            },
        )
        metadata_root = root / config["metadata_root"]
        metadata = sealed_read(metadata_root / "metadata.json")
        daily, total, seen = None, 0, set()
        old_weeks = old_plan["config"]["weeks"]
        hashes = {row["train_end"]: hashlib.sha256() for row in old_weeks}
        with budget.watchdog():
            for item in metadata["counts"]:
                if not budget.can_start():
                    raise DataValidationError("extended planning segment exhausted")
                budget.check(projected_bytes=2 * 1024**2, projected_memory=128 * 1024**2)
                frame = pq.read_table(
                    metadata_root / f"metadata/{item['batch']:04d}.parquet", use_threads=False
                ).to_pandas()
                codes = set(frame.instrument_id)
                if codes & seen:
                    raise DataValidationError("duplicate extended partition codes")
                seen.update(codes)
                total += len(frame)
                part = daily_population(frame, sessions)
                daily = part if daily is None else daily.add(part, fill_value=0).astype("int64")
                for week in old_weeks:
                    update_membership(hashes[week["train_end"]], frame.loc[train_mask(frame, week)])
        if total != 9487149 or len(seen) != 5443:
            raise DataValidationError("extended metadata population changed")
        hashes = {key: value.hexdigest() for key, value in hashes.items()}
        rows = attach_populations(rows, daily, sessions)
        prep = {
            item["slot"]: sealed_read(
                root / PILOT_OUTPUT / "fits" / item["slot"] / "preprocessing.json"
            )
            for item in pilot["attempts"]
        }
        slots = fit_inventory(rows, config, old_plan, pilot, hashes, prep)
        anchors = list(dict.fromkeys(row["monthly_anchor"] for row in rows))
        if len(anchors) != 12:
            raise DataValidationError("extended monthly anchor coverage changed")
        payload = {
            "plan_fingerprint": plan["fingerprint"],
            "weeks": rows,
            "fit_slots": slots,
            "required_unique_models": len(slots),
            "reused_frozen_models": 6,
            "future_max_new_fit_attempts": len(slots) - 6,
            "future_max_attempts_per_segment": 6,
            "monthly_anchor_weeks": anchors,
            "signal_market_days": sum(len(row["prediction_sessions"]) for row in rows),
            "prediction_rows_per_policy_model": sum(row["prediction_rows"] for row in rows),
            "evaluation_rows_per_policy_model": sum(row["evaluation_rows"] for row in rows),
            "train_reuse_membership_sha256": hashes,
            "models": config["models"],
            "economic_specification": config["economic_specification"],
            "actual_new_fit_attempts": 0,
            "new_prediction_rows": 0,
        }
        weekly = atomic_seal(out / "schedule.json", payload)
        pd.DataFrame(
            [{k: v for k, v in row.items() if k != "prediction_sessions"} for row in rows]
        ).to_csv(out / "weeks.csv", index=False)
        binding.check()
        verify_entries(root, config["inputs"])
        return atomic_seal(
            out / "report.json",
            {
                "at": now(),
                "plan_fingerprint": plan["fingerprint"],
                "schedule_fingerprint": weekly["fingerprint"],
                "artifacts": {
                    name: {"sha256": _sha(out / name)} for name in ("schedule.json", "weeks.csv")
                },
                "metadata_rows": total,
                "metadata_codes": len(seen),
                "weekly_rows": len(rows),
                "monthly_anchors": len(anchors),
                "required_unique_models": len(slots),
                "reused_frozen_models": 6,
                "future_max_new_fit_attempts": len(slots) - 6,
                "new_fit_attempts": 0,
                "new_prediction_rows": 0,
                "provider_calls": 0,
                "execution_authority": False,
                "performance_evidence": False,
                "automatic_promotion": False,
                "history_already_observed": True,
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "generated_bytes": budget.check(),
                },
            },
        )


def read_report(root):
    out = root / OUTPUT
    if not (out / "report.json").exists():
        return None
    report, plan = sealed_read(out / "report.json"), sealed_read(out / "plan.json")
    if report["plan_fingerprint"] != plan["fingerprint"] or plan["config_sha256"] != _sha(
        root / CONFIG
    ):
        raise DataValidationError("extended planning identity changed")
    verify_historical_inputs(root, {"code_head": plan["code_head"], "inputs": plan["code_files"]})
    verify_entries(root, plan["inputs"])
    verify_entries(out, report["artifacts"])
    weekly = sealed_read(out / "schedule.json")
    if (
        weekly["fingerprint"] != report["schedule_fingerprint"]
        or weekly["plan_fingerprint"] != plan["fingerprint"]
    ):
        raise DataValidationError("extended schedule identity changed")
    for key in ("new_fit_attempts", "new_prediction_rows", "provider_calls"):
        if type(report.get(key)) is not int or report[key] != 0:
            raise DataValidationError("extended planner exceeded zero-action scope")
    if (
        any(
            report.get(key) is not False
            for key in ("execution_authority", "performance_evidence", "automatic_promotion")
        )
        or report.get("history_already_observed") is not True
    ):
        raise DataValidationError("extended planner authority changed")
    slots = weekly["fit_slots"]
    if (
        len({row["slot"] for row in slots}) != len(slots)
        or len(slots) != 2 * len(weekly["weeks"])
        or sum(row["reuse_slot"] is not None for row in slots) != 6
        or report["required_unique_models"] != len(slots)
        or report["future_max_new_fit_attempts"] != len(slots) - 6
        or weekly["future_max_new_fit_attempts"] != len(slots) - 6
        or weekly["actual_new_fit_attempts"] != 0
        or weekly["new_prediction_rows"] != 0
    ):
        raise DataValidationError("extended model budget changed")
    return report
