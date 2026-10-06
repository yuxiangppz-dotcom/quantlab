"""Synthetic replay of new labels and automatic missing-data recovery, no network."""

import json
from datetime import date, datetime

import pytest

from quantlab.data.models import AdjFactor, DailyBar, DailyPriceLimit, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, Candidate, fingerprint
from quantlab.scout.nextday_tracking import (
    VERSION,
    _eligibility,
    freeze_protocol,
    observe_nextday,
    publication_receipt,
    summarize_nextday,
)
from quantlab.scout.observation_runtime import scan_pending


@pytest.fixture
def sample(tmp_path):
    data, runs, output = tmp_path / "data", tmp_path / "runs", tmp_path / "marks"
    storage = ParquetStorage(data)
    days = [date(2026, 10, d) for d in (8, 9, 12, 13, 14)]
    storage.save_trading_calendar([TradingCalendar("SSE", d, True) for d in days])
    codes = ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ"]
    universe = {c: Candidate(c, c, {"close": 10, "up_limit": 11}, i) for i, c in enumerate(codes)}
    rows = [
        {
            "instrument_id": c,
            "rank": i,
            "final_status": "focus" if i == 1 else "watch",
            "primary_type": "event_update" if i == 1 else "trend_continuation",
        }
        for i, c in enumerate(codes[:4], 1)
    ]
    packet = {
        "timing": {
            "target_session": days[0].isoformat(),
            "primary_eligible": True,
            "publication_deadline": "2026-10-08T09:00:00+08:00",
        },
        "candidates": [{"instrument_id": c, "source_summary": {}} for c in codes[:4]],
        "coverage": [],
    }
    report = {
        "run_id": "synthetic-nextday",
        "status": "demo",
        "synthetic": True,
        "timing": packet["timing"],
        "finished_at": "2026-10-08T08:30:00+08:00",
        "selection_input_packet": packet,
        "nextday_freeze": freeze_protocol(
            rows,
            packet,
            universe,
            {c: "I" for c in codes},
            {"recalled_candidates": codes[:4], "shortlist_candidates": codes[:4]},
        ),
    }
    run = runs / report["run_id"]
    run.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    return storage, days, codes, run, report, output


def fill(sample, omit=None):
    storage, days, codes, _, _, _ = sample
    for offset, d in enumerate(days):
        bars = []
        for i, c in enumerate(codes):
            if (d, c) == omit:
                continue
            # Ex-right raw prices halve. Official reference is 5, not previous raw 10.
            reference = 5.0
            opening = 5.4 if i == 0 else 5.0
            closing = 5.25 if i == 0 else 5.05 + i * 0.05 + offset * 0.01
            bars.append(
                DailyBar(
                    c,
                    d,
                    opening,
                    max(opening, closing) + 0.1,
                    min(opening, closing) - 0.1,
                    closing,
                    reference,
                    1000,
                    5000,
                )
            )
        storage.save_daily_bars_by_date(bars, d)
        storage.save_adj_factors_by_date([AdjFactor(c, d, 2.0) for c in codes], d)
        storage.save_daily_price_limits_by_date(
            [DailyPriceLimit(c, d, 5, 5.5, 4.5, "SZSE", "provider", c) for c in codes], d
        )


def test_official_reference_exright_strongclose_gap_and_t1_proxy(sample):
    fill(sample)
    storage, _, _, run, _, output = sample
    path = observe_nextday(
        run,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 14, 19, tzinfo=SHANGHAI),
    )
    item = json.loads(path.read_text())
    row = item["rows"][0]
    assert row["d1_official_return"] == pytest.approx(0.05)
    assert row["strong_close"] is True
    assert row["d1_open_gap"] == pytest.approx(0.08)
    assert row["d1_open_to_close"] < 0
    assert row["auxiliary"][0]["horizon_sessions"] == 2
    assert row["auxiliary"][0]["end_session"] == "2026-10-09"
    assert row["auxiliary"][0]["adjusted_price_return"] == pytest.approx(5.25 / 5.4 - 1)
    assert item["controls"]["k"] == 1  # watch does not inflate primary k


