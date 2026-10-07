"""Popularity snapshots preserve their retrieval context and truncation limits."""

import json
from datetime import datetime, timedelta

import pytest

from quantlab.scout.hot import collect_hot_rank, normalize_hot_rank, save_hot_snapshot
from quantlab.scout.models import SHANGHAI

NOW = datetime(2026, 9, 30, 8, 30, tzinfo=SHANGHAI)


def row(rank: int, code: str, name: str = "样本") -> dict:
    return {
        "当前排名": rank,
        "代码": code,
        "股票名称": name,
        "最新价": 10.5,
        "涨跌额": 0.3,
        "涨跌幅": 2.94,
    }


def test_first_snapshot_has_unknown_delta_and_preserves_raw_fields():
    raw = [row(1, "SZ000001") | {"new_provider_column": "unmodified"}]
    snapshot, coverage = normalize_hot_rank(raw, NOW)
    assert coverage.status == "partial"  # This fixture is not a complete top 100.
    assert snapshot["raw_rows"] == raw
    assert snapshot["data_asof"] is None
    assert snapshot["retrieved_at"] == NOW.isoformat()
    assert snapshot["normalized"][0] == {
        "instrument_id": "000001.SZ",
        "provider_code": "SZ000001",
        "name": "样本",
        "rank": 1,
        "last_price": 10.5,
        "price_change": 0.3,
        "price_change_pct": 2.94,
        "previous_rank": None,
        "rank_delta": None,
        "change_status": "unknown_no_previous_snapshot",
    }


def test_only_overlapping_compatible_top100_members_get_rank_delta():
    previous, _ = normalize_hot_rank(
        [row(5, "SH600000"), row(9, "SZ000001")], NOW - timedelta(days=1)
    )
    current, _ = normalize_hot_rank([row(2, "SH600000"), row(3, "SZ000002")], NOW, previous)
    assert current["comparison_status"] == "compatible"
    assert current["previous_retrieved_at"] == previous["retrieved_at"]
    assert current["normalized"][0]["rank_delta"] == 3
    assert current["normalized"][0]["previous_rank"] == 5
    assert current["normalized"][1]["rank_delta"] is None
    assert current["normalized"][1]["previous_rank"] is None
    assert current["normalized"][1]["change_status"] == "unknown_outside_previous_top100"


def test_incompatible_or_future_snapshot_never_backfills_movement():
    previous, _ = normalize_hot_rank([row(10, "SZ000001")], NOW + timedelta(hours=1))
    current, _ = normalize_hot_rank([row(1, "SZ000001")], NOW, previous)
    assert current["normalized"][0]["rank_delta"] is None
    assert current["normalized"][0]["change_status"] == "unknown_incompatible_previous_snapshot"
    previous["retrieved_at"] = (NOW - timedelta(days=8)).isoformat()
    current, _ = normalize_hot_rank([row(1, "SZ000001")], NOW, previous)
    assert current["normalized"][0]["rank_delta"] is None


def test_invalid_codes_duplicates_and_provider_drift_are_partial():
    snapshot, coverage = normalize_hot_rank(
        [
            row(1, "SZ000001"),
            row(1, "SH600000"),
            row(2, "000002"),
            row(3, "SZ000001"),
            row(101, "SH600001"),
            row(4, "BJ430001"),
        ],
        NOW,
    )
    assert coverage.status == "partial"
    assert coverage.count == 2
    assert [item["instrument_id"] for item in snapshot["normalized"]] == [
        "000001.SZ",
        "430001.BJ",
    ]
    assert len(snapshot["raw_rows"]) == 6


def test_optional_feed_failure_and_offline_mode_are_not_zero_rank():
    calls = []

    def fail():
        calls.append(1)
        raise RuntimeError("private provider detail")

    snapshot, coverage = collect_hot_rank(NOW, False, fetcher=fail)
    assert snapshot is None and coverage.status == "disabled" and not calls
    snapshot, coverage = collect_hot_rank(NOW, True, fetcher=fail)
    assert snapshot is None and coverage.status == "failed"
    assert coverage.detail == "RuntimeError" and len(calls) == 1
    snapshot, coverage = collect_hot_rank(NOW, True, fetcher=lambda: [])
    assert snapshot is None and coverage.status == "failed"


def test_immutable_snapshot_file(tmp_path):
    snapshot, _ = normalize_hot_rank([row(1, "SZ000001")], NOW)
    path = tmp_path / "snapshots" / "first.json"
    save_hot_snapshot(path, snapshot)
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot
    with pytest.raises(FileExistsError):
        save_hot_snapshot(path, snapshot)
