import json
from copy import deepcopy
from datetime import date, datetime

import pandas as pd
import pytest

from quantlab.scout import article_pipeline
from quantlab.scout.article_data import extend_context

CODE = "600001.SH"
SIGNAL = date(2026, 9, 30)
TARGET = date(2026, 10, 8)
SESSIONS = [
    SIGNAL,
    TARGET,
    date(2026, 10, 9),
    date(2026, 10, 12),
    date(2026, 10, 13),
    date(2026, 10, 14),
]


def clock(monkeypatch, value):
    current = datetime.fromisoformat(value)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr(article_pipeline, "datetime", FixedDatetime)


def partition(root, table, current, rows):
    path = (
        root
        / "market"
        / table
        / f"year={current.year}"
        / f"month={current.month:02}"
        / f"{current}.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def fixture(tmp_path, *, limits=True):
    root = tmp_path / "scout_article_tracking_offline"
    root.mkdir()
    (root / ".scout-article").write_text("quantlab-scout-article-v1", encoding="utf-8")
    market = root / "market"
    (market / "calendar").mkdir(parents=True)
    pd.DataFrame(
        [{"exchange": "SSE", "trade_date": day, "is_open": True} for day in SESSIONS]
    ).to_parquet(market / "calendar" / "calendar.parquet")
    (market / "securities").mkdir()
    pd.DataFrame(
        [
            {
                "instrument_id": CODE,
                "name": "当前名称不代表历史资格",
                "list_status": "L",
                "list_date": "20000101",
            }
        ]
    ).to_parquet(market / "securities" / "securities.parquet")
    for current in SESSIONS[:2]:
        identity = {"instrument_id": CODE, "trade_date": current}
        partition(
            root,
            "daily",
            current,
            [
                identity
                | {
                    "open": 10 if current == SIGNAL else 10.8,
                    "high": 11.5,
                    "low": 9.8,
                    "close": 10 if current == SIGNAL else 11,
                    "volume": 100000,
                    "amount": 1000000,
                }
            ],
        )
        partition(root, "adj_factor", current, [identity | {"adj_factor": 1}])
        if limits or current == SIGNAL:
            partition(
                root, "daily_price_limit", current, [identity | {"up_limit": 12, "down_limit": 9}]
            )
    report = {
        "run_id": "article-fixed-before-target",
        "strategy_version": "article_v1",
        "config_hash": "frozen-test-config",
        "signal_date": str(SIGNAL),
        "target_date": str(TARGET),
        "candidates": [
            {
                "ts_code": CODE,
                "name": "冻结名称",
                "route": "base_breakout",
                "final_status": "priority_candidate",
                "price": {
                    "entry_low": 10.5,
                    "entry_high": 11.2,
                    "invalidation": 9.9,
                    "status": "valid",
                },
            }
        ],
    }
    return root, report


def state(
    root,
    *,
    day=TARGET,
    known="2026-10-08T08:00:00+08:00",
    st=False,
    suspended=False,
    delisting=False,
):
    context = {
        "security_by_code": {
            CODE: {
                "is_st": st,
                "is_suspended": suspended,
                "is_delisting": delisting,
                "status_by_date": {str(day): {"is_st": st, "is_delisting": delisting}},
                "suspended_by_date": {str(day): suspended},
            }
        },
        "group_snapshots": [],
    }
    return extend_context(root, context, day, known)


def observed(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_repeat_tracking_reads_real_observed_at_and_nested_path(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    state(root)
    before = deepcopy(report)
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    first_path = article_pipeline.track_article(root, report)
    first = observed(first_path)
    clock(monkeypatch, "2026-10-08T18:31:00+08:00")
    second_path = article_pipeline.track_article(root, report)
    second = observed(second_path)
    assert first_path.parent == root / "article_observations" / report["run_id"] / "observations"
    assert second["previous_sha256"] == first["sha256"]
    assert second["observed_at"] == "2026-10-08T18:31:00+08:00"
    assert "asof" not in second
    assert second["rows"][0]["entry_check"] == "pass"
    assert second["rows"][0]["entry_observation"] == first["rows"][0]["entry_observation"]
    assert report == before
    assert first_path.exists() and len(list(first_path.parent.glob("*.json"))) == 2


def test_same_timestamp_recovery_follows_terminal_chain(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    state(root)
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    paths = [article_pipeline.track_article(root, report) for _ in range(4)]
    records = [observed(path) for path in paths]
    assert len(set(paths)) == 4
    for prior, current in zip(records[:-1], records[1:], strict=True):
        assert current["previous_sha256"] == prior["sha256"]
        assert current["rows"][0]["entry_check"] == "pass"


def test_daily_bar_and_current_master_never_manufacture_tradability(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    result = observed(article_pipeline.track_article(root, report))
    entry = result["rows"][0]["entry_observation"]
    assert entry["entry_check"] == "unobservable" and entry["frozen"] is False
    assert entry["reason_code"] == "target_open_status_or_limits_missing"
    assert result["rows"][0]["windows"]["1"]["status"] == "complete"
    assert result["rows"][0]["windows"]["3"]["status"] == "pending"
    assert result["rows"][0]["main_price_change"] is None


@pytest.mark.parametrize("unknown", ["st", "suspended", "delisting"])
def test_any_unknown_qualification_stays_unknown(tmp_path, monkeypatch, unknown):
    root, report = fixture(tmp_path)
    state(root, **{unknown: None})
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    result = observed(article_pipeline.track_article(root, report))
    assert result["rows"][0]["entry_check"] == "unobservable"
    assert result["open_reference_pass_subset"]["ids"] == []


@pytest.mark.parametrize("restriction", ["st", "suspended", "delisting"])
def test_actual_target_restriction_can_fail_without_daily_or_limits(
    tmp_path, monkeypatch, restriction
):
    root, report = fixture(tmp_path, limits=False)
    state(root, st=None, suspended=None, delisting=None)
    # A separately frozen later target snapshot contains a real single constraint.
    flags = {"st": None, "suspended": None, "delisting": None, restriction: True}
    state(root, known="2026-10-08T08:01:00+08:00", **flags)
    target_daily = root / "market" / "daily" / "year=2026" / "month=10" / "2026-10-08.parquet"
    target_daily.unlink()
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    result = observed(article_pipeline.track_article(root, report))
    entry = result["rows"][0]["entry_observation"]
    assert entry["entry_check"] == "fail" and entry["frozen"] is True
    assert entry["reason_code"] == "target_not_tradable"
    assert entry["status_receipt"]["status"] == restriction
    assert (
        entry["status_receipt"]["qualification_flags"][
            "is_" + ("suspended" if restriction == "suspended" else restriction)
        ]
        is True
    )


def test_cross_day_limit_supplement_can_resolve_unfrozen_entry(tmp_path, monkeypatch):
    root, report = fixture(tmp_path, limits=False)
    state(root)
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    first = observed(article_pipeline.track_article(root, report))
    assert first["rows"][0]["entry_check"] == "unobservable"
    assert first["rows"][0]["entry_observation"]["frozen"] is False
    partition(
        root,
        "daily_price_limit",
        TARGET,
        [{"instrument_id": CODE, "trade_date": TARGET, "up_limit": 12, "down_limit": 9}],
    )
    clock(monkeypatch, "2026-10-09T18:30:00+08:00")
    second = observed(article_pipeline.track_article(root, report))
    assert second["rows"][0]["entry_check"] == "pass"
    assert second["rows"][0]["entry_observation"]["inputs"]["up_limit"] == 12
    assert second["rows"][0]["windows"]["2"]["status"] == "missing"
    assert second["rows"][0]["windows"]["3"]["status"] == "pending"
    assert second["previous_sha256"] == first["sha256"]


def test_current_and_late_target_context_cannot_clear_historical_unknown(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    state(root, day=date(2026, 10, 9), known="2026-10-09T08:00:00+08:00")
    state(root, day=TARGET, known="2026-10-09T08:05:00+08:00")
    clock(monkeypatch, "2026-10-09T18:30:00+08:00")
    result = observed(article_pipeline.track_article(root, report))
    assert result["rows"][0]["entry_check"] == "unobservable"
    assert result["rows"][0]["entry_observation"]["frozen"] is False


def test_future_context_and_future_open_remain_pending(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    state(root)
    clock(monkeypatch, "2026-09-30T20:00:00+08:00")
    result = observed(article_pipeline.track_article(root, report))
    assert result["rows"][0]["entry_check"] == "pending"
    assert result["rows"][0]["d1_gap"] is None
    assert all(window["status"] == "pending" for window in result["rows"][0]["windows"].values())


def test_equal_time_context_conflict_never_selects_favorable_status(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    state(root)
    state(root, suspended=True)
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    result = observed(article_pipeline.track_article(root, report))
    assert result["rows"][0]["entry_check"] == "unobservable"


def test_later_qualification_does_not_overwrite_frozen_entry(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    state(root)
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    first = observed(article_pipeline.track_article(root, report))
    state(root, known="2026-10-08T19:00:00+08:00", suspended=True)
    clock(monkeypatch, "2026-10-08T19:05:00+08:00")
    second = observed(article_pipeline.track_article(root, report))
    assert second["rows"][0]["entry_check"] == "pass"
    assert second["rows"][0]["entry_observation"] == first["rows"][0]["entry_observation"]
    assert second["rows"][0]["latest_entry_evidence_diagnostic"]["entry_check"] == "fail"
    assert second["rows"][0]["entry_evidence_changed_since_freeze"] is True


def test_context_integrity_failure_stops_without_rewriting_existing_snapshot(tmp_path, monkeypatch):
    root, report = fixture(tmp_path)
    saved = state(root)
    path = root / saved["context_history"]["saved_snapshot"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["security_states"][CODE]["status"]["is_st"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    clock(monkeypatch, "2026-10-08T18:30:00+08:00")
    with pytest.raises(ValueError, match="integrity"):
        article_pipeline.track_article(root, report)
    assert not (root / "article_observations").exists()
    assert json.loads(path.read_text())["security_states"][CODE]["status"]["is_st"] is True
