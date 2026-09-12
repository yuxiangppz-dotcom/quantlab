"""Original global budget95--98 survives crashes, fake successes and parent drift."""

import copy
import json
from types import SimpleNamespace

import pytest

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research import extended_completion as runner
from quantlab.research import extended_completion_protocol as protocol
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_protocol import HEAVY, inherited_locks, invoke_once


@pytest.fixture
def coordinator(tmp_path, monkeypatch):
    out = tmp_path / protocol.OUTPUT
    out.mkdir(parents=True)
    config = json.loads((PROJECT_ROOT / "config/alpha158_extended_frequency_v1.json").read_text())
    plan = atomic_seal(
        out / "plan.json",
        {"contract": protocol.LIMITS, "config": config, "code_head": "synthetic-only"},
    )
    monkeypatch.setattr(runner, "prepare_plan", lambda root: plan)
    monkeypatch.setattr(runner, "verify_context", lambda *a, **k: None)
    monkeypatch.setattr(runner, "resource_preflight", lambda *a: None)
    monkeypatch.setattr(
        runner, "Budget", lambda *a: SimpleNamespace(can_start=lambda: True, check=lambda **k: None)
    )
    monkeypatch.setattr(
        runner,
        "validated_job",
        lambda root, p, slot: sealed_read(runner.folder_for(root, slot) / "worker_result.json"),
    )
    calls = []

    def spawn(command, **kwargs):
        assert len(kwargs["pass_fds"]) == 2 and kwargs["start_new_session"] is True
        slot = command[command.index("--worker") + 1]
        folder = runner.folder_for(tmp_path, slot)
        protocol.verify_locks(tmp_path, kwargs["pass_fds"])
        invoke_once(folder, plan["fingerprint"], slot, lambda: calls.append(slot))
        atomic_seal(
            folder / "worker_result.json",
            {"prediction_rows": config["model_specs"][slot]["prediction_rows"]},
        )
        return object()

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        runner,
        "monitor",
        lambda *a: {"returncode": 0, "violation": None, "combined_peak_rss_bytes": 1000},
    )
    return tmp_path, plan, calls


def test_remaining_four_jobs_use_global95_to98_and_immutable_separate_paths(coordinator):
    root, plan, calls = coordinator
    parents = []
    for name in (protocol.PARENT, protocol.OLD, protocol.RECOVERY):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "old.bin").write_bytes(b"original")
        parents.append((name, protocol.immutable_tree(root, name)))
    result = runner.run(root)
    assert result["status"] == "complete" and calls == protocol.SLOTS
    assert (
        result["completion_fit_invocations"] == 4 and result["global_actual_fit_invocations"] == 98
    )
    assert (
        result["global_consumed_fit_reservations"] == 98
        and result["global_completed_new_models"] == 98
    )
    assert (
        result["combined_model_references"] == 104 and result["combined_prediction_rows"] == 4320562
    )
    assert result["parent_status"] == "failed" and result["complete_annual_comparison"] is False
    assert [
        sealed_read(runner.folder_for(root, s) / "started.json")["global_attempt_number"]
        for s in protocol.SLOTS
    ] == [95, 96, 97, 98]
    assert runner.run(root) == result and calls == protocol.SLOTS
    for name, entries in parents:
        protocol.verify_tree(root, name, entries)
    with pytest.raises(DataValidationError):
        protocol.CarryLedger(root / protocol.OUTPUT, plan["fingerprint"]).start("extra")


@pytest.mark.parametrize("invoked", [False, True])
def test_interrupted_slot_consumes_global_budget_and_cannot_refit(coordinator, invoked):
    root, plan, calls = coordinator
    book = protocol.CarryLedger(root / protocol.OUTPUT, plan["fingerprint"])
    folder = book.start(protocol.SLOTS[0])
    if invoked:
        invoke_once(folder, plan["fingerprint"], protocol.SLOTS[0], lambda: calls.append("first"))
    result = runner.run(root)
    assert result["status"] == "failed" and result["global_consumed_fit_reservations"] == 95
    assert (
        result["global_actual_fit_invocations"] == 94 + int(invoked)
        and result["remaining_unconsumed_fit_slots"] == 3
    )
    assert result["completed_completion_models"] == 0
    assert runner.run(root) == result and calls == (["first"] if invoked else [])


