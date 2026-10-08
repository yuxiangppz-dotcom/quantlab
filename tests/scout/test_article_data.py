"""Offline schema/unit joins and point-in-time context binding."""

import json
from copy import deepcopy
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from quantlab.scout.article_data import bind_context, extend_context, load_market
from quantlab.scout.article_engine import analyze_stock, assess_groups, assess_market
from quantlab.scout.models import SHANGHAI

DAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 20, tzinfo=SHANGHAI)
CUTOFF = NOW + timedelta(minutes=30)
CODE = "600001.SH"


def write(root, table, current, rows):
    target = (
        root
        / "market"
        / table
        / f"year={current.year}"
        / f"month={current.month:02}"
        / f"{current.isoformat()}.parquet"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(target, index=False)
    return target


def fixture(tmp_path, raw=False):
    market = tmp_path / "market"
    dates = pd.bdate_range(end=DAY, periods=165).date.tolist()
    (market / "calendar").mkdir(parents=True)
    pd.DataFrame(
        [{"exchange": "SSE", "trade_date": current, "is_open": True} for current in dates]
    ).to_parquet(market / "calendar" / "calendar.parquet")
    (market / "securities").mkdir()
    securities = [
        {
            "instrument_id": CODE,
            "name": "*ST不能回填过去",
            "list_date": "20000101",
            "delist_date": None,
            "list_status": "L",
        }
    ]
    pd.DataFrame(securities).to_parquet(market / "securities" / "securities.parquet")
    for current in dates[-2:]:
        identity = {"ts_code" if raw else "instrument_id": CODE, "trade_date": current}
        write(
            tmp_path,
            "daily",
            current,
            [
                identity
                | {
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "pre_close": 10,
                    "vol" if raw else "volume": 2,
                    "amount": 5,
                }
            ],
        )
        write(tmp_path, "adj_factor", current, [identity | {"adj_factor": 1.5}])
        write(
            tmp_path, "daily_price_limit", current, [identity | {"up_limit": 11, "down_limit": 9}]
        )
        write(
            tmp_path,
            "daily_basic",
            current,
            [
                identity
                | {
                    "turnover_rate": 0.02,
                    "total_mv": 100_000,
                    "circ_mv": 90_000,
                    "custom_field": "retained",
                }
            ],
        )
    return load_market(tmp_path, now=NOW)


def evidence(row, *, at=NOW, source="source", ordinal=0):
    return row | {
        "_first_seen_at": at.isoformat(),
        "_fetched_at": at.isoformat(),
        "_source_id": source + str(ordinal),
    }


def receipt(api, params, count=0, status="available", at=NOW):
    return {
        "api": api,
        "params": params,
        "row_count": count,
        "valid_count": count,
        "status": status,
        "truncated": status == "partial",
        "fetched_at": at.isoformat(),
        "request_id": api,
        "scope": "test_whole_day",
    }


def source(records=None, coverage=None):
    return {"records": records or {}, "coverage": coverage or [], "mode": "tushare_6000"}


def test_canonical_units_not_double_converted_and_full_basic_preserved(tmp_path):
    market = fixture(tmp_path)
    row = market["bars_by_code"][CODE][-1]
    assert row["amount_cny"] == 5
    assert row["volume_shares"] == 2
    assert row["adj_factor"] == 1.5
    assert row["up_limit"] == 11
    assert row["daily_basic"]["custom_field"] == "retained"
    assert row["unit_basis"] == "canonical_CNY_shares"
    assert row["turnover_rate"] == 0.02
    assert row["total_mv_cny"] == 100_000
    assert market["history_sessions_loaded"] == 160
    assert len(market["bars_by_code"][CODE]) == 2  # missing dates are not filled
    assert market["signal_date"] == DAY


def test_explicit_raw_tushare_schema_converts_thousand_yuan_and_hands(tmp_path):
    market = fixture(tmp_path, raw=True)
    row = market["bars_by_code"][CODE][-1]
    assert row["amount_cny"] == 5000
    assert row["volume_shares"] == 200
    assert row["unit_basis"] == "raw_TuShare_thousand_CNY_hands"
    assert row["turnover_rate"] == 0.0002
    assert row["total_mv_cny"] == 1_000_000_000


def test_conflicting_same_date_code_adjustment_unknown_not_last_wins(tmp_path):
    fixture(tmp_path)
    write(
        tmp_path,
        "adj_factor",
        DAY,
        [
            {"instrument_id": CODE, "trade_date": DAY, "adj_factor": 1.5},
            {"instrument_id": CODE, "trade_date": DAY, "adj_factor": 2},
        ],
    )
    market = load_market(tmp_path, now=NOW)
    assert market["bars_by_code"][CODE][-1]["adj_factor"] is None
    assert market["bars_by_code"][CODE][-1]["join_status"] == "conflict_unknown"
    assert market["conflicts"]
    with pytest.raises(ValueError, match="160"):
        load_market(tmp_path, history_sessions=120, now=NOW)


def test_identical_daily_duplicates_do_not_double_amount(tmp_path):
    market = fixture(tmp_path)
    path = tmp_path / "market" / "daily" / "year=2026" / "month=09" / "2026-09-30.parquet"
    frame = pd.read_parquet(path)
    pd.concat([frame, frame]).to_parquet(path, index=False)
    reread = load_market(tmp_path, now=NOW)
    assert reread["bars_by_code"][CODE] == market["bars_by_code"][CODE]


def test_missing_dated_states_not_name_or_current_master_backfill(tmp_path):
    market = fixture(tmp_path)
    context = bind_context(market, source(), DAY, CUTOFF.isoformat())
    security = context["security_by_code"][CODE]
    assert security["is_st"] is None  # *ST in latest name is not dated state
    assert security["is_delisting"] is None
    assert security["is_suspended"] is None
    assert security["status_by_date"] == {}


def test_fresh_master_current_only_and_exact_dated_empty_st_queries(tmp_path):
    market = fixture(tmp_path)
    master = evidence(
        {"ts_code": CODE, "name": "当前证券", "list_date": "20000101", "list_status": "L"}
    )
    pack = source(
        {"stock_basic": [master]},
        [
            receipt("stock_basic", {"list_status": "L"}, 1),
            receipt("stock_st", {"trade_date": "20260930"}),
            receipt("suspend_d", {"trade_date": "20260930", "suspend_type": "S"}),
            receipt("stock_st", {"trade_date": "20260929"}),
        ],
    )
    security = bind_context(market, pack, DAY, CUTOFF)["security_by_code"][CODE]
    assert security["is_delisting"] is False
    assert security["is_st"] is False
    assert security["is_suspended"] is False
    assert security["status_by_date"]["2026-09-29"]["is_st"] is False
    assert security["status_by_date"]["2026-09-29"]["is_delisting"] is None
    assert "2026-09-28" not in security["status_by_date"]


def test_failed_or_future_state_source_cannot_mean_not_st(tmp_path):
    market = fixture(tmp_path)
    pack = source(
        {
            "stock_st": [
                evidence(
                    {"ts_code": CODE, "trade_date": "20260930"}, at=CUTOFF + timedelta(seconds=1)
                )
            ]
        },
        [receipt("stock_st", {"trade_date": "20260930"}, 1, status="failed")],
    )
    security = bind_context(market, pack, DAY, CUTOFF)["security_by_code"][CODE]
    assert security["is_st"] is None


def test_dc_partial_member_scope_not_complete_and_no_history_fabrication(tmp_path):
    market = fixture(tmp_path)
    rows = [evidence({"ts_code": "BK1.DC", "con_code": CODE, "trade_date": "20260930"})]
    directory = [evidence({"ts_code": "BK1.DC", "idx_type": "概念板块", "trade_date": "20260930"})]
    pack = source(
        {"dc_member": rows, "dc_index": directory},
        [receipt("dc_member", {"con_code": CODE, "trade_date": "20260930"}, 1)],
    )
    context = bind_context(market, pack, DAY, CUTOFF)
    assert len(context["group_snapshots"]) == 1
    assert context["group_snapshots"][0]["complete"] is False
    assert context["group_snapshots"][0]["snapshot_date"] == "2026-09-30"
    assert context["partial_group_ids"] == ["BK1.DC"]
    pack["coverage"] = [receipt("dc_member", {"ts_code": "BK1.DC", "trade_date": "20260930"}, 1)]
    complete = bind_context(market, pack, DAY, CUTOFF)["group_snapshots"][0]
    assert complete["complete"]
    assert complete["known_at"] == NOW.isoformat()  # not backdated to session close


def test_sw_l2_needs_both_interval_partitions_not_current_only(tmp_path):
    market = fixture(tmp_path)
    row = evidence(
        {
            "ts_code": CODE,
            "l1_code": "801010.SI",
            "l2_code": "801016.SI",
            "in_date": "20000101",
            "out_date": None,
            "is_new": "Y",
        }
    )
    pack = source(
        {"index_member_all": [row]},
        [receipt("index_member_all", {"l1_code": "801010.SI", "is_new": "Y"}, 1)],
    )
    first = bind_context(market, pack, DAY, CUTOFF)["group_snapshots"][0]
    assert first["complete"] is False
    assert first["group_type"] == "industry"
    pack["coverage"].append(receipt("index_member_all", {"l1_code": "801010.SI", "is_new": "N"}))
    completed = bind_context(market, pack, DAY, CUTOFF)["group_snapshots"][0]
    assert completed["complete"]
    assert completed["snapshot_date"] == DAY.isoformat()


def test_unavailable_limit_source_not_backfilled_into_limit_sets(tmp_path):
    market = fixture(tmp_path)
    rows = [
        evidence(
            {"ts_code": CODE, "trade_date": "20260930", "limit": "U"},
            at=CUTOFF + timedelta(seconds=1),
        )
    ]
    context = bind_context(market, source({"limit_list_d": rows}), DAY, CUTOFF)
    assert context["limit_structure"] == {"U": [], "D": [], "Z": []}


def test_missing_or_invalid_receipt_clock_cannot_prove_empty_state(tmp_path):
    market = fixture(tmp_path)
    requests = [receipt("stock_st", {"trade_date": "20260930"})]
    requests[0]["fetched_at"] = None
    context = bind_context(market, source(coverage=requests), DAY, CUTOFF)
    assert context["security_by_code"][CODE]["is_st"] is None
    with pytest.raises(ValueError, match="timezone"):
        bind_context(market, source(), DAY, "2026-09-30T20:30:00")


def test_dropped_raw_rows_do_not_prove_complete_membership(tmp_path):
    market = fixture(tmp_path)
    rows = [evidence({"ts_code": "BK1.DC", "con_code": CODE, "trade_date": "20260930"})]
    directory = [evidence({"ts_code": "BK1.DC", "idx_type": "概念板块", "trade_date": "20260930"})]
    pack = source(
        {"dc_member": rows, "dc_index": directory},
        [receipt("dc_member", {"ts_code": "BK1.DC", "trade_date": "20260930"}, 2)],
    )
    assert not bind_context(market, pack, DAY, CUTOFF)["group_snapshots"][0]["complete"]


def test_conflicting_fresh_master_does_not_choose_last_status(tmp_path):
    market = fixture(tmp_path)
    rows = [
        evidence({"ts_code": CODE, "list_status": "L"}, ordinal=0),
        evidence({"ts_code": CODE, "list_status": "D"}, ordinal=1),
    ]
    pack = source({"stock_basic": rows}, [receipt("stock_basic", {}, 2)])
    current = bind_context(market, pack, DAY, CUTOFF)["security_by_code"][CODE]
    assert current["is_delisting"] is None
    assert current["master_conflict"]


def test_missing_join_is_not_reported_as_complete(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "market" / "adj_factor" / "year=2026" / "month=09" / "2026-09-30.parquet"
    path.unlink()
    market = load_market(tmp_path, now=NOW)
    latest = market["partition_coverage"][-1]
    assert latest["status"] == "unknown"
    assert latest["missing_adjustment_count"] == 1
    assert market["bars_by_code"][CODE][-1]["adj_factor"] is None


def test_explicit_signal_still_requires_completed_session(tmp_path):
    fixture(tmp_path)
    with pytest.raises(ValueError, match="availability gate"):
        load_market(tmp_path, signal_date=DAY, now=NOW.replace(hour=17))


def context_root(tmp_path):
    root = tmp_path / "scout_article_context_test"
    root.mkdir()
    (root / ".scout-article").write_text("quantlab-scout-article-v1", encoding="utf-8")
    return root


def daily_context(current, *, codes=(CODE,), is_st=False, complete=True):
    known = datetime.combine(current, datetime.min.time(), tzinfo=SHANGHAI).replace(hour=22)
    states = {
        code: {
            "ts_code": code,
            "list_date": "20000101",
            "is_st": is_st,
            "is_delisting": False,
            "is_suspended": False,
            "status_by_date": {current.isoformat(): {"is_st": is_st, "is_delisting": False}},
            "suspended_by_date": {current.isoformat(): False},
        }
        for code in codes
    }
    groups = []
    # Two comparable industries support the engine's cross-section ranks.
    for name, members in (("G1", codes[:10]), ("G2", codes[10:])):
        if not members:
            continue
        groups.append(
            {
                "group_id": name,
                "group_type": "industry",
                "snapshot_date": current.isoformat(),
                "known_at": known.isoformat(),
                "members": list(members),
                "complete": complete,
                "source": "offline_real_schema_receipt",
                "source_ids": [f"{current}:{name}"],
            }
        )
    return {"security_by_code": states, "group_snapshots": groups}, known


def test_daily_freezes_accumulate_three_day_market_and_five_day_groups(tmp_path):
    """Dated offline receipts exercise the actual engine, not profitability."""
    root = context_root(tmp_path)
    sessions = pd.bdate_range(end=DAY, periods=165).date.tolist()
    codes = tuple(f"600{number:03}.SH" for number in range(20))
    for current in sessions[-5:]:
        context, known = daily_context(current, codes=codes)
        merged = extend_context(root, context, current, known)
    assert merged["context_history"]["historical_dates"] == [
        current.isoformat() for current in sessions[-5:-1]
    ]
    assert len(merged["group_snapshots"]) == 10
    assert len(merged["security_by_code"][codes[0]]["status_by_date"]) == 5
    analyses = {}
    for index, code in enumerate(codes):
        slope = 0.02 if index < 10 else 0.001
        bars = [
            {
                "date": current.isoformat(),
                "open": 10 + number * slope,
                "high": 10.1 + number * slope,
                "low": 9.9 + number * slope,
                "close": 10 + number * slope,
                "amount_cny": 100_000_000,
                "volume_shares": 1_000_000,
                "adj_factor": 1,
                "up_limit": 11 + number * slope,
                "down_limit": 9 + number * slope,
            }
            for number, current in enumerate(sessions)
        ]
        analyses[code] = analyze_stock(code, bars, sessions, DAY, merged["security_by_code"][code])
    environment = assess_market(analyses, sessions, DAY)
    assert environment["state"] == "active"
    assert all(row["core_status"] == "complete" for row in environment["daily_metrics"][-3:])
    groups = assess_groups(merged["group_snapshots"], analyses, sessions, DAY, known)
    leader = next(group for group in groups if group["group_id"] == "G1")
    assert leader["persistence"] == 5
    assert sum(row["coverage_status"] == "complete" for row in leader["daily_history"]) == 5
    current_only = deepcopy(analyses)
    for code, stock in current_only.items():
        stock["security"] = context["security_by_code"][code]
    assert assess_market(current_only, sessions, DAY)["state"] == "unknown"
    current_groups = assess_groups(context["group_snapshots"], current_only, sessions, DAY, known)
    assert all(group["persistence"] is None for group in current_groups)
    assert len(list((root / "article_context" / "snapshots").glob("*.json"))) == 5


def test_current_metadata_and_late_historical_freeze_cannot_backfill(tmp_path):
    root = context_root(tmp_path)
    old = date(2026, 9, 29)
    context, _ = daily_context(old)
    # This context was genuinely first available on the next day, not on the signal day.
    extend_context(root, context, old, NOW)
    current, known = daily_context(DAY)
    current["security_by_code"][CODE]["status_by_date"][old.isoformat()] = {
        "is_st": False,
        "is_delisting": False,
    }
    current["group_snapshots"].append(
        context["group_snapshots"][0] | {"known_at": known.isoformat()}
    )
    merged = extend_context(root, current, DAY, known)
    assert merged["context_history"]["historical_dates"] == []
    assert old.isoformat() not in merged["security_by_code"][CODE]["status_by_date"]
    assert [row["snapshot_date"] for row in merged["group_snapshots"]] == [DAY.isoformat()]
    assert merged["context_history"]["rejected_snapshots"][0]["reason"] == (
        "first_available_after_signal_day"
    )


def test_latest_actual_daily_timestamp_selected_not_oldest(tmp_path):
    root = context_root(tmp_path)
    old = date(2026, 9, 29)
    earlier, known = daily_context(old)
    extend_context(root, earlier, old, known)
    later, _ = daily_context(old, is_st=True, complete=False)
    extend_context(root, later, old, known + timedelta(minutes=30))
    current, known = daily_context(DAY)
    merged = extend_context(root, current, DAY, known, save=False)
    assert merged["security_by_code"][CODE]["status_by_date"][old.isoformat()]["is_st"] is True
    old_group = next(row for row in merged["group_snapshots"] if row["snapshot_date"] == str(old))
    assert old_group["complete"] is False
    assert merged["partial_group_ids"] == []  # old incomplete groups do not relabel today's scope
    assert merged["context_history"]["saved_snapshot"] is None
    assert len(list((root / "article_context" / "snapshots").glob("*.json"))) == 2


def test_equal_time_conflict_becomes_unknown_for_current_and_future(tmp_path):
    root = context_root(tmp_path)
    old = date(2026, 9, 29)
    first, known = daily_context(old)
    extend_context(root, first, old, known)
    second, _ = daily_context(old, is_st=True)
    immediate = extend_context(root, second, old, known)
    assert immediate["security_by_code"][CODE]["is_st"] is None
    assert immediate["group_snapshots"][0]["complete"] is False
    current, known = daily_context(DAY)
    merged = extend_context(root, current, DAY, known)
    assert merged["security_by_code"][CODE]["status_by_date"][old.isoformat()]["is_st"] is None
    assert merged["context_history"]["equal_time_conflict_dates"] == [str(old)]
    old_group = next(row for row in merged["group_snapshots"] if row["snapshot_date"] == str(old))
    assert old_group["members"] == []
    assert old_group["complete"] is False


def test_context_create_only_idempotence_and_integrity_fail_closed(tmp_path):
    root = context_root(tmp_path)
    context, known = daily_context(DAY)
    before = deepcopy(context)
    first = extend_context(root, context, DAY, known)
    target = root / first["context_history"]["saved_snapshot"]
    original_bytes, original_mtime = target.read_bytes(), target.stat().st_mtime_ns
    extend_context(root, context, DAY, known)
    assert target.read_bytes() == original_bytes
    assert target.stat().st_mtime_ns == original_mtime
    assert context == before
    altered = json.loads(target.read_text(encoding="utf-8"))
    altered["security_states"][CODE]["status"]["is_st"] = True
    target.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        extend_context(root, context, DAY, known)
    assert json.loads(target.read_text())["security_states"][CODE]["status"]["is_st"] is True


def test_context_requires_new_marker_name_and_rejects_linked_subtree(tmp_path):
    context, known = daily_context(DAY)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / ".scout-article").write_text("quantlab-scout-article-v1")
    with pytest.raises(ValueError, match="newly marked"):
        extend_context(canonical, context, DAY, known)
    root = context_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "article_context").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="subtree"):
        extend_context(root, context, DAY, known)
    assert list(outside.iterdir()) == []


def test_historical_freeze_bound_is_shanghai_day_end(tmp_path):
    root = context_root(tmp_path)
    old = date(2026, 9, 29)
    context, known = daily_context(old)
    day_end = known.replace(hour=23, minute=59, second=59)
    extend_context(root, context, old, day_end.isoformat())
    current, known = daily_context(DAY)
    merged = extend_context(root, current, DAY, known, save=False)
    assert merged["context_history"]["historical_dates"] == [str(old)]
    extend_context(root, context, old, day_end + timedelta(microseconds=1))
    reread = extend_context(root, current, DAY, known, save=False)
    # The late record is excluded; the earlier proven day-end snapshot remains eligible.
    assert reread["context_history"]["historical_dates"] == [str(old)]
    assert reread["context_history"]["rejected_snapshots"]


def test_context_cannot_be_frozen_before_its_signal_day(tmp_path):
    root = context_root(tmp_path)
    context, known = daily_context(DAY)
    with pytest.raises(ValueError, match="precede"):
        extend_context(root, context, DAY, known - timedelta(days=1))
    assert not (root / "article_context").exists()
