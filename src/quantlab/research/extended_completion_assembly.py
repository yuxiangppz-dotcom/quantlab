"""A new complete annual manifest; original failed and derived receipts stay immutable."""

import math

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal
from quantlab.research.extended_completion import load_report
from quantlab.research.extended_completion_protocol import (
    OUTPUT,
    PARENT,
    RECOVERY,
    SLOTS,
    validate_outputs,
    verify_context,
)
from quantlab.research.extended_frequency_metrics import policy_diagnostics
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.weekly_pilot_protocol import HEAVY, inherited_locks, now


def read_completion_proof(root, report, plan):
    path = root / OUTPUT / "independent_completion" / f"{report['fingerprint']}.json"
    if not path.exists():
        raise DataValidationError("four completed fits still require independent replay")
    proof = sealed_read(path)
    expected = {
        "source_head": plan["code_head"],
        "report_fingerprint": report["fingerprint"],
        "coverage": "completion_four",
        "verified_models": 4,
        "verified_new_models": 4,
        "verified_reused_models": 0,
        "prediction_rows": 60998,
        "metadata_rows": 9487149,
        "metadata_codes": 5443,
        "max_prediction_difference": 0.0,
        "additional_fit_attempts": 0,
        "new_prediction_artifacts": 0,
        "provider_calls": 0,
        "complete_annual_comparison": False,
        "history_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
    }
    if report["status"] != "complete" or any(
        type(proof.get(k)) is not type(v) or proof[k] != v for k, v in expected.items()
    ):
        raise DataValidationError("four-fit independent source/population/authority changed")
    counts = {s: plan["config"]["model_specs"][s]["prediction_rows"] for s in SLOTS}
    if proof.get("replicated_rows_by_slot") != counts:
        raise DataValidationError("four-fit independent coverage incomplete")
    windows = {s.rsplit("_", 1)[0] for s in SLOTS}
    checks = proof.get("scaler_checks", {})
    if set(checks) != windows:
        raise DataValidationError("four-fit training window verification incomplete")
    for week in windows:
        spec = plan["config"]["model_specs"][f"{week}_ridge"]
        if (
            checks[week]["rows"] != spec["train_rows"]
            or checks[week]["train_sha256"] != spec["train_sha256"]
        ):
            raise DataValidationError("four-fit mature training identity changed")
        for name in ("max_mean_difference", "max_scale_difference"):
            difference = checks[week].get(name)
            if (
                type(difference) is not float
                or not math.isfinite(difference)
                or not 0 <= difference < 1e-9
            ):
                raise DataValidationError("four-fit independent scaler differs")
    return proof


def references(config, parent, recovery, completion):
    if (
        parent["status"] != "failed"
        or recovery["status"] != "complete"
        or completion["status"] != "complete"
        or completion["global_actual_fit_invocations"] != 98
        or completion["global_consumed_fit_reservations"] != 98
    ):
        raise DataValidationError("complete assembly requires all original remaining models")
    selected = {}
    for origin, report, base, mode in [
        ("original", parent, PARENT, "fits"),
        ("recovery", recovery, RECOVERY, "reuse"),
        ("completion", completion, OUTPUT, "fits"),
    ]:
        for row in report["attempts"]:
            if row["status"] != "completed":
                continue
            slot = row["slot"]
            summary = row["summary"]
            if slot in selected or slot not in config["model_specs"] or row["mode"] != mode:
                raise DataValidationError("annual assembly duplicates or changes model references")
            if summary["prediction_rows"] != config["model_specs"][slot]["prediction_rows"]:
                raise DataValidationError("annual assembly prediction population changed")
            selected[slot] = {
                "slot": slot,
                "origin": origin,
                "folder": f"{base}/{mode}/{slot}",
                "summary_fingerprint": summary["fingerprint"],
                "saved_model_sha256": summary["saved_model_sha256"],
                "prediction_file_sha256": summary["prediction_file_sha256"],
                "prediction_rows": summary["prediction_rows"],
            }
    if (
        set(selected) != set(config["slot_order"])
        or len(selected) != 104
        or sum(v["prediction_rows"] for v in selected.values()) != 4320562
    ):
        raise DataValidationError("annual assembly is missing declared coverage")
    if {
        k: sum(v["origin"] == k for v in selected.values())
        for k in ("original", "recovery", "completion")
    } != {"original": 94, "recovery": 6, "completion": 4}:
        raise DataValidationError("annual assembly changed original/recovered/completed lineage")
    return {s: selected[s] for s in config["slot_order"]}


