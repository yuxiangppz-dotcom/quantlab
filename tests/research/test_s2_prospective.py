import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.s2_prospective import (
    CONFIG,
    parse_snapshot,
    positive_finite,
    validate_config,
    validate_entry_calendar,
    validate_registration,
    valuation_scores,
)

CODES = tuple(f"{i:06d}.SZ" for i in range(256))


def frame():
    return pd.DataFrame(
        {
            "instrument_id": CODES,
            "pe_ttm": np.arange(1, 257, dtype=float),
            "pb": np.arange(1, 257, dtype=float),
            "circ_mv": 100.0,
        }
    )


def test_equal_size_ties_and_value_direction_hand_calculation():
    f = valuation_scores(frame(), CODES)
    assert f.size_group.eq(2).all()
    assert f.score_known.all()
    assert f.score.iloc[0] == 1
    assert f.score.iloc[-1] == 1 / 256
    assert f.score.iloc[127] == 129 / 256


def test_equal_valuation_ties_use_average_rank():
    raw = frame()
    raw["pe_ttm"] = raw["pb"] = 7.0
    result = valuation_scores(raw, CODES)
    assert result.score.eq(128.5 / 256).all()


def test_input_order_and_market_value_unit_do_not_change_scores():
    raw = frame()
    raw["circ_mv"] = np.arange(1, 257, dtype=float)
    before = valuation_scores(raw, CODES)
    raw["circ_mv"] *= 10000
    after = valuation_scores(raw.iloc[::-1], CODES)
    pd.testing.assert_frame_equal(before.drop(columns="circ_mv"), after.drop(columns="circ_mv"))
    assert before.groupby("size_group").size().tolist() == [52, 51, 51, 51, 51]


@pytest.mark.parametrize("value", [None, np.nan, np.inf, -np.inf, 0, -1, True, False, "2"])
@pytest.mark.parametrize("field", ["pe_ttm", "pb", "circ_mv"])
def test_unknown_nonpositive_and_non_numeric_are_retained(value, field):
    raw = frame().astype(object)
    raw.loc[0, field] = value
    result = valuation_scores(raw, CODES)
    assert len(result) == 256 and result.instrument_id.tolist() == list(CODES)
    assert not result.score_known.iloc[0] and pd.isna(result.score.iloc[0])
    assert result.score_known.iloc[1:].all()


@pytest.mark.parametrize("count", [0, 19, 20])
def test_minimum20_valid_values_inside_stratum(count):
    raw = frame()
    raw.loc[count:, "pe_ttm"] = 0
    result = valuation_scores(raw, CODES)
    assert result.score_known.sum() == (20 if count == 20 else 0)


@pytest.mark.parametrize("count", [0, 19, 20])
def test_minimum20_size_values(count):
    raw = frame()
    raw.loc[count:, "circ_mv"] = np.nan
    result = valuation_scores(raw, CODES)
    assert result.score_known.sum() == (20 if count == 20 else 0)


def test_absent_cohort_rows_not_dropped_or_filled():
    result = valuation_scores(frame().iloc[:240], CODES)
    assert len(result) == 256
    assert result.score_known.iloc[:240].all()
    assert result.iloc[240:].score.isna().all()
    assert result.iloc[240:].circ_mv.isna().all()


@pytest.mark.parametrize("changed", ["duplicate", "null_id", "missing_column", "bad_cohort"])
def test_unusable_identity_contract_raises(changed):
    raw, codes = frame(), CODES
    if changed == "duplicate":
        raw.loc[0, "instrument_id"] = CODES[1]
    elif changed == "null_id":
        raw.loc[0, "instrument_id"] = None
    elif changed == "missing_column":
        raw = raw.drop(columns="pb")
    else:
        codes = CODES[:-1]
    with pytest.raises(DataValidationError):
        valuation_scores(raw, codes)


def wire():
    return {
        "code": 0,
        "data": {
            "fields": ["ts_code", "trade_date", "pe_ttm", "pb", "circ_mv"],
            "items": [["000001.SZ", "20260911", 10.0, 1.0, 1000.0]],
        },
    }


def test_raw_snapshot_null_and_negative_values_remain_observed_values():
    obj = wire()
    obj["data"]["items"][0][2:4] = [None, -3.0]
    result = parse_snapshot(json.dumps(obj).encode())
    assert result.pe_ttm.iloc[0] is None
    assert result.pb.iloc[0] == -3


