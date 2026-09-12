"""A failed parent cannot acquire fit authority through prediction-only recovery."""

import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research import extended_recovery as runner
from quantlab.research import extended_recovery_protocol as protocol
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_protocol import HEAVY, inherited_locks


def parent_fixture():
    report = dict(
        status="failed",
        fingerprint=protocol.PARENT_REPORT,
        cumulative_fit_attempts=94,
        actual_fit_invocations=94,
        completed_new_fits=94,
        completed_reuse_jobs=0,
        failed_or_interrupted_jobs=1,
        prediction_rows_completed=3944760,
        performance_evidence=False,
        execution_authority=False,
        attempts=[dict(slot="2026w32_ridge", mode="reuse", status="failed")],
    )
    original = dict(
        fingerprint=protocol.PARENT_PLAN,
        config=dict(reuse_slots=protocol.SLOTS, new_slots=[f"n{i}" for i in range(98)]),
        sources={"inputs": {}, "runtime": {}},
    )
    proof = dict(
        fingerprint=protocol.PARENT_PROOF,
        report_fingerprint=protocol.PARENT_REPORT,
        coverage="failed_prefix",
        verified_models=94,
        verified_new_models=94,
        verified_reused_models=0,
        prediction_rows=3944760,
        max_prediction_difference=0.0,
        additional_fit_attempts=0,
        new_prediction_artifacts=0,
        complete_annual_comparison=False,
        performance_evidence=False,
        execution_authority=False,
    )
    return report, original, proof


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("report", "status", "complete"),
        ("report", "cumulative_fit_attempts", 0),
        ("report", "actual_fit_invocations", 95),
        ("report", "completed_reuse_jobs", False),
        ("proof", "verified_models", 93),
        ("proof", "max_prediction_difference", 1e-12),
        ("proof", "complete_annual_comparison", True),
        ("proof", "additional_fit_attempts", False),
        ("report", "execution_authority", True),
        ("proof", "fingerprint", "changed"),
    ],
)
def test_failed_parent_budget_and_proof_are_exact(target, key, value):
    report, original, proof = parent_fixture()
    assert protocol.check_parent(report, original, proof) == protocol.BUDGET
    {"report": report, "proof": proof}[target][key] = value
    with pytest.raises(DataValidationError):
        protocol.check_parent(report, original, proof)


def test_parent_rejects_unreported_fit_reservation(tmp_path, monkeypatch):
    report, original, proof = parent_fixture()
    monkeypatch.setattr(protocol, "parent_status", lambda root: report)
    monkeypatch.setattr(
        protocol, "sealed_read", lambda path: original if path.name == "plan.json" else proof
    )
    base = tmp_path / protocol.PARENT / "fits"
    for slot in original["config"]["new_slots"][:95]:
        (base / slot).mkdir(parents=True)
    with pytest.raises(DataValidationError, match="unreported fit"):
        protocol.parent_evidence(tmp_path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("new_fit_authority", 1),
        ("new_fit_authority", False),
        ("global_fit_ceiling", 99),
        ("slots", list(reversed(protocol.SLOTS))),
    ],
)
def test_recovery_contract_cannot_refill_budget(tmp_path, key, value):
    c = json.loads((PROJECT_ROOT / protocol.CONFIG).read_text())
    c[key] = value
    path = tmp_path / protocol.CONFIG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(c))
    with pytest.raises(DataValidationError, match="contract changed"):
        protocol.contract(tmp_path)


@pytest.mark.parametrize("damage", ["added", "removed", "changed"])
def test_original_tree_is_read_only_and_exact(tmp_path, damage):
    path = tmp_path / "original"
    path.mkdir()
    (path / "model.bin").write_bytes(b"original")
    (path / "failed.json").write_text("failed")
    entries = protocol.immutable_tree(tmp_path, "original")
    protocol.verify_tree(tmp_path, "original", entries)
    if damage == "added":
        (path / "extra").touch()
    elif damage == "removed":
        (path / "model.bin").unlink()
    else:
        (path / "model.bin").write_bytes(b"changed!")
    with pytest.raises(DataValidationError):
        protocol.verify_tree(tmp_path, "original", entries)