def test_child_failure_stops_later_original_slots(coordinator, monkeypatch):
    root, _, calls = coordinator
    monkeypatch.setattr(
        runner,
        "monitor",
        lambda *a: {
            "returncode": 73,
            "violation": "combined_rss",
            "combined_peak_rss_bytes": 9 * 1024**3,
        },
    )
    report = runner.run(root)
    assert report["status"] == "failed" and calls == protocol.SLOTS[:1]
    assert (
        report["global_actual_fit_invocations"] == 95
        and report["global_consumed_fit_reservations"] == 95
    )
    assert runner.run(root) == report and calls == protocol.SLOTS[:1]


@pytest.mark.parametrize("guard", ["resource_preflight", "verify_context"])
def test_failed_preflight_reserves_no_remaining_model(coordinator, monkeypatch, guard):
    root, _, calls = coordinator

    def error(*a, **k):
        raise DataValidationError("frozen inputs/resources changed")

    monkeypatch.setattr(runner, guard, error)
    with pytest.raises(DataValidationError):
        runner.run(root)
    assert calls == [] and not list((root / protocol.OUTPUT / "fits").glob("*/started.json"))


def test_shared_lock_and_unrelated_lock_identity_block_workers(coordinator):
    root, _, calls = coordinator
    with inherited_locks([root / HEAVY]):
        with pytest.raises(DataValidationError):
            runner.run(root)
    with inherited_locks([root / HEAVY, root / "unrelated"]) as descriptors:
        with pytest.raises((DataValidationError, FileNotFoundError)):
            protocol.verify_locks(root, descriptors)
    assert calls == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("global_actual_fit_invocations", 4),
        ("global_consumed_fit_reservations", 4),
        ("global_fit_ceiling", 102),
        ("remaining_unconsumed_fit_slots", 4),
        ("recovery_fit_invocations", False),
        ("combined_prediction_rows", 4320563),
        ("parent_status", "complete"),
        ("complete_annual_comparison", True),
        ("execution_authority", True),
        ("performance_evidence", True),
    ],
)
def test_report_rejects_replenished_budget_or_premature_annual_success(coordinator, key, value):
    root, _, _ = coordinator
    report = runner.run(root)
    report.pop("fingerprint")
    report[key] = value
    path = root / protocol.OUTPUT / "report.json"
    path.unlink()
    atomic_seal(path, report)
    with pytest.raises(DataValidationError):
        runner.load_report(root)


@pytest.mark.parametrize(
    "key,value",
    [
        ("global_attempt_number", 1),
        ("global_fit_ceiling", 102),
        ("inherited_consumed_fits", 0),
        ("parent_plan", "new"),
    ],
)
def test_durable_reservation_cannot_reset_original_budget(coordinator, key, value):
    root, plan, _ = coordinator
    book = protocol.CarryLedger(root / protocol.OUTPUT, plan["fingerprint"])
    folder = book.start(protocol.SLOTS[0])
    path = folder / "started.json"
    start = sealed_read(path)
    start.pop("fingerprint")
    start[key] = value
    path.unlink()
    atomic_seal(path, start)
    with pytest.raises(DataValidationError):
        book.read(protocol.SLOTS[0])


def test_failed_terminal_cannot_hide_a_later_directory(coordinator):
    root, plan, _ = coordinator
    book = protocol.CarryLedger(root / protocol.OUTPUT, plan["fingerprint"])
    book.start(protocol.SLOTS[0])
    assert runner.run(root)["status"] == "failed"
    (book.base / protocol.SLOTS[1]).mkdir()
    with pytest.raises(DataValidationError, match="hides a reservation"):
        runner.load_report(root)


