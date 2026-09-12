import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_data import fit_scaler_inplace
from quantlab.research.alpha158_rolling_protocol import FitLedger
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_data import (
    matrix_for_week,
    membership,
    policy_diagnostics,
    prediction_mask,
    train_mask,
)
from quantlab.research.weekly_pilot_protocol import (
    inherited_locks,
    invoke_once,
    job_rss,
    monitor,
    stop_owned_process,
)

SLOTS = [f"week{i}_{kind}" for i in (1, 2, 3) for kind in ("ridge", "lightgbm")]


def meta():
    return pd.DataFrame(
        {
            "instrument_id": ["A", "B", "C", "D"],
            "trade_date": pd.to_datetime(["2024-01-02"] * 4),
            "label_end_date": pd.to_datetime(["2024-01-09", "2024-01-09", "2024-01-10", None]),
            "future_return_5d": [1.0, 2.0, 1000.0, np.nan],
            "complete_features": [True] * 4,
            "label_reason": ["available"] * 4,
        }
    )


def test_weekly_matrix_only_uses_mature_rows_and_independent_scaler(tmp_path, monkeypatch):
    from quantlab.research import weekly_pilot_data as module

    frame = meta()
    values = np.array([[1, 5], [3, 5], [1000, 1000], [2000, 2000]], dtype="float32")
    week = {"train_start": "2024-01-01", "train_end": "2024-01-09", "train_rows": 2}
    monkeypatch.setattr(module, "batches", lambda *args: iter([(frame, values)]))
    x, y = matrix_for_week(tmp_path, tmp_path, {}, ["x", "y"], week, membership(frame.iloc[:2]))
    assert x.flags.f_contiguous and np.array_equal(y, [1, 2])
    assert np.array_equal(x, values[:2])
    scaler = fit_scaler_inplace(x)
    assert scaler["mean"] == [2, 5] and scaler["scale"] == [1, 1]
    assert np.array_equal(x, [[-1, 0], [1, 0]])


def test_prediction_membership_ignores_labels_endpoints_and_values():
    frame = meta()
    original = prediction_mask(frame, ["2024-01-02"])
    frame.future_return_5d = np.nan
    frame.label_end_date = pd.NaT
    assert np.array_equal(original, prediction_mask(frame, ["2024-01-02"]))
    assert not train_mask(frame, {"train_start": "2024-01-01", "train_end": "2024-01-09"}).any()


@pytest.mark.parametrize("change", ["count", "hash", "labels"])
def test_matrix_identity_and_label_change_is_not_silently_filled(tmp_path, monkeypatch, change):
    from quantlab.research import weekly_pilot_data as module

    frame = meta()
    expected = membership(frame.iloc[:2])
    week = {"train_start": "2024-01-01", "train_end": "2024-01-09", "train_rows": 2}
    if change == "count":
        week["train_rows"] = 1
    elif change == "hash":
        expected = "0" * 64
    else:
        frame.loc[0, "future_return_5d"] = np.nan
    monkeypatch.setattr(
        module, "batches", lambda *args: iter([(frame, np.ones((4, 2), dtype="float32"))])
    )
    with pytest.raises(DataValidationError):
        matrix_for_week(tmp_path, tmp_path, {}, ["x", "y"], week, expected)


def test_crashed_or_completed_fit_never_gets_a_second_invocation(tmp_path):
    ledger = FitLedger(tmp_path, "identity", SLOTS)
    folder = ledger.start(SLOTS[0])
    calls = []

    def failed():
        calls.append(1)
        raise RuntimeError("synthetic fit failed")

    with pytest.raises(RuntimeError):
        invoke_once(folder, "identity", SLOTS[0], failed)
    assert sealed_read(folder / "invoked.json")["slot"] == SLOTS[0]
    with pytest.raises(FileExistsError):
        invoke_once(folder, "identity", SLOTS[0], lambda: calls.append(2))
    with pytest.raises(DataValidationError):
        FitLedger(tmp_path, "identity", SLOTS).start(SLOTS[0])
    assert calls == [1]
    ledger.finish(SLOTS[0], "failed", error="kept")
    assert ledger.read(SLOTS[0])["status"] == "failed"
    with pytest.raises(DataValidationError):
        invoke_once(folder, "identity", SLOTS[0], lambda: calls.append(3))


def test_exact_six_slots_and_retained_failure_tampering(tmp_path):
    with pytest.raises(DataValidationError):
        FitLedger(tmp_path, "id", SLOTS + ["extra"])
    ledger = FitLedger(tmp_path, "id", SLOTS)
    folder = ledger.start(SLOTS[0])
    with pytest.raises(DataValidationError):
        invoke_once(folder, "another", SLOTS[0], lambda: None)
    ledger.finish(SLOTS[0], "failed")
    path = folder / "started.json"
    path.write_text(path.read_text().replace('"id"', '"changed"'))
    with pytest.raises(DataValidationError):
        ledger.read(SLOTS[0])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux inherited flock contract")
