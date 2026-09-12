import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.weekly_plan import monthly_summary, pilot_schedule, population_counts


def calendar(end="2026-09-10"):
    return [str(x.date()) for x in pd.bdate_range("2020-01-01", end)]


def test_three_week_schedule_uses_previous_session_and_mature_endpoint():
    days = calendar()
    days.remove("2026-08-07")  # Synthetic holiday: Friday is not a training cutoff.
    result = pilot_schedule(days, "2026-09-10")
    assert [x["week_monday"] for x in result] == ["2026-08-03", "2026-08-10", "2026-08-17"]
    assert [x["train_end"] for x in result] == ["2026-07-31", "2026-08-06", "2026-08-14"]
    assert result[1]["train_start"] == "2023-08-07"
    assert result[-1]["evaluation_label_cutoff"] == "2026-08-28"
    assert all(x["anchor_week"] == 1 for x in result)


def test_calendar_year_transition_and_leap_training_start():
    result = pilot_schedule(calendar("2025-01-10"), "2025-01-10")
    assert all(x["week_monday"].startswith("2024-12") for x in result)
    # First full March week follows Feb29 when Mar1 is a synthetic holiday.
    days = calendar("2024-04-10")
    days.remove("2024-03-01")
    result = pilot_schedule(days, "2024-04-10")
    assert result[0]["train_end"] == "2024-02-29"
    assert result[0]["train_start"] == "2021-03-01"


@pytest.mark.parametrize("change", ["duplicate", "unsorted", "cutoff", "short", "empty_week"])
def test_unavailable_calendar_fails_without_business_day_fallback(change):
    days = calendar()
    if change == "duplicate":
        days.append(days[-1])
    elif change == "unsorted":
        days.reverse()
    elif change == "cutoff":
        days.pop()
    elif change == "short":
        days = days[-40:]
    else:
        days = [x for x in days if not "2026-08-10" <= x <= "2026-08-16"]
    with pytest.raises(DataValidationError):
        pilot_schedule(days, "2026-09-10")


def metadata(days):
    dates = pd.to_datetime(["2026-07-24", "2026-07-27", "2026-08-03", "2026-08-04"])
    lookup = dict(zip(pd.to_datetime(days[:-5]), pd.to_datetime(days[5:]), strict=True))
    return pd.DataFrame(
        {
            "instrument_id": ["000001.SZ"] * 4,
            "trade_date": dates,
            "label_end_date": [lookup[x] for x in dates],
            "future_return_5d": [0.1, 0.2, 0.3, np.nan],
            "complete_features": [True] * 4,
        }
    )


def test_exact_maturity_and_missing_labels_do_not_select_predictions():
    days = calendar()
    plan = pilot_schedule(days, "2026-09-10")
    frame = metadata(days)
    result = population_counts(frame, plan, days)[0]
    assert result["train_feature_rows"] == 2 and result["train_rows"] == 1
    assert result["train_unmatured_or_unknown_end"] == 1
    assert result["prediction_rows"] == 2 and result["evaluation_rows"] == 1
    frame.loc[2, "future_return_5d"] = np.nan
    changed = population_counts(frame, plan, days)[0]
    assert changed["prediction_rows"] == 2 and changed["evaluation_rows"] == 0


@pytest.mark.parametrize("change", ["endpoint", "duplicate", "feature_unknown", "outside"])
def test_metadata_unknown_or_shifted_identity_is_rejected(change):
    days = calendar()
    frame = metadata(days)
    if change == "endpoint":
        frame.loc[0, "label_end_date"] += pd.Timedelta(days=1)
    elif change == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif change == "feature_unknown":
        frame["complete_features"] = frame.complete_features.astype(object)
        frame.loc[0, "complete_features"] = None
    else:
        frame.loc[0, "trade_date"] = pd.Timestamp("2026-07-25")
    with pytest.raises(DataValidationError):
        population_counts(frame, pilot_schedule(days, "2026-09-10"), days)


def daily():
    return pd.DataFrame(
        {
            "trade_date": ["2026-01-30", "2026-02-02", "2026-02-03"],
            "score_rows": [10, 0, 10],
            "evaluation_rows": [8, 0, 9],
            "missing_label_rows": [2, 0, 1],
            "crossing_label_rows": [0, 0, 0],
            "rank_ic": [0.5, np.nan, -0.5],
            "rank_stability": [np.nan] * 3,
            "top20_membership_change": [0.2, np.nan, 0.4],
        }
    )


def test_monthly_preserves_empty_days_null_metrics_and_equal_day_weights():
    frame = daily()
    rows = monthly_summary(frame, list(frame.trade_date))
    assert len(rows) == 2 and rows[1]["market_days"] == 2 and rows[1]["score_days"] == 1
    assert rows[1]["mean_rank_ic"] == -0.5 and rows[1]["rank_ic_days"] == 1
    assert rows[1]["positive_ic_day_fraction"] == 0
    assert all(x["mean_rank_stability"] is None for x in rows)
    frame["rank_ic"] = np.nan
    assert monthly_summary(frame, list(frame.trade_date))[0]["mean_rank_ic"] is None