def assemble(root):
    out = root / OUTPUT
    with inherited_locks([root / HEAVY, out]):
        completion = load_report(root)
        plan = sealed_read(out / "plan.json")
        verify_context(root, plan)
        proof = read_completion_proof(root, completion, plan)
        folder = out / "combined"
        if (folder / "report.json").exists():
            return read_assembly(root)
        if folder.exists():
            raise DataValidationError(
                "consumed annual assembly requires explicit recovery; no overwrite"
            )
        parent = sealed_read(root / PARENT / "report.json")
        recovery = sealed_read(root / RECOVERY / "report.json")
        refs = references(plan["config"], parent, recovery, completion)
        atomic_seal(
            folder / "started.json",
            {
                "identity": plan["fingerprint"],
                "completion_report": completion["fingerprint"],
                "completion_proof": proof["fingerprint"],
                "started_at": now(),
            },
        )
        budget = Budget(out, plan["contract"])
        budget.check(projected_bytes=8 * 1024**2)
        with budget.watchdog():
            frames = {}
            for slot, item in refs.items():
                path = root / item["folder"] / "predictions.parquet"
                if _sha(path) != item["prediction_file_sha256"]:
                    raise DataValidationError("referenced prediction bytes changed")
                frames[slot] = pd.read_parquet(path, use_threads=False)
            daily, weekly, monthly, paired = policy_diagnostics(frames, plan["config"])
            if (len(daily), len(weekly), len(monthly), len(paired)) != (968, 208, 48, 80):
                raise DataValidationError("complete annual diagnostic population changed")
            daily_path = folder / "daily_policies.parquet"
            daily.to_parquet(daily_path, index=False)
            diagnostics = atomic_seal(
                folder / "diagnostics.json",
                {
                    "identity": plan["fingerprint"],
                    "weekly": weekly,
                    "monthly": monthly,
                    "paired": paired,
                    "daily_rows": len(daily),
                    "artifacts": {
                        daily_path.name: {
                            "sha256": _sha(daily_path),
                            "bytes": daily_path.stat().st_size,
                        }
                    },
                    "performance_evidence": False,
                    "execution_authority": False,
                },
            )
            verify_context(root, plan)
            validate_outputs(root, plan["contract"])
            if read_completion_proof(root, completion, plan) != proof:
                raise DataValidationError("four-fit proof changed during assembly")
            result = {
                "at": now(),
                "identity": plan["fingerprint"],
                "source_head": plan["code_head"],
                "status": "complete",
                "parent_status": "failed",
                "parent_report": parent["fingerprint"],
                "recovery_report": recovery["fingerprint"],
                "completion_report": completion["fingerprint"],
                "completion_proof": proof["fingerprint"],
                "references": refs,
                "global_consumed_fit_reservations": 98,
                "global_actual_fit_invocations": 98,
                "completed_new_models": 98,
                "completed_old_references": 6,
                "prediction_rows": 4320562,
                "diagnostics_fingerprint": diagnostics["fingerprint"],
                "diagnostic_counts": {"daily": 968, "weekly": 208, "monthly": 48, "paired": 80},
                "complete_annual_comparison": True,
                "independent_verification_complete": False,
                "history_already_observed": True,
                "historical_market_coverage_complete": False,
                "performance_evidence": False,
                "execution_authority": False,
                "new_fit_invocations_during_assembly": 0,
                "provider_calls": 0,
            }
            return atomic_seal(folder / "report.json", result)


def read_assembly(root):
    folder = root / OUTPUT / "combined"
    if not (folder / "report.json").exists():
        return None
    completion = load_report(root)
    plan = sealed_read(root / OUTPUT / "plan.json")
    proof = read_completion_proof(root, completion, plan)
    report = sealed_read(folder / "report.json")
    d = sealed_read(folder / "diagnostics.json")
    parent = sealed_read(root / PARENT / "report.json")
    recovery = sealed_read(root / RECOVERY / "report.json")
    refs = references(plan["config"], parent, recovery, completion)
    expected = {
        "identity": plan["fingerprint"],
        "source_head": plan["code_head"],
        "status": "complete",
        "parent_status": "failed",
        "parent_report": parent["fingerprint"],
        "recovery_report": recovery["fingerprint"],
        "completion_report": completion["fingerprint"],
        "completion_proof": proof["fingerprint"],
        "global_consumed_fit_reservations": 98,
        "global_actual_fit_invocations": 98,
        "completed_new_models": 98,
        "completed_old_references": 6,
        "prediction_rows": 4320562,
        "complete_annual_comparison": True,
        "independent_verification_complete": False,
        "history_already_observed": True,
        "historical_market_coverage_complete": False,
        "performance_evidence": False,
        "execution_authority": False,
        "new_fit_invocations_during_assembly": 0,
        "provider_calls": 0,
        "diagnostics_fingerprint": d["fingerprint"],
    }
    if (
        any(type(report.get(k)) is not type(v) or report[k] != v for k, v in expected.items())
        or report["references"] != refs
    ):
        raise DataValidationError("combined annual scope, source or budget changed")
    counts = {
        "daily": d["daily_rows"],
        "weekly": len(d["weekly"]),
        "monthly": len(d["monthly"]),
        "paired": len(d["paired"]),
    }
    if report["diagnostic_counts"] != counts or counts != {
        "daily": 968,
        "weekly": 208,
        "monthly": 48,
        "paired": 80,
    }:
        raise DataValidationError("combined annual diagnostic counts changed")
    if d["identity"] != plan["fingerprint"] or any(
        d[k] is not False for k in ("performance_evidence", "execution_authority")
    ):
        raise DataValidationError("combined diagnostic authority changed")
    verify_entries(folder, d["artifacts"])
    return report
