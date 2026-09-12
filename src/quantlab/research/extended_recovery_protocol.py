"""A separate prediction-only recovery; the failed parent's fit ledger stays immutable."""

from __future__ import annotations

import json
import subprocess

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import resource_preflight
from quantlab.research.alpha158_rolling_protocol import artifact_entries, runtime_manifest
from quantlab.research.alpha158_store import atomic_seal, directory_bytes
from quantlab.research.extended_frequency import load_status as parent_status
from quantlab.research.extended_frequency_protocol import OLD, Ledger
from quantlab.research.extended_frequency_protocol import OUTPUT as PARENT
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs
from quantlab.research.weekly_pilot_protocol import now

CONFIG = "config/alpha158_extended_recovery_v1.json"
OUTPUT = "data/products/alpha158_extended_recovery/alpha158_extended_recovery_20260912"
RUNTIME = "data/runtime/research/alpha158_extended_recovery_20260912"
PARENT_REPORT = "1522e7b2bc40b33e2031901759ab09033b4df585657f46a12375be3b0de14071"
PARENT_PLAN = "cfe72f5dec112918cc1ca325b4d950d883e6999a19486566065264438b253c45"
PARENT_PROOF = "127b68cae322a0f857ab87a9d6f0cd22d3da5358b4195dddce6b482301d1e6f8"
SLOTS = [f"2026w{week}_{kind}" for week in (32, 33, 34) for kind in ("ridge", "lightgbm")]
BUDGET = {
    "global_fit_ceiling": 98,
    "inherited_consumed_fits": 94,
    "recovery_fit_invocations": 0,
    "remaining_unconsumed_fits": 4,
}


def contract(root):
    c = json.loads((root / CONFIG).read_text())
    expected = {
        "schema": "alpha158_extended_recovery_v1",
        "experiment_id": "alpha158_extended_recovery_20260912",
        "parent_plan": PARENT_PLAN,
        "parent_report": PARENT_REPORT,
        "parent_proof": PARENT_PROOF,
        "repair_head": "ea30299e8ff34c14c0010071ad1fdfbda790636e",
        "slots": SLOTS,
        "prediction_rows": 314804,
        "reused_rows": 253806,
        "newly_scored_rows": 60998,
        "global_fit_ceiling": 98,
        "inherited_consumed_fits": 94,
        "new_fit_authority": 0,
        "batch_geometry": "metadata_partition_then_existing_and_new_groups_in_original_order",
        "max_jobs_per_segment": 6,
        "max_generated_bytes": 1024**3,
        "max_rss_bytes": 8 * 1024**3,
        "minimum_available_memory_bytes": 10 * 1024**3,
        "reserve_host_D_bytes": 8 * 1024**3,
        "max_wakeup_seconds": 2700,
        "next_partition_time_reserve_seconds": 420,
        "threads": 2,
    }
    if c != expected or any(type(c[k]) is not type(v) for k, v in expected.items()):
        raise DataValidationError("derived recovery contract changed")
    return c