def test_child_keeps_heavy_lock_after_coordinator_closes_its_copy(tmp_path):
    child = None
    try:
        with inherited_locks([tmp_path / "one", tmp_path / "two"]) as descriptors:
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys,time;print('ready',flush=True);time.sleep(20)"],
                pass_fds=descriptors,
                start_new_session=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            assert child.stdout.readline().strip() == "ready"
        with pytest.raises(DataValidationError):
            with inherited_locks([tmp_path / "one", tmp_path / "two"]):
                pass
    finally:
        if child is not None:
            stop_owned_process(child)
    with inherited_locks([tmp_path / "one", tmp_path / "two"]):
        pass


def test_rss_sums_descendants_without_double_counting(tmp_path):
    for pid, resident, children in [(100, 2, "200 200"), (200, 3, "")]:
        folder = tmp_path / str(pid)
        (folder / "task" / str(pid)).mkdir(parents=True)
        (folder / "statm").write_text(f"99 {resident} 0")
        (folder / "task" / str(pid) / "children").write_text(children)
    assert job_rss(100, tmp_path) == 5 * os.sysconf("SC_PAGE_SIZE")
    # A child can be spawned by a non-main thread; it must remain in the process cap.
    (tmp_path / "100/task/101").mkdir()
    (tmp_path / "100/task/101/children").write_text("200 300")
    (tmp_path / "300/task/300").mkdir(parents=True)
    (tmp_path / "300/statm").write_text("99 4 0")
    (tmp_path / "300/task/300/children").write_text("")
    assert job_rss(100, tmp_path) == 9 * os.sysconf("SC_PAGE_SIZE")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process group watchdog")
def test_combined_cap_terminates_only_owned_worker(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
    )
    try:
        report = monitor(
            child, tmp_path, {"max_rss_bytes": 1}, time.monotonic(), rss_reader=lambda pid: 2
        )
        assert report["violation"] == "combined_worker_tree_rss" and report["returncode"] != 0
    finally:
        stop_owned_process(child)


@pytest.mark.parametrize(
    "reason", ["segment_wall_time", "cumulative_generated_bytes", "host_D_reserve"]
)
def test_other_worker_caps_preserve_explicit_failure(tmp_path, monkeypatch, reason):
    from types import SimpleNamespace

    from quantlab.research import weekly_pilot_protocol as protocol

    config = {
        "max_rss_bytes": 10,
        "max_wakeup_seconds": 100,
        "max_generated_bytes": 10,
        "reserve_host_D_bytes": 1,
    }
    began = time.monotonic()
    if reason == "segment_wall_time":
        began -= 101
    monkeypatch.setattr(
        protocol,
        "generated_bytes",
        lambda root: 11 if reason == "cumulative_generated_bytes" else 0,
    )
    monkeypatch.setattr(
        protocol.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=0 if reason == "host_D_reserve" else 100),
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
    )
    try:
        result = monitor(child, tmp_path, config, began, rss_reader=lambda pid: 1)
        assert result["violation"] == reason and result["returncode"] != 0
    finally:
        stop_owned_process(child)


def policy_fixture():
    days = [str(day.date()) for day in pd.bdate_range("2024-01-02", periods=6)]
    weeks = [
        {
            "week": i + 1,
            "prediction_start": days[i * 2],
            "prediction_end": days[i * 2 + 1],
            "prediction_sessions": days[i * 2 : i * 2 + 2],
            "evaluation_label_cutoff": "2024-02-01",
            "prediction_rows": 6,
            "evaluation_rows": 6,
        }
        for i in range(3)
    ]
    frame = pd.DataFrame(
        [
            {
                "instrument_id": code,
                "trade_date": pd.Timestamp(day),
                "label_end_date": pd.Timestamp("2024-01-20"),
                "future_return_5d": float(i),
                "complete_features": True,
                "label_reason": "available",
                "score": float(i),
            }
            for day in days
            for i, code in enumerate(["A", "B", "C"])
        ]
    )
    frames = {}
    for kind in ["ridge", "lightgbm"]:
        frames[f"week1_{kind}"] = frame.copy()
        for w in weeks[1:]:
            part = frame.loc[frame.trade_date.isin(pd.to_datetime(w["prediction_sessions"]))].copy()
            part.score *= -1
            frames[f"week{w['week']}_{kind}"] = part
    return frames, weeks, days


def test_anchor_reuse_matched_labels_and_week_boundary_stability():
    frames, weeks, days = policy_fixture()
    rows, daily, paired = policy_diagnostics(frames, weeks, days)
    assert len(rows) == 12 and len(daily) == 24 and len(paired) == 4
    assert all(x["week"] > 1 and x["mean_weekly_minus_anchor_rank_ic"] == -2 for x in paired)
    assert {r["model_slot"] for r in rows if r["policy"] == "anchor"} == {
        "week1_ridge",
        "week1_lightgbm",
    }
    boundary = daily.loc[
        (daily.model == "ridge") & (daily.policy == "weekly") & (daily.trade_date == days[2])
    ]
    assert boundary.rank_stability.iloc[0] == -1