def test_rule_observations_preserve_unknown_and_actual_version(sample):
    fill(sample)
    storage, _, _, run, report, output = sample
    report["release_identity"] = {
        "engine_commit": "engine-sha",
        "app_commit": "app-sha",
        "config_sha256": "sealed-sha",
    }
    report["ai_provider"], report["ai_model"] = "provider", "model"
    report["config_sha256"] = "runtime-fingerprint"
    rows = report["nextday_freeze"]["rows"]
    rows[0]["invalidation_rule"] = {"rule_id": "relative_d1_nonpositive_v1"}
    rows[0]["participation_cancel_rule"] = {"rule_id": "target_open_limit_or_halt_v1"}
    rows[1]["invalidation_rule"] = {"rule_id": "ma20_break_v1"}
    rows[2]["invalidation_rule"] = {"rule_id": "event_cancelled_d1_v1"}
    report["nextday_freeze"]["sha256"] = fingerprint(
        {k: v for k, v in report["nextday_freeze"].items() if k != "sha256"}
    )
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    path = observe_nextday(
        run,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 14, 19, tzinfo=SHANGHAI),
    )
    item = json.loads(path.read_text())
    assert item["rows"][0]["research_rule_observation"]["invalidated"] is False
    assert item["rows"][0]["participation_cancel_observation"]["cancelled"] is False
    assert item["rows"][1]["research_rule_observation"]["invalidated"] is None
    assert (
        item["rows"][2]["research_rule_observation"]["status"] == "official_event_review_required"
    )
    identity = item["version_identity"]
    assert identity["provider"] == "provider" and identity["model"] == "model"
    assert identity["installed_config_sha256"] == "sealed-sha"
    assert identity["runtime_config_fingerprint"] == "runtime-fingerprint"
    assert item["controls"]["groups"]["score_top_k"]["stock_ids"] == ["000004.SZ"]
    assert item["attribution"]["eligible_original"] == 5
    assert item["attribution"]["reason_counts"]["not_recalled"] == 1


def test_scanner_pending_repeat_crossdays_and_missing_recovery(sample):
    storage, days, codes, run, _, output = sample
    original = (run / "report.json").read_bytes()
    runs = run.parent
    early = scan_pending(
        runs,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 8, 16, tzinfo=SHANGHAI),
    )
    early_bytes = (output / "nextday" / early["rows"][0]["snapshot"]).read_bytes()
    repeat = scan_pending(
        runs,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 8, 17, tzinfo=SHANGHAI),
    )
    assert repeat["receipt"] == early["receipt"]
    assert repeat["model_calls"] == repeat["provider_calls"] == 0
    fill(sample, (days[-1], codes[0]))
    partial = scan_pending(
        runs,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 14, 19, tzinfo=SHANGHAI),
    )
    assert partial["rows"][0]["maturity"] == "pending_or_incomplete"
    fill(sample)
    ready = scan_pending(
        runs,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 15, 8, tzinfo=SHANGHAI),
    )
    assert ready["rows"][0]["maturity"] == "complete"
    assert ready["rows"][0]["snapshot"] != partial["rows"][0]["snapshot"]
    assert (output / "nextday" / early["rows"][0]["snapshot"]).read_bytes() == early_bytes
    assert (run / "report.json").read_bytes() == original
    assert ready["nextday_summary"]["formal_target_days"] == 0


