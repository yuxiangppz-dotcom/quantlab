import copy

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.extended_plan import (
    attach_populations,
    daily_population,
    fit_inventory,
    schedule,
)
from quantlab.research.weekly_plan import population_counts


def calendar():
    return pd.bdate_range("2020-01-01", "2026-09-10").strftime("%Y-%m-%d").tolist()


def test_full_calendar_iso_year_holiday_partial_week_and_monthly_anchors():
    sessions = [day for day in calendar() if day not in ("2025-10-01", "2025-10-02", "2025-10-03")]
    rows = schedule(sessions, "2026-09-10")
    assert rows[0]["prediction_start"] == "2025-09-01"
    assert rows[-1]["prediction_sessions"] == ["2026-08-31"]
    assert rows[-1]["partial_calendar_week"]
    assert rows[-1]["evaluation_label_cutoff"] == "2026-09-07"
    expected = [day for day in sessions if "2025-09-01" <= day <= "2026-08-31"]
    assert [day for row in rows for day in row["prediction_sessions"]] == expected
    assert len({row["monthly_anchor"] for row in rows}) == 12
    boundary = next(row for row in rows if row["prediction_start"] == "2025-12-29")
    assert boundary["week_id"] == "2026w01" and boundary["month"] == "2025-12"
    october = next(row for row in rows if row["month"] == "2025-10")
    assert october["monthly_anchor"] == october["week_id"]
    for row in rows:
        assert row["train_end"] < row["prediction_start"]


@pytest.mark.parametrize("case", ["duplicate", "order", "late", "short"])
def test_bad_or_unmatured_calendars_block(case):
    days = calendar()
    if case == "duplicate":
        days += [days[-1]]
    elif case == "order":
        days.reverse()
    elif case == "short":
        days = days[-300:]
    else:
        days = [day for day in days if day <= "2026-09-01"]
    with pytest.raises(DataValidationError):
        schedule(days, days[-1] if case == "late" else "2026-09-10")


def test_daily_aggregation_matches_direct_label_purge_and_keeps_prediction_members():
    sessions = calendar()
    dates = pd.to_datetime(sessions)
    frame = pd.DataFrame(
        {
            "instrument_id": "A",
            "trade_date": dates,
            "complete_features": True,
            "label_end_date": pd.Series(dates).shift(-5),
            "future_return_5d": 1.0,
        }
    )
    frame.loc[frame.label_end_date.isna(), "future_return_5d"] = np.nan
    frame.loc[frame.trade_date == "2026-08-31", "future_return_5d"] = np.nan
    frame.loc[frame.trade_date == "2026-07-29", "complete_features"] = False
    rows = schedule(sessions, "2026-09-10")
    aggregated = attach_populations(rows, daily_population(frame, sessions), sessions)
    direct = population_counts(frame, rows, sessions)
    for actual, expected in zip(aggregated, direct, strict=True):
        assert all(
            actual[k] == v
            for k, v in expected.items()
            if k != "prediction_unmatured_or_unknown_end"
        )
    assert aggregated[-1]["prediction_rows"] == 1 and aggregated[-1]["evaluation_rows"] == 0
    changed = frame.copy()
    changed.loc[0, "label_end_date"] = changed.loc[0, "trade_date"]
    with pytest.raises(DataValidationError, match="endpoints"):
        daily_population(changed, sessions)


def reuse_fixture():
    rows = [
        {
            "week": i,
            "week_id": f"2026w{i:02d}",
            "train_start": "2023-01-01",
            "train_end": f"2026-01-{i:02d}",
            "train_rows": 100,
        }
        for i in (1, 2, 3, 4)
    ]
    config = {"features": ["f"], "models": {"ridge": {}, "lightgbm": {}}, "runtime": {"v": 1}}
    old = {
        "config": {
            **config,
            "weeks": rows[:3],
            "membership_sha256": {f"week{i}": {"train": str(i)} for i in (1, 2, 3)},
        },
        "sources": {"runtime": config["runtime"]},
        "code_head": "original",
    }
    report, prep = {"attempts": []}, {}
    for row in rows[:3]:
        for kind in ("ridge", "lightgbm"):
            slot = f"week{row['week']}_{kind}"
            report["attempts"].append(
                {"slot": slot, "status": "completed", "summary": {"saved_model_sha256": slot}}
            )
            prep[slot] = {
                **row,
                "features": ["f"],
                "train_membership_sha256": str(row["week"]),
                "fitted_on": "mature_training_only",
                "matrix_dtype": "float32",
                "label_dtype": "float32",
                "label_transform": "none",
                "scaler": None if kind == "lightgbm" else {"rows": 100, "ddof": 0},
            }
    return rows, config, old, report, {row["train_end"]: str(row["week"]) for row in rows[:3]}, prep


def test_exact_reuse_reduces_future_budget_without_loading_models(monkeypatch):
    import pickle

    def forbidden(*args, **kwargs):
        raise AssertionError("planner must not load a model")

    monkeypatch.setattr(pickle, "loads", forbidden)
    slots = fit_inventory(*reuse_fixture())
    assert len(slots) == 8 and sum(s["reuse_slot"] is not None for s in slots) == 6
    assert len(slots) - 6 == 2


@pytest.mark.parametrize(
    "case",
    ["runtime", "model", "feature", "start", "rows", "hash", "preprocess", "failure", "missing"],
)
def test_reuse_mismatch_never_becomes_a_fresh_fit(case):
    values = copy.deepcopy(reuse_fixture())
    rows, config, old, report, hashes, prep = values
    if case == "runtime":
        config["runtime"] = {"v": 2}
    elif case == "model":
        config["models"] = {}
    elif case == "feature":
        config["features"] = ["other"]
    elif case == "start":
        rows[0]["train_start"] = "2023-01-02"
        old["config"]["weeks"] = copy.deepcopy(reuse_fixture()[2]["config"]["weeks"])
    elif case == "rows":
        prep["week1_ridge"]["train_rows"] = 101
    elif case == "hash":
        hashes[rows[0]["train_end"]] = "wrong"
    elif case == "preprocess":
        prep["week1_ridge"]["label_transform"] = "changed"
    elif case == "failure":
        report["attempts"][0]["status"] = "failed"
    else:
        values = (rows[1:], config, old, report, hashes, prep)
    with pytest.raises(DataValidationError):
        fit_inventory(*values)
