import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.cost_input_audit import (
    audit_windows,
    collect_targets,
    fee_matrix,
    market_partition,
    summarize,
    validate_score_frame,
)
from quantlab.research.cost_input_corporate import prepare_events, window_events

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def contract():
    return json.loads((ROOT / "config/cost_input_audit_v1.json").read_text())


def scores():
    days = pd.to_datetime(["2023-08-25", "2023-08-28", "2023-08-29"])
    return pd.DataFrame(
        [
            {
                "instrument_id": code,
                "trade_date": day,
                "score": value,
                "complete_features": True,
                "future_return_5d": float("nan"),
            }
            for day in days
            for code, value in [("000001.SZ", 1.0), ("000002.SZ", 1.0), ("600000.SH", 0.0)]
        ]
    )


def test_physical_label_projection_tail_and_deterministic_ties(tmp_path, contract, monkeypatch):
    frame = scores()
    frame.to_parquet(tmp_path / "scores.parquet", index=False)
    contract.update(models=["ridge"], score_rows_per_model=9)
    contract["target_contract"]["max_names"] = 2
    sessions = sorted(frame.trade_date.dt.strftime("%Y-%m-%d").unique())
    manifest = {"sessions": sessions, "scores": [{"model": "ridge", "path": "scores.parquet"}]}
    original = pd.read_parquet
    used = []

    def read(path, **kwargs):
        used.append(kwargs["columns"])
        return original(path, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", read)
    grid = np.full((3, 3), 2, dtype=np.uint8)
    codes = sorted(frame.instrument_id.unique())
    targets, daily = collect_targets(tmp_path, contract, manifest, grid, codes)
    assert len(targets) == 6 and len(daily) == 3
    assert set(targets.instrument_id) == {"000001.SZ", "000002.SZ"}
    assert targets.target_weight.eq(0.4).all()
    assert all("future_return_5d" not in columns for columns in used)
    frame["future_return_5d"] = [1e9, -1e9, 4.0] * 3
    frame["label_end_date"] = "2099-01-01"
    frame.to_parquet(tmp_path / "scores.parquet", index=False)
    again, _ = collect_targets(tmp_path, contract, manifest, grid, codes)
    pd.testing.assert_frame_equal(targets, again)


@pytest.mark.parametrize(
    "problem",
    ["duplicate", "unknown_code", "unknown_date", "nonfinite", "feature", "grid", "already_seen"],
)
def test_score_identity_or_feature_corruption_never_changes_population(problem):
    frame = scores().iloc[:3].copy()
    grid = np.full((1, 3), 2, dtype=np.uint8)
    seen = np.zeros_like(grid, dtype=bool)
    dates = {pd.Timestamp("2023-08-25"): 0}
    codes = {c: i for i, c in enumerate(sorted(frame.instrument_id))}
    if problem == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif problem == "unknown_code":
        frame.loc[0, "instrument_id"] = "999999.SH"
    elif problem == "unknown_date":
        frame.loc[0, "trade_date"] = pd.Timestamp("2023-08-24")
    elif problem == "nonfinite":
        frame.loc[0, "score"] = np.inf
    elif problem == "feature":
        frame.loc[0, "complete_features"] = False
    elif problem == "grid":
        grid[0, 0] = 1
    else:
        seen[0, 0] = True
    with pytest.raises(DataValidationError):
        validate_score_frame(frame, grid, seen, dates, codes)


@pytest.mark.parametrize("kind", ["daily", "adj_factor"])
def test_ambiguous_invalid_missing_raw_rows_are_not_good_evidence(kind):
    day = pd.Timestamp("2023-08-25")
    frame = pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "trade_date": day,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "adj_factor": 1.0,
            },
            {
                "instrument_id": "000002.SZ",
                "trade_date": day,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": -10.0,
                "adj_factor": -1.0,
            },
        ]
    )
    frame = pd.concat([frame, frame.iloc[:1]])
    result, stats = market_partition(frame, ["000001.SZ", "000002.SZ", "600000.SH"], day, kind)
    assert stats["duplicate_key_rows"] == 2 and stats["invalid_value_rows"] == 1
    assert not result.valid.any() and result.value.isna().all()
    assert result.present.sum() == 2
    absent, missing = market_partition(None, ["000001.SZ"], day, kind)
    assert not missing["partition_present"] and not absent.valid.any()


def event(**changes):
    return {
        "instrument_id": "000001.SZ",
        "source_record_id": "event-1",
        "source": "fixture",
        "process_status": "实施",
        "period_end": "2022-12-31",
        "announcement_date": "2023-08-01",
        "implementation_announcement_date": "2023-08-20",
        "record_date": "2023-08-28",
        "ex_date": "2023-08-29",
        "pay_date": "2023-09-04",
        "share_listing_date": None,
        "stock_dividend_per_share": 0.0,
        "stock_bonus_rate": 0.0,
        "stock_conversion_rate": 0.0,
        "cash_dividend_before_tax": 0.5,
        "cash_dividend_after_tax": 0.5,
        "observed_at": "2026-09-10T01:38:00Z",
        "available_from": "2026-09-10",
        **changes,
    }


