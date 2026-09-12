import copy
import json
import subprocess
import sys
import time

import pytest

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.extended_frequency_protocol import (
    CONFIG,
    HEAVY,
    OUTPUT,
    SOURCES,
    Ledger,
    monitor,
    prediction_union,
    validate_contract,
    verify_locks,
)
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_protocol import inherited_locks, invoke_once


def config():
    return json.loads((PROJECT_ROOT / CONFIG).read_text())


def test_98_cumulative_invocations_and_six_reuses_never_reset_on_resume(tmp_path):
    c = config()
    ledger = Ledger(tmp_path, "source", c["new_slots"], "fits")
    calls = []
    for slot in c["new_slots"]:
        folder = ledger.start(slot)
        invoke_once(folder, "source", slot, lambda slot=slot: calls.append(slot))
        ledger.finish(slot, "completed")
    resumed = Ledger(tmp_path, "source", c["new_slots"], "fits")
    assert len(calls) == 98
    with pytest.raises(DataValidationError, match="cannot be retried"):
        resumed.start(c["new_slots"][0])
    with pytest.raises(DataValidationError, match="outside"):
        resumed.start("99th")
    reuse = Ledger(tmp_path, "source", c["reuse_slots"], "reuse")
    for slot in c["reuse_slots"]:
        reuse.start(slot)
        reuse.finish(slot, "completed")
    assert len(calls) == 98
    assert not list((tmp_path / "reuse").rglob("invoked.json"))


def test_abandoned_or_failed_slot_is_consumed_and_cannot_be_skipped(tmp_path):
    c = config()
    ledger = Ledger(tmp_path, "source", c["new_slots"], "fits")
    first, second = c["new_slots"][:2]
    folder = ledger.start(first)
    with pytest.raises(RuntimeError):
        invoke_once(folder, "source", first, lambda: (_ for _ in ()).throw(RuntimeError("crash")))
    resumed = Ledger(tmp_path, "source", c["new_slots"], "fits")
    assert resumed.read(first)["status"] == "interrupted"
    assert sealed_read(folder / "invoked.json")["slot"] == first
    with pytest.raises(DataValidationError, match="retried"):
        resumed.start(first)
    with pytest.raises(DataValidationError, match="no skipping"):
        resumed.start(second)
    resumed.finish(first, "failed")
    with pytest.raises(DataValidationError, match="no skipping"):
        resumed.start(second)


def test_unknown_skipped_or_wrong_budget_folders_are_rejected(tmp_path):
    c = config()
    (tmp_path / "fits").mkdir()
    (tmp_path / "fits" / c["new_slots"][1]).mkdir()
    with pytest.raises(DataValidationError, match="skipped"):
        Ledger(tmp_path, "source", c["new_slots"], "fits")
    with pytest.raises(DataValidationError, match="98 new"):
        Ledger(tmp_path, "source", c["new_slots"] + ["extra"], "fits")


def test_reuse_may_not_hide_a_fit_invocation(tmp_path):
    c = config()
    ledger = Ledger(tmp_path, "source", c["reuse_slots"], "reuse")
    slot = c["reuse_slots"][0]
    folder = ledger.start(slot)
    invoke_once(folder, "source", slot, lambda: None)
    with pytest.raises(DataValidationError, match="gained a fit"):
        ledger.finish(slot, "completed")


def test_monthly_union_preserves_holidays_partial_week_and_shared_own_week_once():
    c = config()
    first_august = next(row for row in c["weeks"] if row["prediction_start"] == "2026-08-03")
    dates, rows = prediction_union(c["weeks"], first_august["week_id"])
    assert dates[0] == "2026-08-03" and dates[-1] == "2026-08-31"
    assert len(dates) == len(set(dates)) == 21
    assert rows == sum(row["prediction_rows"] for row in c["weeks"] if row["month"] == "2026-08")
    last = c["weeks"][-1]
    assert prediction_union(c["weeks"], last["week_id"])[0] == ["2026-08-31"]
    assert sum(s["prediction_rows"] for s in c["model_specs"].values()) == 4320562
    for week in c["weeks"]:
        left, right = [c["model_specs"][f"{week['week_id']}_{k}"] for k in ("ridge", "lightgbm")]
        assert left["train_sha256"] == right["train_sha256"]
        assert left["prediction_sha256"] == right["prediction_sha256"]


def contract_fixture():
    c = config()
    sources = json.loads((PROJECT_ROOT / SOURCES).read_text())
    keys = (
        "slot",
        "week_id",
        "model",
        "train_start",
        "train_end",
        "train_rows",
        "reuse_slot",
        "model_sha256",
        "source_head",
    )
    p = {
        "weeks": copy.deepcopy(c["weeks"]),
        "models": copy.deepcopy(c["models"]),
        "fit_slots": [
            {k: c["model_specs"][slot][k] for k in keys if k in c["model_specs"][slot]}
            for slot in c["slot_order"]
        ],
    }
    old = {
        "config": {"features": copy.deepcopy(c["features"])},
        "sources": {"runtime": copy.deepcopy(sources["runtime"])},
    }
    report = {
        "attempts": [
            {"slot": s["reuse_slot"], "summary": copy.deepcopy(s["original_summary"])}
            for s in c["model_specs"].values()
            if s["reuse_slot"] is not None
        ]
    }
    return c, sources, p, old, report


@pytest.mark.parametrize(
    "change",
    [None, "budget", "threads", "union", "training", "runtime", "reused", "authority", "seed"],
)
def test_fixed_contract_rejects_undeclared_changes(change):
    c, sources, p, old, report = contract_fixture()
    if change == "budget":
        c["max_fit_attempts"] = 99
    elif change == "threads":
        c["compute_threads"] = 3
    elif change == "union":
        c["model_specs"][c["slot_order"][0]]["prediction_sessions"].pop()
    elif change == "training":
        c["model_specs"][c["slot_order"][0]]["train_end"] = "2026-09-10"
    elif change == "runtime":
        sources["runtime"] = {}
    elif change == "reused":
        c["model_specs"][c["reuse_slots"][0]]["original_summary"]["train_rows"] += 1
    elif change == "authority":
        c["allow_refit"] = True
    elif change == "seed":
        c["models"]["lightgbm"]["seed"] = 42
    if change is None:
        validate_contract(c, sources, p, old, report)
    else:
        with pytest.raises(DataValidationError):
            validate_contract(c, sources, p, old, report)


def test_worker_requires_the_new_experiment_and_shared_locks(tmp_path):
    with inherited_locks([tmp_path / HEAVY, tmp_path / OUTPUT]) as descriptors:
        verify_locks(tmp_path, descriptors)
        with pytest.raises(DataValidationError):
            verify_locks(tmp_path, list(reversed(descriptors)))


@pytest.mark.parametrize("reason", ["rss", "time", "disk"])
def test_capped_worker_is_terminated_without_retry(tmp_path, monkeypatch, reason):
    from quantlab.research import extended_frequency_protocol as module

    c = config()
    monkeypatch.setattr(
        module,
        "generated_bytes",
        lambda root: c["max_generated_bytes"] + 1 if reason == "disk" else 0,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        result = monitor(
            process,
            tmp_path,
            c,
            time.monotonic() - (3000 if reason == "time" else 0),
            rss_reader=lambda pid: c["max_rss_bytes"] + 1 if reason == "rss" else 100,
        )
        assert result["returncode"] != 0 and result["violation"] is not None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
