from copy import deepcopy
from datetime import date, timedelta

import pytest

from quantlab.scout.article_risks import (
    config_hash,
    evaluate_moneyflow,
    evaluate_unlock_risk,
    high_divergence_flag,
    normalize_lhb_events,
    qualify_lhb_unit_evidence,
    risk_config,
)


def days(n=8):
    return [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(n)]


def money_samples(net=-100, large=-100, amounts=None):
    dates = days()
    bars = [
        {"date": day, "close": 10 + i * 0.1, "amount_cny": 100_000_000}
        for i, day in enumerate(dates)
    ]
    if amounts:
        for bar, amount in zip(bars[-len(amounts) :], amounts, strict=True):
            bar["amount_cny"] = amount
    rows = [
        {
            "trade_date": day.replace("-", ""),
            "net_mf_amount": net,
            "buy_lg_amount": max(large, 0),
            "buy_elg_amount": 0,
            "sell_lg_amount": max(-large, 0),
            "sell_elg_amount": 0,
        }
        for day in dates
    ]
    return rows, bars, dates


def evaluate(rows, bars, dates, **kw):
    return evaluate_moneyflow(
        rows,
        bars,
        dates,
        signal_date=dates[-1],
        setup="pullback_recovery",
        units_verified=True,
        **kw,
    )


def event(net=-40_000_000, amount=500_000_000, **kwargs):
    buy = 10_000_000 + max(net, 0)
    sell = 10_000_000 + max(-net, 0)
    row = {
        "report_event_id": "e1",
        "ts_code": "000001.SZ",
        "report_date": "2026-09-08",
        "reason_raw": "daily reason",
        "scope_type": "single_day",
        "scope_start": "2026-09-08",
        "scope_end": "2026-09-08",
        "scope_verified": True,
        "l_buy": buy,
        "l_sell": sell,
        "net_amount": net,
        "l_amount": buy + sell,
        "amount": amount,
        "net_rate": 100 * net / amount if amount else None,
        "units_verified": True,
        "complete": True,
        "published_at": "2026-09-08T18:00:00+08:00",
        "first_seen_at": "2026-09-08T18:01:00+08:00",
        "effective_target_date": "2026-09-09",
        "newness_verified": True,
        "historical_import": False,
        "source_ids": ["exchange-receipt"],
        "institutions_complete": True,
        "institution_scope_verified": True,
        "institution_rows": [],
    }
    row.update(kwargs)
    return row


def lhb(rows, **kw):
    return normalize_lhb_events(
        rows, [], target_date="2026-09-09", cutoff_at="2026-09-09T08:45:00+08:00", **kw
    )


def seat(side, buy, sell, institution=True, **kw):
    return {
        "side": side,
        "buy": buy,
        "sell": sell,
        "institution": institution,
        "classification_source_id": "verified-name-map",
        "exalter": "机构专用",
        **kw,
    }


def test_moneyflow_uses_ratio_of_sums_and_keeps_f_and_g_separate():
    rows, bars, dates = money_samples(
        net=100, large=-50, amounts=[100_000_000, 200_000_000, 400_000_000]
    )
    result = evaluate(rows, bars, dates)
    assert result["windows"]["3"]["flow_ratio"] == pytest.approx(3_000_000 / 700_000_000)
    assert result["windows"]["3"]["large_order_ratio"] == pytest.approx(-1_500_000 / 700_000_000)
    assert result["windows"]["3"]["flow_ratio"] != pytest.approx((0.01 + 0.005 + 0.0025) / 3)
    assert result["action"] == "pass"


def test_severe_funds_combination_excludes_but_standalone_only_watch():
    rows, bars, dates = money_samples(net=-900, large=-400)
    result = evaluate(rows, bars, dates)
    assert result["negative_days_5"] == result["consecutive_outflow_days"] == 5
    assert result["action"] == "watch_only"
    assert evaluate(rows, bars, dates, support_broken=True)["action"] == "exclude"
    assert evaluate(rows, bars, dates, high_divergence=True)["action"] == "exclude"


def test_mild_pullback_outflow_is_not_excluded_or_demanded_to_be_positive():
    rows, bars, dates = money_samples(net=-100, large=-100)
    assert evaluate(rows, bars, dates)["action"] == "pass"
    result = evaluate_moneyflow(
        rows, bars, dates, signal_date=dates[-1], setup="base_breakout", units_verified=True
    )
    assert result["action"] == "watch_only"


def test_missing_calendar_day_not_replaced_by_oldest_available_row():
    rows, bars, dates = money_samples()
    del rows[-3]
    result = evaluate(rows, bars, dates)
    assert result["windows"]["1"]["status"] == "complete"
    assert result["windows"]["3"]["status"] == "unknown"
    assert result["negative_days_5"] is None
    assert result["action"] == "review_required"


