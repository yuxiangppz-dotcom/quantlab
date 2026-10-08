"""Offline source-contract cases, including first-seen and partial member coverage."""

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from quantlab.scout.article_sources import (
    DEFAULT_SOURCE_CONFIG,
    MANDATORY_6000,
    ArticleSources,
    membership_at,
    normalize,
    rank_timestamp,
)
from quantlab.scout.models import SHANGHAI

DAY = date(2026, 9, 30)
NOW = datetime(2026, 10, 8, 7, 30, tzinfo=SHANGHAI)
CUTOFF = NOW + timedelta(hours=1)


class Client:
    def __init__(self, function):
        self.function = function
        self.calls = []

    def query(self, api, **params):
        self.calls.append((api, params))
        result = self.function(api, params)
        return pd.DataFrame(result)


def pack(tmp_path, function, **kwargs):
    root = tmp_path / "article"
    root.mkdir(exist_ok=True)
    (root / ".scout-article").write_text("quantlab-scout-article-v1")
    return ArticleSources(root, CUTOFF, client=Client(function), clock=lambda: NOW, **kwargs)


def test_default6000_sources_all_enabled_no_independent_product_dependency():
    assert all(DEFAULT_SOURCE_CONFIG["enabled"][api] for api in MANDATORY_6000)
    assert not DEFAULT_SOURCE_CONFIG["independent_minutes_required"]
    assert not DEFAULT_SOURCE_CONFIG["independent_announcements_required"]


def test_isolated_root_and_symlink_subtree_guard(tmp_path):
    with pytest.raises(ValueError, match="marked isolated"):
        ArticleSources(tmp_path, CUTOFF)
    source = pack(tmp_path, lambda *_: [])
    outside = tmp_path / "outside"
    outside.mkdir()
    (source.root / "article_sources").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="subtree"):
        ArticleSources(source.root, CUTOFF)


def test_permission_failure_is_not_successful_empty_and_secrets_not_receipted(tmp_path):
    secret = "private-token-never-archive"

    def deny(*_):
        raise RuntimeError("积分权限不够 " + secret)

    source = pack(tmp_path, deny)
    batch = source.fetch("top_list", {"trade_date": "20260930"}, expected_date=DAY)
    assert batch.receipt["status"] == "permission_denied"
    assert batch.rows == []
    paths = list(source.raw_root.glob("*/*.json"))
    assert len(paths) == 2  # intent plus immutable result
    assert all(secret not in path.read_text() for path in paths)
    assert all("RuntimeError" in path.read_text() for path in paths if "result" in path.name)
    assert (
        source.fetch("top_list", {"trade_date": "20260930"}).receipt["status"]
        == "permission_denied"
    )
    assert len(source.client.calls) == 1


def test_empty_full_event_query_neutral_but_dense_source_missing(tmp_path):
    source = pack(tmp_path, lambda *_: [])
    event = source.fetch("top_list", {"trade_date": "20260930"}, expected_date=DAY)
    dense = source.fetch("dc_daily", {"trade_date": "20260930"}, expected_date=DAY)
    assert event.receipt["status"] == "available"
    assert event.receipt["empty_semantics"] == "queried_no_records"
    assert dense.receipt["status"] == "delayed"
    assert dense.receipt["empty_semantics"] == "dense_source_missing"


def test_old_trade_date_not_used_as_current_and_raw_retained(tmp_path):
    source = pack(
        tmp_path,
        lambda *_: [{"ts_code": "BK001.DC", "trade_date": "20260929", "close": 5, "amount": 2}],
    )
    result = source.fetch("dc_daily", {"trade_date": "20260930"}, expected_date=DAY)
    assert result.receipt["status"] == "delayed"
    assert result.rows == []
    raw = json.loads((source.root / result.receipt["artifact_ref"]).read_text())
    assert raw["rows"][0]["trade_date"] == "20260929"
    assert result.receipt["data_dates"] == ["2026-09-29"]