def test_missing_reference_and_adjustment_are_separate_labels(sample):
    fill(sample)
    storage, days, codes, run, _, output = sample
    # D1 can be labelled without adj factors; H3/H5 proxy cannot be fabricated.
    storage.save_adj_factors_by_date([], days[0])
    path = observe_nextday(
        run,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 14, 19, tzinfo=SHANGHAI),
    )
    row = json.loads(path.read_text())["rows"][0]
    assert row["strong_close"] is True
    assert row["auxiliary"][0]["adjusted_price_return"] is None
    assert row["auxiliary"][0]["status"] == "factor_missing_or_invalid"
    bars = storage.load_daily_bars_by_date(days[0])
    first = bars[0]
    storage.save_daily_bars_by_date(
        [
            DailyBar(
                first.instrument_id,
                first.trade_date,
                first.open,
                first.high,
                first.low,
                first.close,
                0,
                first.volume,
                first.amount,
            ),
            *bars[1:],
        ],
        days[0],
    )
    path2 = observe_nextday(
        run,
        storage.base_dir,
        output,
        allow_synthetic=True,
        observed_at=datetime(2026, 10, 14, 19, tzinfo=SHANGHAI),
    )
    row2 = json.loads(path2.read_text())["rows"][0]
    assert row2["strong_close"] is None
    assert row2["d1_status"] == "official_reference_missing_or_invalid"


def test_summary_abstention_unknown_denominators_and_duplicate_guard():
    base = {
        "run_id": "a",
        "definition_version": VERSION,
        "primary_eligibility": "eligible",
        "target_session": "2026-10-08",
        "synthetic": False,
        "controls": {
            "groups": {
                "focus": {
                    "original_count": 0,
                    "observable_count": 0,
                    "unknown_count": 0,
                    "hit_count": 0,
                    "strong_close_hit_rate": None,
                }
            },
            "paired_complete": False,
        },
    }
    result = summarize_nextday([base])
    assert result["abstention_days"] == 1 and result["recommendation_coverage"] == 0
    assert result["stock_weighted_hit_rate"] is None
    calendar = summarize_nextday([base], eligible_target_sessions=["2026-10-08", "2026-10-09"])
    assert calendar["coverage_day_denominator"] == 2
    assert calendar["missing_formal_target_sessions"] == ["2026-10-09"]
    with pytest.raises(ValueError, match="Multiple formal"):
        summarize_nextday([base, {**base, "run_id": "b"}])


def test_late_cloud_publication_overrides_local_and_missing_is_unknown(sample):
    _, days, _, run, report, _ = sample
    actual = {**report, "synthetic": False, "status": "complete"}
    digest = fingerprint(actual)
    freeze = actual["nextday_freeze"]
    empty = publication_receipt(run, actual, digest)
    assert _eligibility(actual, freeze, days, empty)[1] == "publication_unknown"
    receipt = {
        "run_id": actual["run_id"],
        "source_report_sha256": digest,
        "status": "local_saved",
        "published_at": "2026-10-08T08:59:00+08:00",
    }
    (run / "publication.json").write_text(json.dumps(receipt))
    assert (
        _eligibility(actual, freeze, days, publication_receipt(run, actual, digest))[1]
        == "eligible"
    )
    cloud_report = {**actual, "release_identity": {"delivery_channel": "cloud"}}
    assert publication_receipt(run, cloud_report, fingerprint(cloud_report))["status"] == "unknown"
    cloud = {**receipt, "status": "published", "published_at": "2026-10-08T09:00:01+08:00"}
    (run / "cloud-publication.json").write_text(json.dumps(cloud))
    assert (
        _eligibility(actual, freeze, days, publication_receipt(run, actual, digest))[1]
        == "late_research"
    )
    (run / "cloud-publication.json").write_text(
        json.dumps({**cloud, "source_report_sha256": "bad"})
    )
    with pytest.raises(ValueError, match="receipt"):
        publication_receipt(run, actual, digest)


def test_duplicate_scanner_and_no_original_rewrite(sample):
    import fcntl

    storage, _, _, run, _, output = sample
    output.mkdir()
    with (output / ".observation-scan.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        assert scan_pending(run.parent, storage.base_dir, output)["status"].startswith(
            "another_observer"
        )
    assert scan_pending(run.parent, storage.base_dir, output)["status"] == "complete"