def test_invocation_requires_the_same_reserved_slot_and_actual_time(coordinator):
    root, plan, _ = coordinator
    book = protocol.CarryLedger(root / protocol.OUTPUT, plan["fingerprint"])
    slot = protocol.SLOTS[0]
    folder = book.start(slot)
    atomic_seal(
        folder / "invoked.json",
        {"identity": plan["fingerprint"], "slot": slot, "at": "2000-01-01T00:00:00+00:00"},
    )
    with pytest.raises(DataValidationError, match="invocation"):
        runner.publish(root, plan, "unused")


@pytest.mark.parametrize(
    "key,value",
    [
        ("remaining_slot_authority", 98),
        ("global_attempt_numbers", [1, 2, 3, 4]),
        ("inherited_consumed_fits", 0),
        ("slots", list(reversed(protocol.SLOTS))),
    ],
)
def test_frozen_contract_is_not_a_new_training_budget(tmp_path, key, value):
    c = copy.deepcopy(protocol.LIMITS)
    c[key] = value
    path = tmp_path / protocol.CONFIG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(c))
    with pytest.raises(DataValidationError):
        protocol.contract(tmp_path)


def test_incomplete_or_changed_recovery_cannot_authorize_four_new_fits(tmp_path, monkeypatch):
    monkeypatch.setattr(protocol, "recovery_report", lambda root: {"fingerprint": "different"})
    monkeypatch.setattr(protocol, "read_verification", lambda *a: None)
    with pytest.raises(DataValidationError, match="independently closed"):
        protocol.basis(tmp_path)


@pytest.mark.parametrize("damage", ["source", "runtime", "original_model", "scope"])
def test_completion_context_rejects_source_runtime_and_original_drift(
    tmp_path, monkeypatch, damage
):
    old = {"config": {}, "sources": {"runtime": {}}}
    c = copy.deepcopy(protocol.LIMITS)
    p = {
        "contract": c,
        "contract_sha256": "hash",
        "config": {},
        "sources": old["sources"],
        "schema": c["schema"],
        "parent_plan": protocol.PARENT_PLAN,
        "parent_report": protocol.PARENT_REPORT,
        "recovery_report": protocol.RECOVERY_REPORT,
        "recovery_proof": protocol.RECOVERY_PROOF,
        "immutable_trees": {n: {} for n in (protocol.PARENT, protocol.OLD, protocol.RECOVERY)},
        "code_files": {},
    }
    monkeypatch.setattr(protocol, "basis", lambda root: old)
    monkeypatch.setattr(protocol, "contract", lambda root: c)
    monkeypatch.setattr(protocol, "_sha", lambda path: "hash")

    def verify_tree(*a):
        if damage == "original_model":
            raise DataValidationError("original tree changed")

    def verify_entries(*a):
        if damage == "source":
            raise DataValidationError("bound source changed")

    monkeypatch.setattr(protocol, "verify_tree", verify_tree)
    monkeypatch.setattr(protocol, "verify_entries", verify_entries)
    monkeypatch.setattr(
        protocol, "runtime_manifest", lambda: {"changed": True} if damage == "runtime" else {}
    )
    if damage == "scope":
        p["config"] = {"new_search": True}
    with pytest.raises(DataValidationError):
        protocol.verify_context(tmp_path, p)