def test_no_undocumented_offset_or_limit_sent(tmp_path):
    source = pack(tmp_path, lambda *_: [])
    with pytest.raises(ValueError, match="pagination"):
        source.fetch("dc_member", {"trade_date": "20260930", "offset": 5000})
    with pytest.raises(ValueError, match="classification"):
        source.fetch("dc_index", {"trade_date": "20260930"})
    assert source.client.calls == []


def test_amount_volume_percentage_units_are_source_specific():
    assert normalize(
        "moneyflow", {"net_mf_amount": -4000, "buy_lg_amount": 50, "buy_lg_vol": 3}
    ) == {
        "net_mf_amount_yuan": -40_000_000,
        "buy_lg_amount_yuan": 500_000,
        "buy_lg_vol_shares": 300,
    }
    assert normalize("dc_daily", {"amount": 4000, "vol": 3, "pct_change": 5}) == {
        "amount_yuan": 4000,
        "vol_shares": 3,
        "pct_change_fraction": 0.05,
    }
    assert normalize("sw_daily", {"amount": 4000, "vol": 3}) == {
        "amount_yuan": 40_000_000,
        "vol_shares": 30_000,
    }
    assert normalize("top_list", {"net_amount": -40_000_000, "net_rate": -8}) == {
        "net_rate_fraction": -0.08
    }
    assert normalize("top_inst", {"buy": 3000, "sell": 4000}) == {
        "buy_yuan": 3000,
        "sell_yuan": 4000,
    }
    assert normalize("moneyflow", {"net_mf_amount": float("inf")}) == {}


def test_ths_rank_time_later_than_cutoff_or_retrieval_not_used(tmp_path):
    source = pack(
        tmp_path,
        lambda *_: [
            {"ts_code": "600001.SH", "trade_date": "20260930", "rank_time": "2026-10-08 22:30:00"},
            {"ts_code": "600002.SH", "trade_date": "20260930", "rank_time": "15:00:00"},
        ],
    )
    batch = source.fetch(
        "ths_hot", {"trade_date": "20260930", "market": "热股", "is_new": "N"}, expected_date=DAY
    )
    assert batch.receipt["status"] == "partial"
    assert [row["ts_code"] for row in batch.rows] == ["600002.SH"]
    assert batch.rows[0]["_published_at"] == "2026-09-30T15:00:00+08:00"
    assert rank_timestamp("202609301530", DAY).hour == 15


def test_cached_first_seen_does_not_become_historical_known_time(tmp_path):
    row = {"ts_code": "600001.SH", "trade_date": "20260930", "net_mf_amount": 10}
    source = pack(tmp_path, lambda *_: [row])
    params = {"ts_code": "600001.SH", "trade_date": "20260930"}
    first = source.fetch("moneyflow", params, expected_date=DAY)
    original = first.rows[0]["_first_seen_at"]
    later = ArticleSources(source.root, CUTOFF, clock=lambda: NOW + timedelta(minutes=1))
    cached = later.fetch("moneyflow", params, expected_date=DAY)
    assert cached.rows[0]["_first_seen_at"] == original
    assert cached.receipt["cache_reused"]
    assert later.calls == 0
    old = ArticleSources(
        source.root, NOW - timedelta(days=7), client=Client(lambda *_: [row]), clock=lambda: NOW
    )
    historical = old.fetch("moneyflow", params, expected_date=DAY)
    assert not historical.rows
    assert historical.receipt["status"] == "failed"
    assert historical.receipt["reason"] == "source_cutoff_passed"
    assert old.calls == 0


def test_target_st_before920_is_delayed_even_empty(tmp_path):
    source = pack(tmp_path, lambda *_: [])
    batch = source.fetch("stock_st", {"trade_date": "20261008"}, expected_date=date(2026, 10, 8))
    assert batch.receipt["status"] == "delayed"
    assert batch.receipt["reason"] == "target_status_before_documented_update"