@pytest.fixture
def coordinator(tmp_path, monkeypatch):
    # Coordinator fixtures never compute scores; keep the optional ML runtime out
    # of the default suite while exercising real locks and durable reservations.
    monkeypatch.setitem(
        sys.modules, "threadpoolctl", SimpleNamespace(threadpool_limits=lambda **k: nullcontext())
    )
    c = json.loads((PROJECT_ROOT / protocol.CONFIG).read_text())
    path = tmp_path / protocol.CONFIG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(c))
    out = tmp_path / protocol.OUTPUT
    out.mkdir(parents=True)
    plan = atomic_seal(out / "plan.json", {"contract": c, "code_head": "synthetic-only"})
    monkeypatch.setattr(runner, "prepare_plan", lambda root: plan)
    monkeypatch.setattr(runner, "verify_context", lambda *a, **k: None)
    monkeypatch.setattr(runner, "resource_preflight", lambda *a: None)
    monkeypatch.setattr(
        runner,
        "Budget",
        lambda *a: SimpleNamespace(
            check=lambda **k: None, can_start=lambda: True, watchdog=nullcontext
        ),
    )
    calls = []

    def derive(root, p, slot, budget):
        calls.append(slot)
        number = protocol.SLOTS.index(slot)
        total = (106617, 106617, 25375, 25375, 25410, 25410)[number]
        new = 30499 if number < 2 else 0
        return atomic_seal(
            out / "reuse" / slot / "worker_result.json",
            {
                "prediction_rows": total,
                "reused_prediction_rows": total - new,
                "newly_scored_rows": new,
            },
        )

    monkeypatch.setattr(runner, "derive_one", derive)
    monkeypatch.setattr(
        runner,
        "validated_job",
        lambda root, p, slot: sealed_read(out / "reuse" / slot / "worker_result.json"),
    )
    return tmp_path, plan, calls


def test_six_derived_jobs_are_separate_and_never_fit_or_rerun(coordinator):
    root, plan, calls = coordinator
    original = root / protocol.PARENT
    original.mkdir(parents=True)
    (original / "failed.json").write_text("terminal immutable failure")
    entries = protocol.immutable_tree(root, protocol.PARENT)
    result = runner.run(root)
    assert result["status"] == "complete" and calls == protocol.SLOTS
    assert result["prediction_rows"] == 314804 and result["reused_rows"] == 253806
    assert result["newly_scored_rows"] == 60998 and result["combined_model_references"] == 100
    assert result["additional_fit_attempts"] == 0 and result["carried_budget"] == protocol.BUDGET
    assert result["parent_status"] == "failed" and result["complete_annual_comparison"] is False
    assert runner.run(root) == result and calls == protocol.SLOTS
    protocol.verify_tree(root, protocol.PARENT, entries)
    assert not (root / protocol.OUTPUT / "fits").exists()


@pytest.mark.parametrize("failed", [False, True])
def test_consumed_derived_job_is_never_retried(coordinator, failed):
    root, plan, calls = coordinator
    book = protocol.ledger(root, plan)
    book.start(protocol.SLOTS[0])
    if failed:
        book.finish(protocol.SLOTS[0], "failed", error="synthetic error")
    result = runner.run(root)
    assert result["status"] == "failed" and result["consumed_recovery_jobs"] == 1
    assert result["completed_recovery_jobs"] == 0 and calls == []
    assert runner.run(root) == result and calls == []


def test_numerical_failure_retains_consumed_recovery_job(coordinator, monkeypatch):
    root, plan, calls = coordinator

    def error(*a):
        raise DataValidationError("old score differs")

    monkeypatch.setattr(runner, "derive_one", error)
    report = runner.run(root)
    assert report["status"] == "failed" and report["attempts"][0]["error"].endswith(
        "old score differs"
    )
    assert runner.run(root) == report
    assert [p.name for p in protocol.ledger(root, plan).base.iterdir()] == [protocol.SLOTS[0]]


@pytest.mark.parametrize("guard", ["verify_context", "resource_preflight"])
def test_preflight_failure_reserves_nothing(coordinator, monkeypatch, guard):
    root, plan, calls = coordinator

    def unavailable(*a, **k):
        raise DataValidationError("frozen input/resource changed")

    monkeypatch.setattr(runner, guard, unavailable)
    with pytest.raises(DataValidationError):
        runner.run(root)
    assert calls == [] and not list((root / protocol.OUTPUT / "reuse").glob("*/started.json"))


def test_shared_lock_prevents_parallel_recovery(coordinator):
    root, _, calls = coordinator
    with inherited_locks([root / HEAVY]):
        with pytest.raises(DataValidationError):
            runner.run(root)
    assert calls == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("status", "checkpoint"),
        ("parent_status", "complete"),
        ("prediction_rows", 314803),
        ("additional_fit_attempts", False),
        ("combined_model_references", 104),
        ("complete_annual_comparison", True),
        ("execution_authority", True),
        ("carried_budget", {**protocol.BUDGET, "recovery_fit_invocations": False}),
    ],
)
def test_saved_report_rejects_favorable_coverage_and_budget_changes(coordinator, key, value):
    root, _, _ = coordinator
    report = runner.run(root)
    report.pop("fingerprint")
    report[key] = value
    target = root / protocol.OUTPUT / "report.json"
    target.unlink()
    atomic_seal(target, report)
    with pytest.raises(DataValidationError):
        runner.load_report(root)