@pytest.mark.parametrize(
    "change", ["duplicate", "missing_day", "negative_count", "inf", "extra_label"]
)
def test_monthly_invalid_saved_evidence_is_not_silently_dropped(change):
    frame = daily()
    days = list(frame.trade_date)
    if change == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif change == "missing_day":
        frame = frame.iloc[1:]
    elif change == "negative_count":
        frame.loc[0, "score_rows"] = -1
    elif change == "inf":
        frame.loc[0, "rank_ic"] = np.inf
    else:
        frame.loc[0, "evaluation_rows"] = 100
    with pytest.raises(DataValidationError):
        monthly_summary(frame, days)


@pytest.mark.parametrize("change", [None, "source", "output", "authority", "population"])
def test_weekly_reader_rejects_changed_source_and_output(tmp_path, monkeypatch, change):
    import json

    from quantlab.research import weekly_plan_run as module
    from quantlab.research.input_audit import _sha
    from quantlab.research.round2_dataset import sealed_write

    monkeypatch.setattr(module, "verify_historical_inputs", lambda *args: None)
    config = tmp_path / module.CONFIG
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"metadata_rows": 10, "metadata_codes": 1, "models": {}}))
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    out = tmp_path / module.OUTPUT
    out.mkdir(parents=True)
    plan = sealed_write(
        out / "plan.json",
        {
            "code_head": "a" * 40,
            "code_files": {},
            "config_sha256": _sha(config),
            "inputs": {"source.bin": {"sha256": _sha(source)}},
        },
    )
    monthly = sealed_write(
        out / "monthly.json",
        {"plan_fingerprint": plan["fingerprint"], "rows": [], "annual": [{"market_days": 2}]},
    )
    weekly = sealed_write(
        out / "weekly.json",
        {
            "plan_fingerprint": plan["fingerprint"],
            "rows": [{}, {}, {}],
            "models": {},
            "actual_fit_attempts": 0,
            "max_future_fit_attempts": 6,
        },
    )
    report = {
        "plan_fingerprint": plan["fingerprint"],
        "monthly_fingerprint": monthly["fingerprint"],
        "weekly_fingerprint": weekly["fingerprint"],
        "metadata_rows": 10,
        "metadata_codes": 1,
        "monthly_rows": 0,
        "daily_rows": 2,
        "new_fit_attempts": 0,
        "provider_calls": 0,
        "future_pilot_max_fit_attempts": 6,
        "historical_data_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "artifacts": {
            name: {"sha256": _sha(out / name)} for name in ("monthly.json", "weekly.json")
        },
    }
    if change == "source":
        source.write_bytes(b"different")
    elif change == "output":
        (out / "weekly.json").write_bytes(b"{}")
    elif change == "authority":
        report["execution_authority"] = 0
    elif change == "population":
        report["metadata_rows"] = 0
    sealed_write(out / "report.json", report)
    if change is None:
        assert module.read_report(tmp_path)[0]["metadata_rows"] == 10
    else:
        with pytest.raises(DataValidationError):
            module.read_report(tmp_path)


@pytest.mark.parametrize("change", [None, "source", "counts", "authority", "boolean"])
def test_independent_weekly_review_binds_exact_model_populations(tmp_path, change):
    from quantlab.research.round2_dataset import sealed_write
    from quantlab.research.weekly_plan_run import OUTPUT, read_verification

    out = tmp_path / OUTPUT
    out.mkdir(parents=True)
    sealed_write(out / "plan.json", {"code_head": "a" * 40})
    report = {
        "fingerprint": "b" * 64,
        "metadata_rows": 10,
        "metadata_codes": 1,
        "daily_rows": 5,
        "monthly_rows": 1,
    }
    values = dict.fromkeys(
        (
            "train_feature_rows",
            "train_rows",
            "train_unmatured_or_unknown_end",
            "train_missing_label_rows",
            "prediction_rows",
            "evaluation_rows",
            "prediction_missing_label_rows",
            "prediction_unmatured_or_unknown_end",
        ),
        0,
    )
    weekly = {"rows": [values]}
    review = {
        **{k: v for k, v in report.items() if k != "fingerprint"},
        "report_fingerprint": report["fingerprint"],
        "source_head": "a" * 40,
        "new_fit_attempts": 0,
        "provider_calls": 0,
        "weekly_populations": [values.copy()],
        "six_future_fit_slots": True,
        "performance_evidence": False,
        "execution_authority": False,
    }
    if change == "source":
        review["source_head"] = "c" * 40
    elif change == "counts":
        review["weekly_populations"][0]["prediction_rows"] = 1
    elif change == "authority":
        review["execution_authority"] = 0
    elif change == "boolean":
        review["weekly_populations"][0]["prediction_missing_label_rows"] = False
    sealed_write(out / "independent_verification.json", review)
    if change is None:
        assert read_verification(tmp_path, report, weekly)["source_head"] == "a" * 40
    else:
        with pytest.raises(DataValidationError):
            read_verification(tmp_path, report, weekly)