def test_today_lhb_before_close_is_not_neutral_and_early_kpl_delayed(tmp_path):
    source = pack(tmp_path, lambda *_: [])
    batch = source.fetch("top_list", {"trade_date": "20261008"}, expected_date=date(2026, 10, 8))
    assert batch.receipt["status"] == "delayed"
    assert batch.receipt["reason"] == "completed_session_not_yet_available"
    source.clock = lambda: NOW.replace(hour=5)
    kpl = source.fetch("kpl_list", {"trade_date": "20261007"}, expected_date=date(2026, 10, 7))
    assert kpl.receipt["status"] == "delayed"
    assert kpl.receipt["reason"] == "KPL_before_documented_next_day_update"


def test_reused_request_revalidates_required_window_without_new_call(tmp_path):
    source = pack(
        tmp_path,
        lambda *_: [{"ts_code": "600001.SH", "trade_date": "20260930", "net_mf_amount": 1}],
    )
    params = {"ts_code": "600001.SH", "start_date": "20260929", "end_date": "20260930"}
    assert source.fetch("moneyflow", params, expected_date=DAY).receipt["status"] == "available"
    reused = source.fetch("moneyflow", params, expected_dates=(date(2026, 9, 29), DAY))
    assert reused.receipt["status"] == "partial"
    assert source.calls == 1


def test_provider_wrong_subject_is_archived_but_not_used(tmp_path):
    source = pack(
        tmp_path,
        lambda *_: [{"ts_code": "600002.SH", "trade_date": "20260930", "net_mf_amount": 1}],
    )
    result = source.fetch(
        "moneyflow", {"ts_code": "600001.SH", "trade_date": "20260930"}, expected_date=DAY
    )
    assert not result.rows
    assert "requested_subject_mismatch" in result.receipt["reasons"]
    assert result.receipt["row_count"] == 1


def test_future_target_st_and_suspend_empty_are_delayed_at_night(tmp_path):
    source = pack(tmp_path, lambda *_: [])
    source.clock = lambda: NOW.replace(hour=22)
    source.cutoff = NOW + timedelta(days=1)
    for api in ("stock_st", "suspend_d"):
        batch = source.fetch(api, {"trade_date": "20261009"}, expected_date=date(2026, 10, 9))
        assert batch.receipt["status"] == "delayed"
        assert batch.receipt["reason"] == "future_target_state_not_available"


def test_candidate_funds_shared_five_daily_queries_before_deep_quota(tmp_path):
    dates = [date(2026, 9, current) for current in (24, 25, 28, 29, 30)]
    codes = ["600001.SH", "600002.SH"]

    def data(api, params):
        if api == "moneyflow":
            return [
                {"ts_code": code, "trade_date": params["trade_date"], "net_mf_amount": 5}
                for code in codes + ["300001.SZ"]
            ]
        return []

    source = pack(tmp_path, data)
    result = source.collect_candidate_checks(DAY, dates, codes)
    queries = [params for api, params in source.client.calls if api == "moneyflow"]
    assert len(queries) == 5
    assert all("ts_code" not in params for params in queries)
    assert len(result["records"]["moneyflow"]) == 10
    assert all(row["ts_code"] in codes for row in result["records"]["moneyflow"])
    assert all(
        coverage["moneyflow"]["status"] == "available"
        for coverage in result["candidate_coverage"].values()
    )
    assert source.calls == 22  # 10 LHB + 5 funds + 2 shared risks + 5 periods
    assert result["target_unlock_window_status"] == "required_calendar_incomplete"