def check_parent(report, original, proof):
    expected = {
        "status": "failed",
        "fingerprint": PARENT_REPORT,
        "cumulative_fit_attempts": 94,
        "actual_fit_invocations": 94,
        "completed_new_fits": 94,
        "completed_reuse_jobs": 0,
        "failed_or_interrupted_jobs": 1,
        "prediction_rows_completed": 3944760,
    }
    if any(type(report.get(k)) is not type(v) or report[k] != v for k, v in expected.items()):
        raise DataValidationError("failed parent status or carried fit budget changed")
    if original["fingerprint"] != PARENT_PLAN or original["config"]["reuse_slots"] != SLOTS:
        raise DataValidationError("recovery parent plan/slots changed")
    failed = [r for r in report["attempts"] if r["status"] != "completed"]
    if [(r["slot"], r["mode"], r["status"]) for r in failed] != [
        ("2026w32_ridge", "reuse", "failed")
    ]:
        raise DataValidationError("original failed reuse identity changed")
    expected_proof = {
        "fingerprint": PARENT_PROOF,
        "report_fingerprint": PARENT_REPORT,
        "coverage": "failed_prefix",
        "verified_models": 94,
        "verified_new_models": 94,
        "verified_reused_models": 0,
        "prediction_rows": 3944760,
        "max_prediction_difference": 0.0,
        "additional_fit_attempts": 0,
        "new_prediction_artifacts": 0,
        "complete_annual_comparison": False,
    }
    if any(type(proof.get(k)) is not type(v) or proof[k] != v for k, v in expected_proof.items()):
        raise DataValidationError("recovery inherited independent proof changed")
    for value in (report, proof):
        if (
            value.get("performance_evidence") is not False
            or value.get("execution_authority") is not False
        ):
            raise DataValidationError("recovery parent gained financial authority")
    return BUDGET.copy()


def parent_evidence(root):
    report = parent_status(root)
    original = sealed_read(root / PARENT / "plan.json")
    proof = sealed_read(root / PARENT / "independent_failed_prefix" / f"{PARENT_REPORT}.json")
    budget = check_parent(report, original, proof)
    c = original["config"]
    actual = {p.name for p in (root / PARENT / "fits").iterdir()}
    if actual != set(c["new_slots"][:94]):
        raise DataValidationError("parent gained an unreported fit reservation")
    if {p.name for p in (root / PARENT / "reuse").iterdir()} != {"2026w32_ridge"}:
        raise DataValidationError("parent gained a reuse attempt after terminal failure")
    # Read durable intents, not just the old report's counter projection.
    ledger = Ledger(root / PARENT, PARENT_PLAN, c["new_slots"], "fits")
    for slot in c["new_slots"][:94]:
        if (
            ledger.read(slot)["status"] != "completed"
            or not (root / PARENT / "fits" / slot / "invoked.json").exists()
        ):
            raise DataValidationError("parent durable fit evidence changed")
    return original, report, proof, budget


def immutable_tree(root, relative):
    return artifact_entries(root / relative, exclude=("worker.lock",))


def verify_tree(root, relative, entries):
    actual = {
        p.relative_to(root / relative).as_posix()
        for p in (root / relative).rglob("*")
        if p.is_file() and p.name != "worker.lock"
    }
    if actual != set(entries):
        raise DataValidationError("original evidence tree gained or lost files")
    verify_entries(root / relative, entries)


def check_carried_budget(value):
    if value != BUDGET or any(type(value.get(k)) is not int for k in BUDGET):
        raise DataValidationError("derived recovery replenished original fit budget")


def verify_context(root, plan, *, historical=False):
    original, _, _, budget = parent_evidence(root)
    check_carried_budget(plan["carried_budget"])
    if budget != plan["carried_budget"] or original["config"] != plan["config"]:
        raise DataValidationError("derived recovery changed inherited budget or model spec")
    if (
        plan["contract"] != contract(root)
        or plan["schema"] != plan["contract"]["schema"]
        or plan["parent_plan"] != PARENT_PLAN
        or plan["parent_report"] != PARENT_REPORT
        or plan["parent_proof"] != PARENT_PROOF
        or plan["sources"] != original["sources"]
        or set(plan["immutable_trees"]) != {PARENT, OLD}
    ):
        raise DataValidationError("derived recovery lineage/inventory changed")
    if _sha(root / CONFIG) != plan["contract_sha256"]:
        raise DataValidationError("derived recovery config bytes changed")
    for name, entries in plan["immutable_trees"].items():
        verify_tree(root, name, entries)
    verify_entries(root, plan["sources"]["inputs"])
    if historical:
        verify_historical_inputs(
            root, {"code_head": plan["code_head"], "inputs": plan["code_files"]}
        )
    else:
        verify_entries(root, plan["code_files"])
        if runtime_manifest() != plan["sources"]["runtime"]:
            raise DataValidationError("derived recovery runtime changed")