@pytest.mark.parametrize(
    "field,value",
    [
        ("net_mf_amount", float("nan")),
        ("net_mf_amount", float("inf")),
        ("net_mf_amount", 1e308),
        ("buy_lg_amount", -1),
        ("sell_elg_amount", None),
    ],
)
def test_invalid_moneyflow_cannot_trigger_or_receive_bonus(field, value):
    rows, bars, dates = money_samples(net=-900, large=-400)
    rows[-1][field] = value
    result = evaluate(rows, bars, dates, support_broken=True)
    assert result["status"] == "unknown"
    assert result["action"] == "review_required"
    assert result["windows"]["1"]["flow_ratio"] is None


def test_conflicts_unit_uncertainty_and_zero_amount_are_unknown():
    rows, bars, dates = money_samples()
    conflict = deepcopy(rows[-1])
    conflict["net_mf_amount"] = 900
    assert evaluate(rows + [conflict], bars, dates)["status"] == "unknown"
    assert (
        evaluate_moneyflow(rows, bars, dates, signal_date=dates[-1], setup="base_breakout")[
            "status"
        ]
        == "unknown"
    )
    for bar in bars:
        bar["amount_cny"] = 0
    assert evaluate(rows, bars, dates)["status"] == "unknown"


def test_funds_order_invariance_and_no_future_injection():
    rows, bars, dates = money_samples(net=200, large=100)
    baseline = evaluate(rows, bars, dates)
    assert evaluate(list(reversed(rows)), list(reversed(bars)), dates) == baseline
    rows.append({"trade_date": "20260909", "net_mf_amount": -9000})
    assert evaluate(rows, bars, dates) == baseline


def test_lhb_severe_absolute_and_ratio_warning_are_independent():
    assert lhb([event()])["action"] == "exclude"
    assert lhb([event(amount=2_000_000_000)])["action"] == "watch_only"
    assert lhb([event(net=-10_000_000, amount=100_000_000)])["action"] == "watch_only"
    assert lhb([])["status"] == "not_listed"
    assert lhb([], source_status="delayed")["action"] == "review_required"


def test_multi_reason_duplicate_is_one_event_without_readding_institutions():
    seats = [seat(0, 2_000_000, 30_000_000), seat(1, 500_000, 25_000_000)]
    first = event(net=0, institution_rows=seats)
    second = event(net=0, report_event_id="e2", reason_raw="another reason", institution_rows=seats)
    result = lhb([first, second])
    assert len(result["events"]) == 1
    assert result["events"][0]["reason_raw"] == ["another reason", "daily reason"]
    assert result["events"][0]["institution_rank_net_cny"] == -23_000_000
    assert result["action"] == "exclude"


def test_real_same_name_same_amount_rows_are_not_name_deduplicated():
    row = event(
        net=0,
        amount=500_000_000,
        institution_rows=[
            seat(0, 1_000_000, 90_000_000),
            seat(0, 1_000_000, 90_000_000),
            seat(1, 80_000_000, 12_000_000),
            seat(1, 80_000_000, 12_000_000),
        ],
    )
    result = lhb([row])["events"][0]
    assert result["institution_rank_buy_cny"] == 2_000_000
    assert result["institution_rank_sell_cny"] == 24_000_000
    assert result["institution_rank_net_cny"] == -22_000_000
    assert result["action"] == "exclude"


def test_same_amount_and_scope_but_different_seat_sets_do_not_merge():
    first = event(net=0, institution_rows=[seat(0, 1_000_000, 0)])
    second = event(net=0, report_event_id="e2", institution_rows=[seat(0, 2_000_000, 0)])
    assert len(lhb([first, second])["events"]) == 2


def test_overlapping_multi_day_and_single_day_do_not_net_or_sum():
    first = event()
    second = event(
        net=60_000_000, report_event_id="e2", scope_type="multi_day", scope_start="2026-09-06"
    )
    result = lhb([first, second])
    assert len(result["events"]) == 2
    assert result["action"] == "exclude"
    assert result["no_cross_event_netting"]


@pytest.mark.parametrize(
    "patch",
    [
        {"scope_verified": False},
        {"scope_start": None},
        {"scope_type": "unknown"},
        {"units_verified": False},
        {"newness_verified": False},
        {"effective_target_date": None},
        {"published_at": "2026-09-09T09:30:00+08:00"},
        {"historical_import": True},
        {"net_rate": -8.2},
        {"net_amount": float("nan")},
        {"amount": 0},
        {"l_buy": -1},
        {"l_amount": 1},
        {"net_amount": -50_000_000},
        {"complete": False},
    ],
)
def test_untrusted_lhb_is_review_not_hard_exclusion(patch):
    result = lhb([event(**patch)])
    assert result["action"] == "review_required"
    assert result["status"] == "unknown"