def test_unlock_targeted_empty_complete_independent_of_other_stock_unknown(tmp_path):
    dates = [date(2026, 9, current) for current in (24, 25, 28, 29, 30)]
    future = [date(2026, 10, current) for current in (9, 12, 13, 14, 15)]
    codes = ["600001.SH", "600002.SH"]

    def data(api, params):
        if api != "share_float":
            return []
        if params.get("ts_code") == codes[0]:
            return []  # A completed stock-specific query has no disclosed events.
        if params.get("ts_code") == codes[1]:
            raise PermissionError("permission denied")
        return [
            {
                "ts_code": "300001.SZ",
                "ann_date": "20260930",
                "float_date": params["start_date"],
                "float_share": number + 1,
            }
            for number in range(6000)
        ]

    source = pack(tmp_path, data, max_requests=600, max_rows=500_000)
    result = source.collect_candidate_checks(DAY, dates, codes, target_sessions=future)
    assert result["candidate_coverage"][codes[0]]["share_float"]["status"] == "available"
    assert result["candidate_coverage"][codes[0]]["share_float"]["row_count"] == 0
    assert result["candidate_coverage"][codes[1]]["share_float"]["status"] == "permission_denied"
    targeted = [
        row
        for row in result["coverage"]
        if row["api"] == "share_float" and row["params"].get("ts_code")
    ]
    assert len(targeted) == 2
    assert {row["params"]["ts_code"]: row["status"] for row in targeted} == {
        codes[0]: "available",
        codes[1]: "permission_denied",
    }
    assert all(row["params"]["start_date"] == "20261009" for row in targeted)
    assert all(row["params"]["end_date"] == "20261015" for row in targeted)
    assert result["records"]["share_float"] == []


def test_complete_global_unlock_window_needs_no_extra_stock_queries(tmp_path):
    source = pack(tmp_path, lambda *_: [])
    future = [date(2026, 10, current) for current in (9, 12, 13, 14, 15)]
    result = source.collect_candidate_checks(DAY, [DAY], ["600001.SH"], target_sessions=future)
    coverage = result["candidate_coverage"]["600001.SH"]["share_float"]
    assert coverage["status"] == "available"
    assert coverage["coverage_mode"] == "complete_global_unlock_window"
    assert not any(
        api == "share_float" and "ts_code" in params for api, params in source.client.calls
    )


def test_unlock_response_outside_requested_float_date_remains_unknown(tmp_path):
    source = pack(
        tmp_path,
        lambda *_: [
            {
                "ts_code": "600001.SH",
                "ann_date": "20260930",
                "float_date": "20261008",
                "float_share": 1,
            }
        ],
    )
    result = source.fetch(
        "share_float",
        {"ts_code": "600001.SH", "start_date": "20261009", "end_date": "20261015"},
        freshness_required=False,
    )
    assert result.receipt["status"] == "delayed"
    assert result.receipt["valid_count"] == 0
    assert "requested_unlock_date_mismatch" in result.receipt["reasons"]
    assert result.rows == []


def test_moneyflow_missing_one_of_required_five_sessions_remains_partial(tmp_path):
    dates = [date(2026, 9, day) for day in (24, 25, 28, 29, 30)]
    source = pack(
        tmp_path,
        lambda *_: [
            {"ts_code": "600001.SH", "trade_date": current.strftime("%Y%m%d"), "net_mf_amount": 1}
            for current in dates[:-1]
        ],
    )
    batch = source.fetch(
        "moneyflow",
        {"ts_code": "600001.SH", "start_date": "20260924", "end_date": "20260930"},
        expected_dates=dates,
    )
    assert batch.receipt["status"] == "partial"
    assert "missing_expected_dates" in batch.receipt["reasons"]
    assert len(batch.rows) == 4


def test_request_budget_does_not_reset_and_unknown_requests_are_not_paid(tmp_path):
    source = pack(tmp_path, lambda *_: [], max_requests=1)
    source.fetch("top_list", {"trade_date": "20260930"})
    failure = source.fetch("top_inst", {"trade_date": "20260930"})
    assert source.calls == 1
    assert failure.receipt["status"] == "failed"
    assert failure.receipt["reason"] == "source_budget_exhausted"
    assert len(source.client.calls) == 1