@pytest.mark.parametrize("change", ["members", "labels", "calendar"])
def test_policy_pair_rejects_unmatched_cohorts_and_missing_days(change):
    frames, weeks, days = policy_fixture()
    if change == "members":
        frames["week2_ridge"] = frames["week2_ridge"].iloc[1:]
    elif change == "labels":
        frames["week2_ridge"].loc[:, "future_return_5d"] = 999
    else:
        days = days[:-1]
    with pytest.raises(DataValidationError):
        policy_diagnostics(frames, weeks, days)


def test_constant_scores_keep_undefined_rank_ic():
    frames, weeks, days = policy_fixture()
    for frame in frames.values():
        frame.score = 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rows, _, paired = policy_diagnostics(frames, weeks, days)
    assert all(row["mean_rank_ic"] is None and row["rank_ic_days"] == 0 for row in rows)
    assert all(row["mean_weekly_minus_anchor_rank_ic"] is None for row in paired)


@pytest.mark.parametrize(
    "change", [None, "model", "predictions", "train_rows", "chronology", "authority", "iterations"]
)
def test_worker_validation_rejects_changed_evidence(tmp_path, change):
    from quantlab.research.alpha158_store import atomic_seal
    from quantlab.research.input_audit import _sha
    from quantlab.research.weekly_pilot import validated_worker
    from quantlab.research.weekly_pilot_protocol import OUTPUT

    out = tmp_path / OUTPUT
    out.mkdir(parents=True)
    ledger = FitLedger(out, "id", SLOTS)
    folder = ledger.start("week1_ridge")
    invoke_once(folder, "id", "week1_ridge", lambda: None)
    (folder / "model.pkl").write_bytes(b"synthetic-model-not-loaded")
    frames, weeks, _ = policy_fixture()
    frames["week1_ridge"].to_parquet(folder / "predictions.parquet", index=False)
    for week in weeks:
        week.update(train_rows=2, train_end="2023-12-29")
    plan = {
        "fingerprint": "id",
        "config": {
            "weeks": weeks,
            "all_weeks_prediction_sha256": "members",
            "models": {"ridge": {"max_iter": 200}},
        },
    }
    summary = {
        "identity": "id",
        "slot": "week1_ridge",
        "train_rows": 2,
        "train_end": "2023-12-29",
        "prediction_rows": 18,
        "membership_sha256": "members",
        "saved_model_max_prediction_difference": 0,
        "replay_prediction_start": weeks[0]["prediction_start"],
        "trained_at_utc": sealed_read(folder / "invoked.json")["at"],
        "history_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
        "ridge_n_iter": [3],
        "saved_model_sha256": _sha(folder / "model.pkl"),
        "prediction_file_sha256": _sha(folder / "predictions.parquet"),
    }
    if change == "model":
        (folder / "model.pkl").write_bytes(b"different")
    elif change == "predictions":
        (folder / "predictions.parquet").write_bytes(b"different")
    elif change == "train_rows":
        summary["train_rows"] = 3
    elif change == "chronology":
        summary["trained_at_utc"] = "2020-01-01T00:00:00+00:00"
    elif change == "authority":
        summary["execution_authority"] = 0
    elif change == "iterations":
        summary["ridge_n_iter"] = [201]
    atomic_seal(folder / "worker_result.json", summary)
    if change is None:
        assert validated_worker(tmp_path, plan, "week1_ridge")["prediction_rows"] == 18
    else:
        with pytest.raises(DataValidationError):
            validated_worker(tmp_path, plan, "week1_ridge")


def test_resume_publishes_verified_finished_workers_without_spawning_fits(tmp_path, monkeypatch):
    from quantlab.research import weekly_pilot as module
    from quantlab.research.alpha158_store import atomic_seal
    from quantlab.research.weekly_pilot_protocol import OUTPUT

    out = tmp_path / OUTPUT
    out.mkdir(parents=True)
    config = {"slots": SLOTS, "max_generated_bytes": 1024**3}
    plan = {"fingerprint": "id", "config": config, "sources": {"inputs": {}}, "code_files": {}}
    ledger = FitLedger(out, "id", SLOTS)
    for slot in SLOTS:
        folder = ledger.start(slot)
        invoke_once(folder, "id", slot, lambda: None)
        atomic_seal(
            folder / "monitor.json",
            {"returncode": 0, "violation": None, "combined_peak_rss_bytes": 7},
        )
    monkeypatch.setattr(module, "prepare_plan", lambda root: plan)
    monkeypatch.setattr(
        module, "validated_worker", lambda root, plan, slot: {"identity": "id", "slot": slot}
    )
    monkeypatch.setattr(module, "make_diagnostics", lambda *args: {})

    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("recovery must not spawn another fit")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden_spawn)
    result = module.run(tmp_path)
    assert result["status"] == "complete" and result["actual_fit_invocations"] == 6
    assert result["combined_peak_rss_bytes"] == 7
    assert all(
        x["recovery"] == "verified saved worker; no additional fit" for x in result["attempts"]
    )