@pytest.mark.parametrize("damage", ["source", "runtime", "tree", "budget", "lineage"])
def test_context_rejects_changed_source_runtime_inventory_and_budget(tmp_path, monkeypatch, damage):
    report, original, proof = parent_fixture()
    c = {}
    plan = {
        "carried_budget": protocol.BUDGET.copy(),
        "config": original["config"],
        "contract": c,
        "schema": "test",
        "parent_plan": protocol.PARENT_PLAN,
        "parent_report": protocol.PARENT_REPORT,
        "parent_proof": protocol.PARENT_PROOF,
        "sources": original["sources"],
        "immutable_trees": {protocol.PARENT: {}, protocol.OLD: {}},
        "code_files": {"source": {}},
        "contract_sha256": "hash",
    }
    c["schema"] = "test"
    monkeypatch.setattr(
        protocol, "parent_evidence", lambda root: (original, report, proof, protocol.BUDGET)
    )
    monkeypatch.setattr(protocol, "contract", lambda root: c)
    monkeypatch.setattr(protocol, "_sha", lambda path: "hash")
    monkeypatch.setattr(protocol, "verify_tree", lambda *a: None)
    monkeypatch.setattr(
        protocol, "runtime_manifest", lambda: {} if damage != "runtime" else {"changed": True}
    )

    def verify(root, entries):
        if damage == "source" and entries == plan["code_files"]:
            raise DataValidationError("source drift")

    monkeypatch.setattr(protocol, "verify_entries", verify)
    if damage == "tree":
        plan["immutable_trees"].pop(protocol.OLD)
    if damage == "budget":
        plan["carried_budget"]["remaining_unconsumed_fits"] = 98
    if damage == "lineage":
        plan["parent_report"] = "changed"
    with pytest.raises(DataValidationError):
        protocol.verify_context(tmp_path, plan)


def test_derive_preserves_group_geometry_and_returns_sealed_result(tmp_path, monkeypatch):
    slot = protocol.SLOTS[0]
    spec = dict(
        slot=slot,
        reuse_slot="old",
        model="ridge",
        train_start="2020-01-01",
        train_end="2025-01-01",
        train_rows=1,
        train_sha256="training",
        replay_prediction_start="2025-01-02",
    )
    plan = {
        "fingerprint": "synthetic",
        "config": {"metadata_root": "metadata", "model_specs": {slot: spec}},
        "contract": {"batch_geometry": "two ordered groups", "max_generated_bytes": 1024**3},
    }
    atomic_seal(tmp_path / "metadata/metadata.json", {"counts": [{"batch": "batch-a"}]})
    folder = tmp_path / protocol.OUTPUT / "reuse" / slot
    folder.mkdir(parents=True)
    atomic_seal(folder / "started.json", {"identity": "synthetic", "slot": slot, "mode": "reuse"})
    model = SimpleNamespace()  # Deliberately has no fit method.
    monkeypatch.setattr(runner, "load_reuse", lambda *a: (model, None, {"new_fit_invocations": 0}))
    part = pd.DataFrame(
        {"instrument_id": ["B", "A", "C"], "trade_date": pd.to_datetime(["2025-01-02"] * 3)}
    )
    x = np.arange(6, dtype="float32").reshape(3, 2)

    def predict(*a, batch_observer):
        batch_observer(0, part, np.array([True, False, True]), x)
        return {"prediction_rows": 3, "reused_prediction_rows": 2, "newly_scored_rows": 1}

    monkeypatch.setattr(runner, "predict_union", predict)
    summary = runner.derive_one(tmp_path, plan, slot, SimpleNamespace(check=lambda **k: None))
    assert summary == sealed_read(folder / "worker_result.json")
    group = sealed_read(folder / "groups.json")["partitions"][0]
    assert group["existing"]["rows"] == 2 and group["new"]["rows"] == 1
    assert group["existing"]["identity_sha256"] == runner.membership(part.loc[[0, 2]])
    assert group["existing"]["identity_sha256"] != runner.membership(part.loc[[2, 0]])
    assert (
        group["existing"]["float32_feature_sha256"]
        == runner.hashlib.sha256(x[[0, 2]].tobytes()).hexdigest()
    )
    assert summary["new_fit_invocations"] == 0 and not (folder / "model.pkl").exists()
    with pytest.raises(DataValidationError, match="cannot run again"):
        runner.derive_one(tmp_path, plan, slot, SimpleNamespace(check=lambda **k: None))