def test_response_over_time_budget_not_available(tmp_path):
    timer = iter((0, 0, 20))
    source = pack(
        tmp_path,
        lambda *_: [{"ts_code": "600001.SH", "trade_date": "20260930", "reason": "日涨幅偏离"}],
        max_seconds=10,
        elapsed_clock=lambda: next(timer),
    )
    result = source.fetch("top_list", {"trade_date": "20260930"}, expected_date=DAY)
    assert result.receipt["status"] == "failed"
    assert result.rows == []
    assert result.receipt["reason"] == "source_time_budget_exceeded"


def test_full_directory_member_recovery_not_first_hot8(tmp_path):
    identifiers = [f"BK{number:03}.DC" for number in range(1, 11)]

    def data(api, params):
        if api == "dc_index":
            return [
                {"ts_code": code, "trade_date": "20260930", "idx_type": params["idx_type"]}
                for code in reversed(identifiers)
            ]
        if api == "dc_member":
            if "ts_code" not in params:
                return [
                    {
                        "ts_code": identifiers[0],
                        "trade_date": "20260930",
                        "con_code": f"{number:06}.SZ",
                    }
                    for number in range(5000)
                ]
            return [
                {"ts_code": params["ts_code"], "trade_date": "20260930", "con_code": "600001.SH"}
            ]
        return []

    source = pack(tmp_path, data)
    result = source.collect_direction_sources(DAY, [], member_request_budget=10)
    assert result["membership"]["full_directory_membership"]
    assert result["membership"]["completed_group_ids"] == identifiers
    assert len(result["records"]["dc_member"]) == 10
    calls = [
        params for api, params in source.client.calls if api == "dc_member" and "ts_code" in params
    ]
    assert [params["ts_code"] for params in calls] == identifiers
    assert all(
        "offset" not in params and "limit" not in params for _, params in source.client.calls
    )
    assert any(item["truncated"] and item["api"] == "dc_member" for item in result["coverage"])


def test_targeted_members_not_claimed_as_full_directory(tmp_path):
    def data(api, params):
        if api == "dc_index":
            return [
                {"ts_code": f"BK{n}.DC", "trade_date": "20260930", "idx_type": params["idx_type"]}
                for n in range(4)
            ]
        if api == "dc_member" and "ts_code" in params:
            return [
                {"ts_code": params["ts_code"], "trade_date": "20260930", "con_code": "600001.SH"}
            ]
        return []

    source = pack(tmp_path, data)
    result = source.collect_direction_sources(DAY, [], member_request_budget=2)
    assert result["membership"]["completed_group_ids"] == ["BK0.DC", "BK1.DC"]
    assert result["membership"]["unverified_group_ids"] == ["BK2.DC", "BK3.DC"]
    assert not result["membership"]["full_directory_membership"]
    assert result["fallback"]["degraded"]


def test_direction_recomputes_600_budget_after_110_shared_calls(tmp_path):
    """An early 474-member estimate must not consume the 120 check-call reserve."""
    identifiers = [f"BK{number:04}.DC" for number in range(474)]

    def data(api, params):
        if api == "dc_index" and params["idx_type"] == "概念板块":
            return [
                {"ts_code": code, "trade_date": "20260930", "idx_type": "概念板块"}
                for code in identifiers
            ]
        if api == "index_classify" and params["level"] == "L1":
            return [{"index_code": f"L1{number:03}.SI", "level": "L1"} for number in range(33)]
        if api == "dc_member" and "ts_code" not in params:
            return [
                {"ts_code": identifiers[0], "trade_date": "20260930", "con_code": str(number)}
                for number in range(5000)
            ]
        if api == "dc_member":
            return [
                {"ts_code": params["ts_code"], "trade_date": "20260930", "con_code": "600001.SH"}
            ]
        if api == "kpl_concept_cons":
            return [
                {"ts_code": "KPL", "trade_date": "20260930", "con_code": str(number)}
                for number in range(5000)
            ]
        return []

    source = pack(tmp_path, data, max_requests=600, max_rows=500_000)
    # Already completed basic/master/probe calls are part of the same frozen total.
    for number in range(6):
        source.fetch("top_list", {"trade_date": f"202609{number + 1:02}"})
    days = [date(2026, 9, 23) + timedelta(days=number) for number in range(6)]
    result = source.collect_direction_sources(
        DAY,
        days,
        candidate_codes=[f"{number:06}.SZ" for number in range(24)],
        member_request_budget=474,
        reserve_requests=120,
    )
    budget = result["membership"]["budget"]
    assert budget["calls_before_member_stage"] == 116  # 6 prior + 110 shared
    assert budget["actual_member_allowance"] == 363  # 600 - 116 - 120 - KPL1
    assert source.calls == 480
    assert budget["remaining_total_requests"] == 120
    assert len(result["membership"]["completed_group_ids"]) == 363
    assert len(result["membership"]["unverified_group_ids"]) == 111
    assert result["membership"]["budget"]["skipped_group_count"] == 111
    assert result["mode"] == "degraded"
    assert not any(
        receipt.get("reason") == "source_budget_exhausted" for receipt in result["coverage"]
    )
    assert len(source.client.calls) == 480
    assert not any(
        "con_code" in params and api in {"dc_member", "kpl_concept_cons"}
        for api, params in source.client.calls
    )
    # Downstream calls remain usable; their budget is never reset or increased.
    downstream = source.fetch("top_inst", {"trade_date": "20260930"})
    assert downstream.receipt["status"] == "available"
    assert source.calls == 481


