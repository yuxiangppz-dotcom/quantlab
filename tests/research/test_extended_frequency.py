import copy
import json
import time

import numpy as np
import pandas as pd
import pytest

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research import extended_frequency as runner
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_frequency_metrics import policy_diagnostics
from quantlab.research.extended_frequency_protocol import CONFIG, OUTPUT, SOURCES, Ledger
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_protocol import invoke_once


def diagnostic_fixture():
    groups = [
        ("2025w49", "2025w49", ["2025-12-01", "2025-12-02"]),
        ("2026w01", "2025w49", ["2025-12-29", "2026-01-02"]),
        ("2026w02", "2026w02", ["2026-01-05"]),
    ]
    weeks, frames = [], {}
    for number, (week, anchor, dates) in enumerate(groups):
        weeks.append(
            {
                "week_id": week,
                "monthly_anchor": anchor,
                "prediction_sessions": dates,
                "prediction_rows": len(dates) * 24,
                "evaluation_rows": len(dates) * 24 - (1 if number == 1 else 0),
                "evaluation_label_cutoff": "2026-01-12",
            }
        )
    for number, (week, _, _) in enumerate(groups):
        dates = [d for w, a, ds in groups if w == week or a == week for d in ds]
        for kind in ("ridge", "lightgbm"):
            rows = []
            for day in dates:
                for code in range(24):
                    rows.append(
                        {
                            "instrument_id": f"S{code:02}",
                            "trade_date": pd.Timestamp(day),
                            "score": float(code if number != 1 else -code),
                            "future_return_5d": float(code),
                            "complete_features": True,
                            "label_reason": "available",
                            "label_end_date": pd.Timestamp("2026-01-12"),
                        }
                    )
            frame = pd.DataFrame(rows)
            missing = frame.trade_date.eq("2026-01-02") & frame.instrument_id.eq("S00")
            frame.loc[missing, "future_return_5d"] = np.nan
            frame.loc[missing, "label_reason"] = "missing"
            frames[f"{week}_{kind}"] = frame
    config = {
        "weeks": weeks,
        "policy_members_per_model": 120,
        "policy_evaluation_rows_per_model": 119,
        "shared_signal_days_per_model": 3,
        "comparison_signal_days_per_model": 2,
    }
    return frames, config


def test_extended_diagnostics_stitch_boundaries_and_group_actual_calendar_months():
    frames, config = diagnostic_fixture()
    daily, weekly, monthly, paired = policy_diagnostics(frames, config)
    assert (len(daily), len(weekly), len(monthly), len(paired)) == (20, 12, 8, 2)
    rows = daily.loc[(daily.model == "ridge") & (daily.policy == "weekly")].set_index("trade_date")
    assert rows.loc["2025-12-29", "previous_market_session"] == "2025-12-02"
    assert rows.loc["2025-12-29", "rank_stability"] == pytest.approx(-1)
    assert rows.loc["2025-12-29", "top20_membership_change"] == pytest.approx(0.2)
    assert rows.loc["2026-01-05", "rank_stability"] == pytest.approx(-1)
    assert pd.isna(rows.loc["2025-12-01", "rank_stability"])
    assert rows.loc["2026-01-02", "score_rows"] == 24
    assert rows.loc["2026-01-02", "evaluation_rows"] == 23
    december = next(
        r
        for r in monthly
        if r["model"] == "ridge" and r["policy"] == "weekly" and r["month"] == "2025-12"
    )
    assert december["market_days"] == 3  # Cross-year ISO week is split by actual date.
    assert all(r["week_id"] == "2026w01" and r["paired_days"] == 2 for r in paired)
    assert all(r["mean_weekly_minus_monthly_rank_ic"] == pytest.approx(-2) for r in paired)


def test_extended_null_correlations_remain_unknown():
    frames, config = diagnostic_fixture()
    for frame in frames.values():
        frame["score"] = 1.0
    daily, weekly, monthly, paired = policy_diagnostics(frames, config)
    assert daily.rank_ic.isna().all() and daily.rank_stability.isna().all()
    assert all(r["rank_ic_days"] == 0 and r["mean_rank_ic"] is None for r in weekly + monthly)
    assert all(
        r["paired_days"] == 0 and r["mean_weekly_minus_monthly_rank_ic"] is None for r in paired
    )


@pytest.mark.parametrize("damage", ["member", "duplicate", "label", "nan_score", "dates", "count"])
def test_extended_diagnostics_fail_closed_on_membership_or_evidence_drift(damage):
    frames, config = diagnostic_fixture()
    target = frames["2026w01_ridge"]
    if damage == "member":
        target.loc[0, "instrument_id"] = "different"
    elif damage == "duplicate":
        target.loc[0, "instrument_id"] = target.loc[1, "instrument_id"]
    elif damage == "label":
        target.loc[0, "future_return_5d"] = 900
    elif damage == "nan_score":
        target.loc[0, "score"] = np.nan
    elif damage == "dates":
        config["weeks"][1]["prediction_sessions"].append("2025-12-29")
    else:
        config["policy_evaluation_rows_per_model"] += 1
    with pytest.raises(DataValidationError):
        policy_diagnostics(frames, config)


