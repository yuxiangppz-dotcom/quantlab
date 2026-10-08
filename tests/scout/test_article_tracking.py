from copy import deepcopy
from pathlib import Path

import pytest

from quantlab.scout.article_risks import stable_id
from quantlab.scout.article_tracking import observe, save_observation

SESSIONS = ["2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]


def sample():
    report = {
        "run_id": "20261008-frozen-research",
        "strategy_version": "article_v1",
        "config_hash": "frozen-config",
        "signal_date": SESSIONS[0],
        "target_date": SESSIONS[1],
        "cutoff_at": "2026-10-08T08:45:00+08:00",
        "data_version": "sources_v1",
        "chart_version": "visual_v1",
        "candidates": [
            {
                "ts_code": "600001.SH",
                "route": "base_breakout",
                "final_status": "priority_candidate",
                "chart_status": "partial",
                "intraday_quality": "mixed",
                "price": {
                    "status": "valid",
                    "entry_low": 10.5,
                    "entry_high": 11.2,
                    "invalidation": 9.9,
                    "entry_mode": "open_reference",
                },
            }
        ],
    }
    bars = [
        {
            "date": day,
            "open": 10.8 if index else 10,
            "high": 11.5 if index else 10.1,
            "low": 10 if index else 9.9,
            "close": 10.5 if index else 10,
            "adj_factor": 1,
        }
        for index, day in enumerate(SESSIONS)
    ]
    market = {
        "asof": "2026-10-14T18:00:00+08:00",
        "sessions": SESSIONS,
        "daily": {"600001.SH": bars},
        "statuses": {"600001.SH": {"date": SESSIONS[1], "status": "tradable"}},
        "limits": {
            "600001.SH": {"date": SESSIONS[1], "up_limit": 12, "down_limit": 9, "tick": 0.01}
        },
    }
    return report, market


def row(tracking):
    return tracking["rows"][0]


def test_actual_holidays_calendar_and_high_open_does_not_mean_user_earned_close_gain():
    report, market = sample()
    result = observe(report, market)
    observed = row(result)
    assert observed["d1_gap"] == pytest.approx(0.08)
    assert observed["windows"]["1"]["open_to_close"] == pytest.approx(-0.02777777777777779)
    assert observed["windows"]["3"]["dates"] == SESSIONS[1:4]
    assert observed["main_price_change"] == observed["windows"]["3"]["open_to_close"]
    assert result["d1_open_to_close_is_diagnostic_not_t_plus_zero_exit"]
    assert observed["entry_observation"]["fill_status"] == "not_inferred"


def test_mfe_mae_and_prices_use_one_scale_across_ex_rights():
    report, market = sample()
    for bar in market["daily"]["600001.SH"][1:]:
        for field in ("open", "high", "low", "close"):
            bar[field] /= 2
        bar["adj_factor"] = 2
    market["limits"]["600001.SH"].update(up_limit=6, down_limit=4.5)
    result = observe(report, market)
    observed = row(result)
    assert observed["d1_gap"] == pytest.approx(0.08)
    assert observed["entry_check"] == "pass"
    assert observed["windows"]["3"]["open_to_close"] == pytest.approx(10.5 / 10.8 - 1)
    assert observed["windows"]["3"]["mfe"] == pytest.approx(11.5 / 10.8 - 1)
    assert observed["windows"]["3"]["mae"] == pytest.approx(10 / 10.8 - 1)


def test_unfinished_window_never_uses_injected_same_day_close_or_future_prices():
    report, market = sample()
    market["asof"] = "2026-10-08T11:00:00+08:00"
    result = observe(report, market)
    assert row(result)["entry_check"] == "pass"
    assert row(result)["windows"]["1"]["status"] == "pending"
    assert row(result)["windows"]["1"]["open_to_close"] is None
    assert row(result)["windows"]["5"]["mae"] is None
    assert result["all_frozen"]["windows"]["3"]["mean_open_to_close_price_change"] is None
    market["asof"] = "2026-10-08T08:45:00+08:00"
    before = observe(report, market)
    assert row(before)["entry_check"] == "pending"
    assert row(before)["d1_gap"] is None


def test_missing_middle_day_does_not_shift_horizon_or_replace_with_later_row():
    report, market = sample()
    del market["daily"]["600001.SH"][2]
    result = observe(report, market)
    assert row(result)["windows"]["3"]["dates"] == SESSIONS[1:4]
    assert row(result)["windows"]["3"]["status"] == "missing"
    assert row(result)["windows"]["3"]["open_to_close"] is None
    assert row(result)["windows"]["1"]["status"] == "complete"
    assert result["all_frozen"]["original_count"] == 1


@pytest.mark.parametrize(
    "field,value", [("adj_factor", None), ("adj_factor", 0), ("high", float("inf")), ("low", -1)]
)
def test_nonfinite_or_invalid_prices_retained_unknown_not_zero(field, value):
    report, market = sample()
    market["daily"]["600001.SH"][3][field] = value
    result = observe(report, market)
    assert row(result)["windows"]["3"]["status"] == "missing"
    assert row(result)["main_price_change"] is None
    assert result["all_frozen"]["windows"]["3"]["missing_count"] == 1


def test_limit_source_missing_is_not_tradable_and_can_later_complete_once():
    report, market = sample()
    original_limits = deepcopy(market["limits"])
    market["limits"] = {}
    first = observe(report, market)
    assert row(first)["entry_check"] == "unobservable"
    assert not row(first)["entry_observation"]["frozen"]
    market["limits"] = original_limits
    second = observe(report, market, previous=first)
    assert row(second)["entry_check"] == "pass"
    assert row(second)["entry_observation"]["frozen"]


def test_open_at_actual_limit_stays_unobservable_without_inferring_fill():
    report, market = sample()
    market["limits"]["600001.SH"]["up_limit"] = 10.8
    result = observe(report, market)
    assert row(result)["entry_check"] == "unobservable"
    assert row(result)["entry_observation"]["frozen"]
    assert (
        row(result)["entry_observation"]["reason_code"] == "open_at_up_limit_execution_unconfirmed"
    )
    assert result["open_reference_pass_subset"]["ids"] == []


def test_failed_high_open_is_never_upgraded_by_later_rise_or_bound_change():
    report, market = sample()
    report["candidates"][0]["price"]["entry_high"] = 10.7
    first = observe(report, market)
    assert row(first)["entry_check"] == "fail"
    frozen_entry = deepcopy(row(first)["entry_observation"])
    market["daily"]["600001.SH"][1]["open"] = 10.6
    market["daily"]["600001.SH"][1]["close"] = 11
    second = observe(report, market, previous=first)
    assert row(second)["entry_observation"] == frozen_entry
    assert row(second)["entry_check"] == "fail"
    assert row(second)["entry_evidence_changed_since_freeze"]
    assert result_ids(second) == ["600001.SH"]
    assert second["open_reference_pass_subset"]["ids"] == []


def result_ids(result):
    return [item["ts_code"] for item in result["rows"]]


def test_all_frozen_and_open_pass_subset_retained_separately_even_suspension():
    report, market = sample()
    second = deepcopy(report["candidates"][0])
    second.update(ts_code="600002.SH", route="pullback_recovery", final_status="watch")
    report["candidates"].append(second)
    market["daily"]["600002.SH"] = []
    market["statuses"]["600002.SH"] = {"date": SESSIONS[1], "status": "suspended"}
    result = observe(report, market)
    assert result_ids(result) == ["600001.SH", "600002.SH"]
    assert result["all_frozen"]["original_count"] == 2
    assert result["all_frozen"]["windows"]["3"]["missing_count"] == 1
    assert result["open_reference_pass_subset"]["ids"] == ["600001.SH"]
    assert result["entry_counts"]["fail"] == 1
    assert result["rows"][1]["route"] == "pullback_recovery"
    assert result["rows"][1]["chart_version"] == "visual_v1"


def test_wrong_target_status_or_future_receipt_cannot_supply_open_confirmation():
    report, market = sample()
    market["statuses"]["600001.SH"]["date"] = SESSIONS[0]
    assert row(observe(report, market))["entry_check"] == "unobservable"
    market["statuses"]["600001.SH"]["date"] = SESSIONS[1]
    market["limits"]["600001.SH"]["first_seen_at"] = "2026-10-15T00:00:00+08:00"
    assert row(observe(report, market))["entry_check"] == "unobservable"


def test_partial_failed_or_unverified_receipts_never_complete_price_or_entry():
    report, market = sample()
    market["limits"]["600001.SH"]["source_status"] = "failed"
    assert row(observe(report, market))["entry_check"] == "unobservable"
    del market["limits"]["600001.SH"]["source_status"]
    market["daily"]["600001.SH"][3]["complete"] = False
    result = observe(report, market)
    assert row(result)["windows"]["3"]["status"] == "missing"
    assert row(result)["windows"]["3"]["open_to_close"] is None


def test_later_trade_status_change_is_diagnostic_not_a_retrospective_subset_removal():
    report, market = sample()
    first = observe(report, market)
    market["statuses"]["600001.SH"]["status"] = "suspended"
    second = observe(report, market, first)
    assert row(second)["entry_check"] == "pass"
    assert row(second)["entry_evidence_changed_since_freeze"]
    assert row(second)["latest_entry_evidence_diagnostic"]["reason_code"] == "target_not_tradable"
    assert second["open_reference_pass_subset"]["ids"] == ["600001.SH"]


def test_frozen_status_receipt_has_no_mutable_alias_to_current_market_or_previous():
    report, market = sample()
    market["statuses"]["600001.SH"]["status"] = "suspended"
    first = observe(report, market)
    snapshot = deepcopy(first)
    market["statuses"]["600001.SH"]["status"] = "tradable"
    second = observe(report, market, first)
    assert first == snapshot
    assert row(second)["entry_check"] == "fail"
    row(second)["entry_observation"]["status_receipt"]["status"] = "changed-output"
    assert first == snapshot


def test_conflicting_duplicates_never_choose_last_provider_row():
    report, market = sample()
    duplicate = deepcopy(market["daily"]["600001.SH"][1])
    duplicate["open"] = 10.7
    market["daily"]["600001.SH"].append(duplicate)
    result = observe(report, market)
    assert row(result)["entry_check"] == "unobservable"
    assert row(result)["windows"]["3"]["status"] == "missing"
    market["daily"]["600001.SH"].reverse()
    reversed_result = observe(report, market)
    assert reversed_result["rows"] == result["rows"]


def test_integrity_rejects_changed_reports_tampered_previous_or_backward_clock():
    report, market = sample()
    first = observe(report, market)
    changed_report = deepcopy(report)
    changed_report["candidates"][0]["price"]["entry_high"] = 12
    with pytest.raises(ValueError, match="frozen report"):
        observe(changed_report, market, first)
    tampered = deepcopy(first)
    row(tampered)["entry_check"] = "fail"
    with pytest.raises(ValueError, match="integrity"):
        observe(report, market, tampered)
    market["asof"] = "2026-10-13T18:00:00+08:00"
    with pytest.raises(ValueError, match="backward"):
        observe(report, market, first)


def test_actual_asof_required_and_empty_list_is_real_empty_not_failure():
    report, market = sample()
    market["asof"] = "2026-10-14"
    with pytest.raises(ValueError, match="aware actual asof"):
        observe(report, market)
    market["asof"] = "2026-10-14T18:00:00+08:00"
    report["candidates"] = []
    result = observe(report, market)
    assert result["all_frozen"]["original_count"] == 0
    assert result["all_frozen"]["windows"]["3"]["coverage"] is None


def test_immutable_storage_idempotent_and_corruption_never_overwritten(tmp_path):
    report, market = sample()
    result = observe(report, market)
    path = save_observation(tmp_path, result)
    original = path.read_bytes()
    assert path == save_observation(tmp_path, result)
    assert path.read_bytes() == original
    path.write_text("corrupted", encoding="utf-8")
    with pytest.raises(ValueError, match="collision or corruption"):
        save_observation(tmp_path, result)
    assert path.read_text() == "corrupted"


def test_storage_rejects_path_traversal_and_invalid_checksums(tmp_path):
    report, market = sample()
    result = observe(report, market)
    result["run_id"] = "../../canonical"
    result["sha256"] = stable_id({k: v for k, v in result.items() if k != "sha256"})
    with pytest.raises(ValueError, match="Unsafe"):
        save_observation(tmp_path, result)
    result["run_id"] = "safe"
    with pytest.raises(ValueError, match="checksum"):
        save_observation(tmp_path, result)
    assert not list(Path(tmp_path).iterdir())