def test_annual_reference_assembly_requires_all_three_exact_sources():
    from quantlab.research.extended_completion_assembly import references

    c = json.loads((PROJECT_ROOT / "config/alpha158_extended_frequency_v1.json").read_text())
    parent = {"status": "failed", "attempts": []}
    recovery = {"status": "complete", "attempts": []}
    completion = {
        "status": "complete",
        "global_actual_fit_invocations": 98,
        "global_consumed_fit_reservations": 98,
        "attempts": [],
    }
    for slot in c["slot_order"]:
        report, mode = (
            (completion, "fits")
            if slot in protocol.SLOTS
            else (recovery, "reuse")
            if slot in c["reuse_slots"]
            else (parent, "fits")
        )
        report["attempts"].append(
            {
                "slot": slot,
                "mode": mode,
                "status": "completed",
                "summary": {
                    "fingerprint": slot,
                    "prediction_rows": c["model_specs"][slot]["prediction_rows"],
                    "saved_model_sha256": "synthetic",
                    "prediction_file_sha256": "synthetic",
                },
            }
        )
    parent["attempts"].append({"slot": "2026w32_ridge", "mode": "reuse", "status": "failed"})
    result = references(c, parent, recovery, completion)
    assert len(result) == 104 and sum(v["prediction_rows"] for v in result.values()) == 4320562
    assert (
        result["2026w32_ridge"]["origin"] == "recovery"
        and result["2026w35_ridge"]["origin"] == "completion"
    )
    for damage in ("missing", "duplicate", "unfinished", "counter", "population"):
        p, r, k = copy.deepcopy((parent, recovery, completion))
        if damage == "missing":
            k["attempts"].pop()
        elif damage == "duplicate":
            p["attempts"].append(k["attempts"][0])
        elif damage == "unfinished":
            k["status"] = "checkpoint"
        elif damage == "counter":
            k["global_actual_fit_invocations"] = 4
        else:
            k["attempts"][0]["summary"]["prediction_rows"] += 1
        with pytest.raises(DataValidationError):
            references(c, p, r, k)


def test_qlib_copy_is_allowed_only_when_identical_and_within_the_single_run(tmp_path):
    root = tmp_path / protocol.OUTPUT / "fits" / protocol.SLOTS[0]
    root.mkdir(parents=True)
    (root / "model.pkl").write_bytes(b"original model")
    copy = root / "qlib/mlruns/123/00000000000000000000000000000000/artifacts/model.pkl"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"original model")
    protocol.validate_outputs(tmp_path, protocol.LIMITS)
    copy.write_bytes(b"different model")
    with pytest.raises(DataValidationError, match="undeclared model"):
        protocol.validate_outputs(tmp_path, protocol.LIMITS)


def test_annual_assembly_waits_for_independent_four_fit_proof(coordinator):
    from quantlab.research.extended_completion_assembly import read_completion_proof

    root, plan, _ = coordinator
    report = runner.run(root)
    with pytest.raises(DataValidationError, match="require independent"):
        read_completion_proof(root, report, plan)
    values = {
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
        "replicated_rows_by_slot": {
            s: plan["config"]["model_specs"][s]["prediction_rows"] for s in protocol.SLOTS
        },
        "scaler_checks": {},
    }
    for week in ("2026w35", "2026w36"):
        spec = plan["config"]["model_specs"][week + "_ridge"]
        values["scaler_checks"][week] = {
            "rows": spec["train_rows"],
            "train_sha256": spec["train_sha256"],
            "max_mean_difference": 0.0,
            "max_scale_difference": 0.0,
        }
    path = root / protocol.OUTPUT / "independent_completion" / f"{report['fingerprint']}.json"
    proof = atomic_seal(path, values)
    assert read_completion_proof(root, report, plan) == proof
    for damage in ("fit", "score", "train", "source"):
        changed = copy.deepcopy(values)
        if damage == "fit":
            changed["additional_fit_attempts"] = 1
        elif damage == "score":
            changed["max_prediction_difference"] = 1e-12
        elif damage == "train":
            changed["scaler_checks"]["2026w35"]["max_mean_difference"] = 0.1
        else:
            changed["source_head"] = "different"
        path.unlink()
        atomic_seal(path, changed)
        with pytest.raises(DataValidationError):
            read_completion_proof(root, report, plan)