@pytest.mark.parametrize(
    "bad",
    [
        "date",
        "duplicate_row",
        "duplicate_field",
        "short_row",
        "code",
        "empty",
        "error",
        "numeric_string",
        "bool",
        "has_more",
        "total",
    ],
)
def test_invalid_or_incomplete_provider_envelope_rejected(bad):
    obj = wire()
    data = obj["data"]
    if bad == "date":
        data["items"][0][1] = "20260910"
    elif bad == "duplicate_row":
        data["items"].append(data["items"][0][:])
    elif bad == "duplicate_field":
        data["fields"][3] = "pe_ttm"
    elif bad == "short_row":
        data["items"][0].pop()
    elif bad == "code":
        data["items"][0][0] = "bad"
    elif bad == "empty":
        data["items"] = []
    elif bad == "error":
        obj["code"] = 2002
    elif bad == "numeric_string":
        data["items"][0][2] = "10"
    elif bad == "bool":
        data["items"][0][2] = True
    elif bad == "has_more":
        data["has_more"] = True
    else:
        data["total"] = 2
    with pytest.raises(DataValidationError):
        parse_snapshot(json.dumps(obj).encode())


@pytest.mark.parametrize(
    "created,received",
    [
        (datetime(2026, 9, 12, 10, tzinfo=UTC), datetime(2026, 9, 12, 9, tzinfo=UTC)),
        (datetime(2026, 9, 14, 2, tzinfo=UTC), datetime(2026, 9, 13, tzinfo=UTC)),
        (datetime(2026, 9, 13), datetime(2026, 9, 12, tzinfo=UTC)),
        (datetime(2026, 9, 13, tzinfo=UTC), datetime(2026, 9, 13, 1, tzinfo=UTC)),
        (datetime(2026, 9, 13, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)),
    ],
)
def test_invalid_registration_timing(created, received):
    with pytest.raises(DataValidationError):
        validate_registration(created, received)


def test_valid_registration_is_actual_weekend_not_price_day():
    validate_registration(
        datetime(2026, 9, 13, 3, tzinfo=UTC), datetime(2026, 9, 12, 17, 36, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("execution_authority", True),
        ("performance_evidence", True),
        ("historical_pit_certified", True),
        ("exit_subsequent_sessions", 5),
        ("future_calendar_complete", True),
        ("future_exit_date", "2026-10-15"),
        ("economic_paths", 1),
        ("model_fits", 1),
        ("provider_requests", 1),
        ("rule_candidates_after", 3),
        ("minimum_future_pairs", 1),
    ],
)
def test_unregistered_scope_or_budget_rejected(key, value):
    root = Path(__file__).resolve().parents[2]
    c = copy.deepcopy(json.loads((root / CONFIG).read_text()))
    c[key] = value
    with pytest.raises(DataValidationError):
        validate_config(c)


def test_positive_finite_rejects_huge_unrepresentable_int():
    assert not positive_finite(10**1000)


@pytest.mark.parametrize("bad", ["source", "omit_input", "resource"])
def test_exact_input_and_resource_contract(bad):
    root = Path(__file__).resolve().parents[2]
    c = json.loads((root / CONFIG).read_text())
    if bad == "source":
        c["raw_path"] = "unapproved.body"
    elif bad == "omit_input":
        c["inputs"].pop("data/canonical/calendar/calendar.parquet")
    else:
        c["resources"]["max_wakeup_seconds"] = 900
    with pytest.raises(DataValidationError):
        validate_config(c)


@pytest.mark.parametrize("state", [None, pd.NA, 1, "True", False])
def test_unknown_or_nonboolean_calendar_does_not_become_open(state):
    cal = pd.DataFrame(
        {"trade_date": ["2026-09-14"] * 2, "exchange": ["SSE", "SZSE"], "is_open": [True, state]},
        dtype=object,
    )
    with pytest.raises(DataValidationError):
        validate_entry_calendar(cal)


def test_entry_calendar_requires_both_exchanges_and_unique_date():
    cal = pd.DataFrame(
        {"trade_date": ["2026-09-14"] * 2, "exchange": ["SSE", "SZSE"], "is_open": [True, True]}
    )
    validate_entry_calendar(cal)
    with pytest.raises(DataValidationError):
        validate_entry_calendar(cal.iloc[:1])
    with pytest.raises(DataValidationError):
        validate_entry_calendar(pd.concat([cal, cal.iloc[:1]]))