def test_event_overlap_is_not_entitlement_or_historical_availability():
    events, profile = prepare_events(pd.DataFrame([event()]))
    result = window_events(
        events,
        "000001.SZ",
        pd.Timestamp("2023-08-25"),
        pd.Timestamp("2023-08-28"),
        pd.Timestamp("2023-08-30"),
        pd.Timestamp("2023-09-05"),
    )
    assert result["candidate_event_rows"] == 1
    assert result["candidate_rows_observed_by_signal_close"] == 0
    assert result["candidate_payments_after_intended_exit"] == 1
    assert result["entitlement_amount"] is None and result["dividend_tax_fen"] is None
    assert result["unknown_event_history"] and not profile["complete_event_history_certified"]


def test_observation_duplicates_preserve_first_seen_and_conflicts_stay_unknown():
    a = event()
    later = event(observed_at="2026-09-11T01:00:00Z", available_from="2026-09-11")
    events, profile = prepare_events(pd.DataFrame([later, a]))
    assert len(events) == 1 and profile["repeated_source_observations"] == 1
    assert events.iloc[0].observation_day == pd.Timestamp("2026-09-10")
    assert profile["quality_counts"]["conflicting_source_identity"] == 0
    later["cash_dividend_before_tax"] = 0.6
    events, profile = prepare_events(pd.DataFrame([a, later]))
    assert profile["quality_counts"]["conflicting_source_identity"] == 2
    assert events.quality_unknown.all()


@pytest.mark.parametrize(
    "change,flag",
    [
        ({"record_date": "broken"}, "malformed_values"),
        ({"cash_dividend_before_tax": "broken"}, "malformed_values"),
        ({"available_from": "2023-08-25"}, "availability_before_observation"),
        ({"record_date": "2023-08-30"}, "record_after_ex"),
        ({"pay_date": None}, "positive_cash_missing_pay_date"),
        ({"observed_at": "2026-09-10 01:00:00"}, "observation_timezone_unknown"),
        ({"stock_dividend_per_share": 0.2}, "share_components_disagree"),
    ],
)
def test_event_quality_problems_are_explicit(change, flag):
    events, profile = prepare_events(pd.DataFrame([event(**change)]))
    assert profile["quality_counts"][flag] == 1 and events.quality_unknown.all()


def test_intended_exit_offsets_tail_and_missing_data_preserve_targets(contract):
    sessions = list(pd.to_datetime(["2023-08-25", "2023-08-28", "2023-08-29"]))
    targets = pd.DataFrame(
        [
            {"model": "ridge", "instrument_id": "000001.SZ", "trade_date": d}
            for d in (sessions[0], sessions[-1])
        ]
    )
    market = pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "trade_date": d,
                "daily_valid": True,
                "adj_factor_valid": True,
                "adj_factor_value": float(i + 1),
            }
            for i, d in enumerate(sessions)
        ]
    )
    events, _ = prepare_events(pd.DataFrame())
    contract["horizons"] = [1]
    result = audit_windows(targets, market, events, contract, sessions)
    assert len(result) == 2
    assert result.iloc[0].entry_date == sessions[1] and result.iloc[0].exit_date == sessions[2]
    assert result.iloc[0].complete_market_window and result.iloc[0].adjacent_adjustment_changes == 2
    assert result.iloc[1].entry_beyond_cutoff and result.iloc[1].exit_beyond_cutoff
    assert result.iloc[1].observed_window_sessions == 0
    assert result.complete_cost_fen.isna().all() and not result.execution_eligible.any()
    assert summarize(result)[0]["windows_with_event_history_unknown"] == 2
    market.loc[1, "daily_valid"] = False
    again = audit_windows(targets, market, events, contract, sessions)
    pd.testing.assert_frame_equal(
        result[["model", "instrument_id", "trade_date"]],
        again[["model", "instrument_id", "trade_date"]],
    )
    assert not again.complete_market_window.any()


def test_fee_components_keep_minimum_dated_stamp_and_unknown_total(contract):
    result = fee_matrix(ROOT, contract, ["2023-08-25", "2023-08-28"])
    assert len(result) == 12
    small = result.loc[result.hypothetical_capital_cny.eq(50000)]
    assert small.commission_fen.eq(500).all()
    assert small.loc[small.side.eq("sell"), "stamp_duty_fen"].tolist() == [200, 100]
    assert small.loc[small.side.eq("buy"), "stamp_duty_fen"].eq(0).all()
    assert result.complete_trading_cost_fen.isna().all()
    assert result.share_quantity.isna().all() and not result.execution_authority.any()
