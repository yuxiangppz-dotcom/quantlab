"""Synthetic, hand-checkable D-open observations and frozen-version aggregation."""

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from quantlab.data.models import AdjFactor, DailyBar, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import fingerprint
from quantlab.scout.tracking import observe_run, summarize_tracking


def _market(root):
    storage = ParquetStorage(root)
    days = [date(2026, 1, 5) + timedelta(days=offset) for offset in range(18)]
    days = [day for day in days if day.weekday() < 5]
    storage.save_trading_calendar([TradingCalendar("SSE", day, True) for day in days])
    for index, day in enumerate(days):
        # A rises; B falls. The factor change on A after D preserves an adjusted
        # sequence despite a halving of its raw quote.
        a_factor = 2.0 if index >= 4 else 1.0
        a_close = (10.0 + index) / a_factor
        b_close = 10.0 - index * 0.3
        storage.save_daily_bars_by_date(
            [
                DailyBar(
                    "000001.SZ",
                    day,
                    10.0 if index == 1 else a_close,
                    a_close,
                    a_close,
                    a_close,
                    10.0,
                    1000.0,
                    10000.0,
                ),
                DailyBar(
                    "000002.SZ", day, b_close, b_close, b_close, b_close, 10.0, 1000.0, 10000.0
                ),
            ],
            day,
        )
        storage.save_adj_factors_by_date(
            [AdjFactor("000001.SZ", day, a_factor), AdjFactor("000002.SZ", day, 1.0)],
            day,
        )
    return days


def _run(root, name, target, finished, selected, primary=True):
    report = {
        "run_id": name,
        "status": "live_research_unvalidated",
        "finished_at": finished,
        "timing": {
            "target_session": target.isoformat(),
            "generated_at": finished,
            "report_kind": "next_session_prep",
            "primary_eligible": primary,
        },
        "selection": {"selected": selected},
    }
    run = root / name
    run.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    return run


def _observe_at(run, canonical, marks, at):
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at

    with patch("quantlab.scout.tracking.datetime", FrozenClock):
        path = observe_run(run, canonical, marks)
    return path, json.loads(path.read_text())


def test_d_is_h1_gap_and_adjusted_open_to_close(tmp_path):
    canonical = tmp_path / "canonical"
    days = _market(canonical)
    run = _run(
        tmp_path / "runs",
        "first",
        days[1],
        f"{days[0]}T20:00:00+08:00",
        [{"instrument_id": "000001.SZ", "status": "focus"}],
    )
    marks = tmp_path / "marks"
    at = datetime.fromisoformat(f"{days[10]}T19:00:00+08:00")
    path, result = _observe_at(run, canonical, marks, at)
    same_path, _ = _observe_at(run, canonical, marks, at)
    assert path == same_path
    assert len(list(marks.glob("*.json"))) == 1
    rows = result["rows"]
    assert [row["end_session"] for row in rows] == [
        days[index].isoformat() for index in (1, 3, 5, 10)
    ]
    assert rows[0]["d_open_gap_adjusted"] == pytest.approx(0)
    assert rows[0]["d_open_to_end_close_adjusted"] == pytest.approx(0.1)
    assert rows[1]["d_open_to_end_close_adjusted"] == pytest.approx(0.3)
    assert rows[2]["company_action_status"] == "adjustment_factor_changed"
    assert rows[3]["d_open_to_end_close_adjusted"] == pytest.approx(1.0)


def test_pending_missing_d_open_and_late_report_are_not_zero(tmp_path):
    canonical = tmp_path / "canonical"
    days = _market(canonical)
    run_root = tmp_path / "runs"
    run_root.mkdir()
    run = _run(
        run_root,
        "pending",
        days[1],
        f"{days[0]}T20:00:00+08:00",
        [{"instrument_id": "000001.SZ", "status": "focus"}],
    )
    path, item = _observe_at(
        run, canonical, tmp_path / "marks", datetime.fromisoformat(f"{days[1]}T12:00:00+08:00")
    )
    assert path.exists()
    assert all(row["status"] == "d_open_not_yet_due" for row in item["rows"])
    assert all(row["d_open_to_end_close_adjusted"] is None for row in item["rows"])

    storage = ParquetStorage(canonical)
    storage.daily_bars_path(days[1]).unlink()
    _, item = _observe_at(
        run, canonical, tmp_path / "marks2", datetime.fromisoformat(f"{days[10]}T19:00:00+08:00")
    )
    assert all(row["status"] == "d_open_partition_missing" for row in item["rows"])
    late = _run(
        run_root,
        "late",
        days[1],
        f"{days[1]}T10:00:00+08:00",
        [{"instrument_id": "000002.SZ", "status": "focus"}],
    )
    _, late_item = _observe_at(
        late, canonical, tmp_path / "marks2", datetime.fromisoformat(f"{days[10]}T19:00:00+08:00")
    )
    assert late_item["primary_eligibility"] == "finished_after_target_open"
    assert late_item["rows"] == []


def test_latest_preopen_version_and_watch_are_separate(tmp_path):
    canonical = tmp_path / "canonical"
    days = _market(canonical)
    run_root = tmp_path / "runs"
    run_root.mkdir()
    old = _run(
        run_root,
        "old",
        days[1],
        f"{days[0]}T20:00:00+08:00",
        [{"instrument_id": "000001.SZ", "status": "focus"}],
    )
    new = _run(
        run_root,
        "new",
        days[1],
        f"{days[1]}T08:00:00+08:00",
        [
            {"instrument_id": "000002.SZ", "status": "focus"},
            {"instrument_id": "000001.SZ", "status": "watch"},
        ],
    )
    marks = tmp_path / "marks"
    at = datetime.fromisoformat(f"{days[10]}T19:00:00+08:00")
    _observe_at(old, canonical, marks, at)
    _observe_at(new, canonical, marks, at)
    output = summarize_tracking(run_root, marks, tmp_path / "summary.json")
    item = json.loads(output.read_text())
    assert item["covered_report_dates"] == 1
    assert item["effective_reports"][0]["run_id"] == "new"
    assert item["summary"]["focus"]["1"]["original_candidates"] == 1
    assert item["summary"]["focus"]["1"]["positive"] == 0
    assert item["summary"]["watch"]["1"]["original_candidates"] == 1
    assert item["summary"]["watch"]["1"]["positive"] == 1


def test_newer_version_without_observation_does_not_fall_back_to_old_winner(tmp_path):
    canonical = tmp_path / "canonical"
    days = _market(canonical)
    run_root = tmp_path / "runs"
    run_root.mkdir()
    old = _run(
        run_root,
        "old",
        days[1],
        f"{days[0]}T20:00:00+08:00",
        [{"instrument_id": "000001.SZ", "status": "focus"}],
    )
    _run(
        run_root,
        "new",
        days[1],
        f"{days[1]}T08:00:00+08:00",
        [{"instrument_id": "000002.SZ", "status": "focus"}],
    )
    marks = tmp_path / "marks"
    _observe_at(old, canonical, marks, datetime.fromisoformat(f"{days[10]}T19:00:00+08:00"))
    output = summarize_tracking(run_root, marks, tmp_path / "summary.json")
    item = json.loads(output.read_text())
    assert item["effective_reports"][0]["run_id"] == "new"
    assert item["summary"]["focus"]["1"]["calculable"] == 0
    assert item["summary"]["focus"]["1"]["missing_reasons"] == {"observation_not_run": 1}