@pytest.fixture
def coordinator(tmp_path, monkeypatch):
    cfg = json.loads((PROJECT_ROOT / CONFIG).read_text())
    (tmp_path / CONFIG).parent.mkdir(parents=True)
    (tmp_path / CONFIG).write_text(json.dumps(cfg))
    sources = {"inputs": {}, "config_sha256": _sha(tmp_path / CONFIG)}
    (tmp_path / SOURCES).write_text(json.dumps(sources))
    (tmp_path / OUTPUT).mkdir(parents=True)
    plan = atomic_seal(
        tmp_path / OUTPUT / "plan.json",
        {
            "config": cfg,
            "code_head": "synthetic-only",
            "code_files": {},
            "sources": sources,
        },
    )
    monkeypatch.setattr(runner, "prepare_plan", lambda root: plan)
    monkeypatch.setattr(runner, "verify_historical_inputs", lambda *a: None)
    monkeypatch.setattr(runner, "resource_preflight", lambda *a: None)
    monkeypatch.setattr(
        runner,
        "validated_worker",
        lambda root, p, slot: sealed_read(
            runner.folder_for(root, cfg, slot) / "worker_result.json"
        ),
    )
    calls = []

    def spawn(command, **kwargs):
        slot = command[command.index("--worker") + 1]
        folder = runner.folder_for(tmp_path, cfg, slot)
        if slot in cfg["new_slots"]:
            invoke_once(folder, plan["fingerprint"], slot, lambda: calls.append(slot))
        atomic_seal(
            folder / "worker_result.json",
            {
                "synthetic": True,
                "prediction_rows": 12,
                "reused_prediction_rows": 0,
                "newly_scored_rows": 12,
            },
        )
        return object()

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        runner,
        "monitor",
        lambda *a: {
            "returncode": 0,
            "violation": None,
            "combined_peak_rss_bytes": 1000,
        },
    )
    return tmp_path, plan, calls


def test_segments_have_six_job_cap_and_resume_without_reset(coordinator):
    root, plan, calls = coordinator
    first = runner.run(root)
    assert first["status"] == "checkpoint" and first["cumulative_fit_attempts"] == 6
    assert first["completed_new_fits"] == first["actual_fit_invocations"] == 6
    assert len(calls) == 6 and len(set(calls)) == 6
    assert first["next_unused_slot"] == plan["config"]["slot_order"][6]
    assert runner.load_status(root) == first
    second = runner.run(root)
    assert second["segment_jobs"] == 6 and second["completed_new_fits"] == 12
    assert len(calls) == len(set(calls)) == 12 and not (root / OUTPUT / "report.json").exists()
    # A later active reservation does not invalidate the last sealed snapshot.
    Ledger(root / OUTPUT, plan["fingerprint"], plan["config"]["new_slots"], "fits").start(
        second["next_unused_slot"]
    )
    assert runner.load_status(root) == second


def test_unavailable_resources_consume_no_slot(coordinator, monkeypatch):
    root, _, calls = coordinator

    def unavailable(*a):
        raise DataValidationError("available memory below frozen minimum")

    monkeypatch.setattr(runner, "resource_preflight", unavailable)
    with pytest.raises(DataValidationError, match="memory"):
        runner.run(root)
    assert calls == [] and not list((root / OUTPUT / "fits").iterdir())


def test_crash_recovery_only_accepts_saved_success_receipts(coordinator):
    root, plan, calls = coordinator
    ledger = Ledger(root / OUTPUT, plan["fingerprint"], plan["config"]["new_slots"], "fits")
    slot = plan["config"]["new_slots"][0]
    folder = ledger.start(slot)
    invoke_once(folder, plan["fingerprint"], slot, lambda: calls.append(slot))
    atomic_seal(folder / "worker_result.json", {"prediction_rows": 12})
    atomic_seal(folder / "monitor.json", {"returncode": 0, "violation": None})
    assert runner.recover(root, plan, ledger, slot, ledger.read(slot))
    assert ledger.read(slot)["status"] == "completed" and calls == [slot]
    assert runner.recover(root, plan, ledger, slot, ledger.read(slot))
    assert calls == [slot]


@pytest.mark.parametrize("had_invoked", [False, True])
def test_interrupted_reservation_closes_failed_without_another_fit(coordinator, had_invoked):
    root, plan, calls = coordinator
    ledger = Ledger(root / OUTPUT, plan["fingerprint"], plan["config"]["new_slots"], "fits")
    slot = plan["config"]["new_slots"][0]
    folder = ledger.start(slot)
    if had_invoked:
        invoke_once(folder, plan["fingerprint"], slot, lambda: calls.append(slot))
    result = runner.run(root)
    assert result["status"] == "failed" and result["cumulative_fit_attempts"] == 1
    assert result["actual_fit_invocations"] == int(had_invoked)
    assert result["failed_or_interrupted_jobs"] == 1 and result["completed_new_fits"] == 0
    assert runner.run(root) == result and len(calls) == int(had_invoked)


@pytest.mark.parametrize(
    "key,value",
    [
        ("cumulative_fit_attempts", 0),
        ("actual_fit_invocations", 99),
        ("prediction_rows_completed", 100000),
        ("next_unused_slot", "not_a_slot"),
        ("status", "complete"),
        ("performance_evidence", True),
    ],
)
def test_status_reader_rejects_false_budget_coverage_and_authority(coordinator, key, value):
    root, plan, _ = coordinator
    original = runner.run(root)
    changed = copy.deepcopy(original)
    changed.pop("fingerprint")
    changed[key] = value
    atomic_seal(root / OUTPUT / "checkpoints" / "9999999999999999999.json", changed)
    with pytest.raises(DataValidationError):
        runner.load_status(root)
    assert len(runner.records(root, plan)) == 6


def test_partial_batch_cannot_emit_complete_diagnostics(coordinator):
    root, plan, _ = coordinator
    runner.run(root)
    with pytest.raises(DataValidationError, match="all 98"):
        runner.diagnostic_report(root, plan, runner.records(root, plan))
    result = runner.publish(root, plan, time.monotonic(), 0)
    assert result["diagnostics_fingerprint"] is None