def prepare_plan(root):
    c = contract(root)
    original, report, proof, budget = parent_evidence(root)
    resource_preflight(c)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != upstream:
        raise DataValidationError("derived recovery source must be pushed")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", c["repair_head"], head], cwd=root, check=False
    ).returncode:
        raise DataValidationError("derived recovery source excludes the declared repair")
    if runtime_manifest() != original["sources"]["runtime"]:
        raise DataValidationError("derived recovery must use the original model runtime")
    payload = {
        "schema": c["schema"],
        "contract": c,
        "contract_sha256": _sha(root / CONFIG),
        "code_head": head,
        "code_files": binding.entries,
        "config": original["config"],
        "sources": original["sources"],
        "parent_plan": original["fingerprint"],
        "parent_report": report["fingerprint"],
        "parent_proof": proof["fingerprint"],
        "carried_budget": budget,
        "immutable_trees": {name: immutable_tree(root, name) for name in (PARENT, OLD)},
    }
    out = root / OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    path = out / "plan.json"
    if path.exists():
        plan = sealed_read(path)
        if {k: v for k, v in plan.items() if k != "fingerprint"} != payload:
            raise DataValidationError("derived recovery resume requires its exact frozen source")
        return plan
    return atomic_seal(path, payload)


def generated_bytes(root):
    return directory_bytes(root / OUTPUT) + directory_bytes(root / RUNTIME)


def check_outputs(root, plan):
    if generated_bytes(root) > plan["contract"]["max_generated_bytes"]:
        raise DataValidationError("derived recovery cumulative output budget exceeded")
    if (
        (root / OUTPUT / "fits").exists()
        or list((root / OUTPUT).rglob("invoked.json"))
        or list((root / OUTPUT).rglob("*.pkl"))
    ):
        raise DataValidationError("prediction-only recovery gained model fitting artifacts")


def ledger(root, plan):
    return Ledger(root / OUTPUT, plan["fingerprint"], plan["contract"]["slots"], "reuse")


def publish(root, plan, started):
    book = ledger(root, plan)
    values = [v for slot in SLOTS if (v := book.read(slot)) is not None]
    completed = [r for r in values if r["status"] == "completed"]
    failed = any(r["status"] != "completed" for r in values)
    status = "failed" if failed else "complete" if len(completed) == 6 else "checkpoint"
    summary = {
        "at": now(),
        "source_head": plan["code_head"],
        "identity": plan["fingerprint"],
        "status": status,
        "parent_status": "failed",
        "parent_report": PARENT_REPORT,
        "carried_budget": BUDGET,
        "attempts": values,
        "completed_recovery_jobs": len(completed),
        "consumed_recovery_jobs": len(values),
        "prediction_rows": sum(r["summary"]["prediction_rows"] for r in completed),
        "reused_rows": sum(r["summary"]["reused_prediction_rows"] for r in completed),
        "newly_scored_rows": sum(r["summary"]["newly_scored_rows"] for r in completed),
        "combined_model_references": 94 + len(completed),
        "required_model_references": 104,
        "additional_fit_attempts": 0,
        "provider_calls": 0,
        "performance_evidence": False,
        "execution_authority": False,
        "complete_annual_comparison": False,
        "history_already_observed": True,
        "segment_started_at": started,
    }
    path = (
        root
        / OUTPUT
        / (
            "report.json"
            if status in ("failed", "complete")
            else f"checkpoints/{len(values):02d}.json"
        )
    )
    if path.exists():
        previous = sealed_read(path)
        ignored = {"fingerprint", "at", "segment_started_at"}
        if {k: v for k, v in previous.items() if k not in ignored} != {
            k: v for k, v in summary.items() if k not in ignored
        }:
            raise DataValidationError("sealed recovery checkpoint differs from durable evidence")
        return previous
    return atomic_seal(path, summary)
