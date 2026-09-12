import json

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_plan import schedule
from quantlab.research.extended_plan_review import read_verification
from quantlab.research.extended_plan_run import CONFIG, OUTPUT, read_report, run
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read


def fixture(root, monkeypatch):
    import pandas as pd

    from quantlab.research import extended_plan_run as module

    monkeypatch.setattr(module, "verify_historical_inputs", lambda *args: None)
    out = root / OUTPUT
    out.mkdir(parents=True)
    cfg = root / CONFIG
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"models": {"ridge": {}, "lightgbm": {}}}))
    source = root / "source.bin"
    source.write_bytes(b"frozen")
    rows = schedule(pd.bdate_range("2020-01-01", "2026-09-10"), "2026-09-10")
    plan = atomic_seal(
        out / "plan.json",
        {
            "code_head": "source",
            "code_files": {},
            "config_sha256": _sha(cfg),
            "inputs": {"source.bin": {"sha256": _sha(source)}},
            "schedule": rows,
        },
    )
    weeks = [
        {
            **row,
            "prediction_rows": 100,
            "evaluation_rows": 99,
            "train_rows": 200,
            "train_feature_rows": 205,
            "train_unmatured_or_unknown_end": 5,
            "train_missing_label_rows": 0,
            "prediction_missing_label_rows": 1,
        }
        for row in rows
    ]
    slots = [
        {"slot": f"{row['week_id']}_{kind}", "reuse_slot": f"week{i + 1}_{kind}" if i < 3 else None}
        for i, row in enumerate(rows)
        for kind in ("ridge", "lightgbm")
    ]
    weekly = {
        "plan_fingerprint": plan["fingerprint"],
        "weeks": weeks,
        "fit_slots": slots,
        "models": {"ridge": {}, "lightgbm": {}},
        "monthly_anchor_weeks": list(dict.fromkeys(row["monthly_anchor"] for row in rows)),
        "signal_market_days": sum(len(row["prediction_sessions"]) for row in rows),
        "prediction_rows_per_policy_model": 100 * len(rows),
        "evaluation_rows_per_policy_model": 99 * len(rows),
        "required_unique_models": len(slots),
        "reused_frozen_models": 6,
        "future_max_new_fit_attempts": len(slots) - 6,
        "future_max_attempts_per_segment": 6,
        "actual_new_fit_attempts": 0,
        "new_prediction_rows": 0,
    }
    weekly = atomic_seal(out / "schedule.json", weekly)
    report = {
        "plan_fingerprint": plan["fingerprint"],
        "schedule_fingerprint": weekly["fingerprint"],
        "artifacts": {"schedule.json": {"sha256": _sha(out / "schedule.json")}},
        "new_fit_attempts": 0,
        "new_prediction_rows": 0,
        "provider_calls": 0,
        "performance_evidence": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "history_already_observed": True,
        "metadata_rows": 9487149,
        "metadata_codes": 5443,
        "weekly_rows": len(rows),
        "monthly_anchors": 12,
        "required_unique_models": len(slots),
        "reused_frozen_models": 6,
        "future_max_new_fit_attempts": len(slots) - 6,
    }
    report = atomic_seal(out / "report.json", report)
    return report, weekly


def replace(path, value):
    path.unlink()
    return atomic_seal(path, {k: v for k, v in value.items() if k != "fingerprint"})


def test_saved_plan_read_and_resume_never_enter_planning_or_model_code(tmp_path, monkeypatch):
    from quantlab.research import extended_plan_run as module

    report, _ = fixture(tmp_path, monkeypatch)

    def forbidden(*args):
        raise AssertionError("read-only resume must not enter new processing")

    monkeypatch.setattr(module, "load_pilot", forbidden)
    monkeypatch.setattr(module, "code_binding", forbidden)
    assert run(tmp_path) == report


@pytest.mark.parametrize(
    "case",
    [
        "source",
        "config",
        "action",
        "bool_action",
        "authority",
        "calendar",
        "budget",
        "slot",
        "reuse",
        "count",
    ],
)
def test_resealed_scope_and_schedule_changes_are_rejected(tmp_path, monkeypatch, case):
    report, weekly = fixture(tmp_path, monkeypatch)
    out = tmp_path / OUTPUT
    if case == "source":
        (tmp_path / "source.bin").write_bytes(b"changed")
    elif case == "config":
        (tmp_path / CONFIG).write_text("{}")
    elif case == "action":
        report["new_fit_attempts"] = 1
    elif case == "bool_action":
        weekly["actual_new_fit_attempts"] = False
    elif case == "authority":
        report["performance_evidence"] = True
    elif case == "calendar":
        weekly["weeks"][0]["prediction_start"] = "2025-09-02"
    elif case == "budget":
        weekly["future_max_new_fit_attempts"] += 1
    elif case == "slot":
        weekly["fit_slots"][0]["slot"] = "other"
    elif case == "reuse":
        weekly["fit_slots"][0]["reuse_slot"] = "other"
    else:
        report["metadata_codes"] -= 1
    weekly = replace(out / "schedule.json", weekly)
    report["schedule_fingerprint"] = weekly["fingerprint"]
    report["artifacts"]["schedule.json"]["sha256"] = _sha(out / "schedule.json")
    replace(out / "report.json", report)
    with pytest.raises(DataValidationError):
        read_report(tmp_path)


@pytest.mark.parametrize("change", [None, "source", "fit", "bool", "population", "authority"])
def test_independent_review_has_exact_zero_action_source_and_population(
    tmp_path, monkeypatch, change
):
    report, weekly = fixture(tmp_path, monkeypatch)
    assert read_verification(tmp_path, report, weekly) is None
    fields = (
        "train_feature_rows",
        "train_rows",
        "train_unmatured_or_unknown_end",
        "train_missing_label_rows",
        "prediction_rows",
        "evaluation_rows",
        "prediction_missing_label_rows",
    )
    review = {
        k: report[k]
        for k in (
            "metadata_rows",
            "metadata_codes",
            "weekly_rows",
            "monthly_anchors",
            "future_max_new_fit_attempts",
        )
    }
    review.update(
        report_fingerprint=report["fingerprint"],
        schedule_fingerprint=weekly["fingerprint"],
        source_head="source",
        signal_market_days=weekly["signal_market_days"],
        reused_models=6,
        new_fit_attempts=0,
        new_prediction_rows=0,
        provider_calls=0,
        performance_evidence=False,
        execution_authority=False,
        weekly_populations=[{k: row[k] for k in fields} for row in weekly["weeks"]],
    )
    if change == "source":
        review["source_head"] = "wrong"
    elif change == "fit":
        review["new_fit_attempts"] = 1
    elif change == "bool":
        review["new_prediction_rows"] = False
    elif change == "population":
        review["weekly_populations"][0]["train_rows"] -= 1
    elif change == "authority":
        review["execution_authority"] = True
    path = tmp_path / OUTPUT / "independent_verification.json"
    atomic_seal(path, review)
    if change is None:
        assert read_verification(tmp_path, report, weekly) == sealed_read(path)
    else:
        with pytest.raises(DataValidationError):
            read_verification(tmp_path, report, weekly)