def test_lhb_side_sum_exceeding_turnover_not_an_identity_error():
    row = event(net=0, amount=20_000_000, l_buy=30_000_000, l_sell=30_000_000, l_amount=60_000_000)
    result = lhb([row])["events"][0]
    assert all(result["normalization_checks"].values())
    assert result["action"] == "pass"


def test_old_event_remains_history_and_same_target_replay_preserves_restriction():
    row = event()
    assert lhb([row]) == lhb([deepcopy(row)])
    old = event(effective_target_date="2026-09-08")
    result = lhb([old])["events"][0]
    assert result["action"] == "pass"
    assert result["historical_evidence"]


def test_none_disclosed_distinct_from_unknown_institution_response():
    none = lhb([event(net=0)])["events"][0]
    assert none["institution_status"] == "none_disclosed"
    assert none["institution_rank_net_cny"] == 0
    unknown = lhb([event(net=0, institutions_complete=False)])["events"][0]
    assert unknown["institution_status"] == "unknown"
    assert unknown["institution_rank_net_cny"] is None
    assert unknown["action"] == "review_required"
    unknown_name = seat(0, 1_000_000, 0)
    unknown_name["institution"] = None
    assert lhb([event(net=0, institution_rows=[unknown_name])])["action"] == "review_required"


@pytest.mark.parametrize("inline", [False, True])
def test_event_id_cannot_override_contradictory_stock_date_or_scope(inline):
    raw = event(net=0)
    bad_seat = seat(
        1,
        0,
        30_000_000,
        report_event_id="e1",
        ts_code="600999.SH",
        report_date="2026-09-02",
        scope_type="multi_day",
        scope_start="2026-08-01",
        scope_end="2026-09-02",
        scope_verified=False,
    )
    if inline:
        raw["institution_rows"] = [bad_seat]
        result = lhb([raw])
    else:
        del raw["institution_rows"]
        result = normalize_lhb_events(
            [raw], [bad_seat], target_date="2026-09-09", cutoff_at="2026-09-09T08:45:00+08:00"
        )
    assert result["action"] == "review_required"
    assert result["events"][0]["institution_status"] == "unknown"
    assert result["events"][0]["institution_rank_net_cny"] is None


def test_replayed_request_batches_must_select_one_canonical_response():
    rows = [
        seat(1, 0, 13_000_000, response_batch_id="original"),
        seat(1, 0, 13_000_000, response_batch_id="retry"),
    ]
    row = event(net=0, institution_rows=rows)
    assert lhb([row])["action"] == "review_required"
    row["canonical_institution_batch_id"] = "original"
    result = lhb([row])["events"][0]
    assert result["institution_rank_sell_cny"] == 13_000_000
    assert result["action"] == "pass"


def test_unit_receipt_requires_official_range_unit_and_daily_crosscheck():
    daily = {"trade_date": "20260908", "amount": 500_000}
    raw = event(trade_date="20260908")
    kwargs = dict(
        official_source_id="exchange-original",
        official_unit="CNY",
        official_scope_start="2026-09-08",
        official_scope_end="2026-09-08",
        top_inst_unit_confirmed=True,
    )
    assert qualify_lhb_unit_evidence(raw, daily, **kwargs)["units_verified"]
    kwargs["official_source_id"] = None
    assert not qualify_lhb_unit_evidence(raw, daily, **kwargs)["units_verified"]
    kwargs["official_source_id"] = "exchange-original"
    kwargs["official_scope_start"] = "2026-09-06"
    assert not qualify_lhb_unit_evidence(raw, daily, **kwargs)["units_verified"]


def test_large_unlock_uses_total_shares_and_unknown_does_not_mean_no_risk():
    dates = days(8)
    row = {"float_date": dates[4], "float_share": 5_000_000}
    result = evaluate_unlock_risk([row], dates, signal_date=dates[2], total_shares=100_000_000)
    assert result["ratio_total_shares"] == 0.05
    assert result["action"] == "watch_only"
    assert (
        evaluate_unlock_risk([row], dates, signal_date=dates[2], total_shares=None)["status"]
        == "unknown"
    )


def test_high_divergence_finite_and_config_hash_stable():
    metrics = {"ret20": 0.25, "amount_ratio": 2, "upper_shadow": 0.4, "close_location": 0.6}
    assert high_divergence_flag(metrics)
    metrics["upper_shadow"] = float("nan")
    assert high_divergence_flag(metrics) is None
    assert config_hash() == config_hash({"active_risk_max": 0.08})


@pytest.mark.parametrize(
    "config",
    [
        {"active_risk_max": 1},
        {"selective_risk_max": float("nan")},
        {"touch_separation_days": 0},
        {"pivot_right_left_days": 1.5},
        {"geometric_rr_min": -1},
        {"invalidation_atr_multiple": -0.5},
    ],
)
def test_invalid_frozen_geometry_configuration_rejected_before_calculation(config):
    with pytest.raises(ValueError):
        risk_config(config)
