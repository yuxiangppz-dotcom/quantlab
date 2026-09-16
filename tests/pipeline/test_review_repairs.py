"""Regression coverage for real integration boundaries, using synthetic source facts."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError, Security, SecurityCodeChange
from quantlab.pipeline import features
from quantlab.pipeline.market import close_market_open
from quantlab.research.alpha158_native import map_inputs


@pytest.mark.parametrize(
    "facts,bar,expected",
    [
        ([], True, True),
        ([], False, None),
        ([("R", None)], True, True),
        ([("R", None)], False, None),
        ([("S", None)], True, False),
        ([("S", "09:30-10:00")], True, True),
        ([("S", "14:30-15:00")], True, False),
        ([("S", "09:30-10:00,14:00-15:00")], True, False),
        ([("S", "09:30-10:00"), ("R", None)], True, True),
        ([("S", None), ("R", None)], True, None),
        ([("?", None)], True, None),
        ([("S", "unknown")], True, None),
        ([("S", "25:00-26:00")], True, None),
        ([("S", "15:00-09:00")], True, None),
        ([("S", "09:30-10:00")], False, None),
    ],
)
def test_close_trade_state_uses_event_type_and_time(facts, bar, expected):
    records = [SimpleNamespace(suspend_type=kind, suspend_timing=time) for kind, time in facts]
    assert close_market_open(object() if bar else None, records) is expected


def test_native_bridge_uses_dated_old_code_without_stitching(monkeypatch, tmp_path):
    old, new = "000022.SZ", "001872.SZ"
    sessions = pd.bdate_range("2018-12-24", periods=5)
    security = Security(
        new, "001872", "synthetic", "SZSE", "SZ", "主板", "L", date(2018, 12, 26), None
    )
    change = SecurityCodeChange(old, new, date(2018, 12, 26), "old", date(1993, 5, 5))
    raw = pd.DataFrame(
        [
            dict(
                instrument_id=code,
                trade_date=day,
                open=10,
                high=11,
                low=9,
                close=10,
                volume=1000,
                amount=10000,
                adj_factor=1,
            )
            for code in (old, new)
            for day in sessions
        ]
    )
    captured = []

    def capture(*args):
        mapped, evidence = map_inputs(*args)
        captured.append(evidence)
        return mapped, evidence

    monkeypatch.setattr(features, "map_inputs", capture)
    monkeypatch.setattr(features, "write_binary_cache", lambda *args: None)
    monkeypatch.setattr(features, "evaluate_native", lambda mapped, *args: mapped)
    monkeypatch.setattr(features, "apply_evidence_mask", lambda native, *args: (native, None))
    features.native_features(raw, sessions, [security], {}, tmp_path, code_changes=[change])
    evidence = captured[0]
    before = evidence.trade_date.lt(pd.Timestamp(change.effective_date))
    assert evidence.loc[evidence.instrument_id.eq(old), "lifecycle_active"].tolist() == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert evidence.loc[evidence.instrument_id.eq(new), "lifecycle_active"].tolist() == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert not evidence.loc[before & evidence.instrument_id.eq(new), "price_inputs_valid"].any()
    with pytest.raises(DataValidationError, match="unknown native audit lifecycle"):
        features.native_features(raw, sessions, [security], {}, tmp_path)
    # A successor's later real delisting must also remain effective.
    features.native_features(
        raw,
        sessions,
        [replace(security, delist_date=date(2018, 12, 27))],
        {},
        tmp_path,
        code_changes=[change],
    )
    assert not captured[-1].query("instrument_id == @new").iloc[-1].lifecycle_active