def test_direction_rejects_invalid_reserve_before_any_requests(tmp_path):
    source = pack(tmp_path, lambda *_: [], max_requests=600)
    for reserve in (-1, True, 601):
        with pytest.raises(ValueError, match="[Rr]eserve"):
            source.collect_direction_sources(DAY, [], reserve_requests=reserve)
    assert source.calls == 0


def test_sw_membership_in_out_interval_not_current_snapshot_backfill():
    row = {"in_date": "20260929", "out_date": "20261008", "is_new": "N"}
    assert membership_at(row, DAY)
    assert not membership_at(row, date(2026, 9, 28))
    assert not membership_at(row, date(2026, 10, 8))
    assert not membership_at({"in_date": None}, DAY)


def test_lhb_reason_side_and_duplicate_institution_rows_preserved(tmp_path):
    rows = [
        {
            "ts_code": "600001.SH",
            "trade_date": "20260930",
            "exalter": "机构专用",
            "side": "0",
            "buy": 100,
            "sell": 30,
            "reason": "日涨幅偏离",
        },
        {
            "ts_code": "600001.SH",
            "trade_date": "20260930",
            "exalter": "机构专用",
            "side": "0",
            "buy": 100,
            "sell": 30,
            "reason": "日涨幅偏离",
        },
        {
            "ts_code": "600001.SH",
            "trade_date": "20260930",
            "exalter": "机构专用",
            "side": "1",
            "buy": 100,
            "sell": 30,
            "reason": "连续三个交易日内",
        },
    ]
    source = pack(tmp_path, lambda *_: rows)
    batch = source.fetch("top_inst", {"trade_date": "20260930"}, expected_date=DAY)
    assert len(batch.rows) == 3
    assert [row["side"] for row in batch.rows] == ["0", "0", "1"]
    assert batch.rows[2]["reason"] == "连续三个交易日内"
    assert "scope_start" not in batch.rows[2]


def test_future_reporting_period_is_not_future_publication(tmp_path):
    source = pack(
        tmp_path,
        lambda *_: [
            {
                "ts_code": "600001.SH",
                "ann_date": "20260930",
                "end_date": "20261231",
                "type": "预亏",
            },
            {
                "ts_code": "600002.SH",
                "ann_date": "20261009",
                "end_date": "20261231",
                "type": "预亏",
            },
        ],
    )
    batch = source.fetch("forecast", {"ts_code": "600001.SH"}, freshness_required=False)
    assert [row["ts_code"] for row in batch.rows] == ["600001.SH"]
    assert batch.rows[0]["_published_at"] is None
    assert batch.rows[0]["_published_date"] == "2026-09-30"
    assert batch.receipt["status"] == "partial"
