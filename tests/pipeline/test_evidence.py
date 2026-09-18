"""Evidence builder semantics: dated membership, fees, industries and events."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from quantlab.pipeline.evidence import (
    availability_frame,
    board_group,
    corporate_events,
    coverage_intervals,
    execution_policy_document,
    fee_eras,
    industry_intervals,
    membership_changes,
    membership_document,
    merge_industry_sources,
    scheduled_effective,
)


def _month_calendar(year: int, month: int) -> list[date]:
    days = pd.date_range(f"{year}-{month:02d}-01", periods=28, freq="B")
    return [d.date() for d in days]


def test_scheduled_effective_follows_second_friday_rule():
    calendar = _month_calendar(2024, 6)
    effective = scheduled_effective(calendar, 2024, 6)
    assert effective == date(2024, 6, 17)
    assert scheduled_effective(calendar, 2024, 7) is None


def test_membership_changes_detect_scheduled_and_extraordinary():
    members_a = frozenset(f"{n:06}.SZ" for n in range(800))
    members_b = frozenset({f"{n:06}.SZ" for n in range(1, 799)} | {"800001.SZ", "800002.SZ"})
    observations = [
        (date(2024, 5, 31), members_a),
        (date(2024, 6, 30), members_a),
        (date(2024, 7, 31), members_b),
    ]
    calendar = _month_calendar(2024, 6) + _month_calendar(2024, 7)
    intervals, changes = membership_changes(observations, calendar)
    assert len(intervals) == 2
    assert len(changes) == 1
    change = changes[0]
    assert change.dating_basis == "observation_bounded"
    assert change.effective == date(2024, 7, 31)

    observations_june = [
        (date(2024, 5, 31), members_a),
        (date(2024, 6, 30), members_b),
    ]
    intervals, changes = membership_changes(observations_june, calendar)
    assert changes[0].dating_basis == "observation_bounded"
    assert changes[0].effective == date(2024, 6, 30)
    assert intervals[0][1] == date(2024, 6, 29)
    assert intervals[1][0] == date(2024, 6, 30)
    assert intervals[-1][1] == date(2024, 6, 30)
    for previous, nxt in zip(intervals, intervals[1:], strict=False):
        assert nxt[0] > previous[1]


def test_membership_document_enforces_snapshot_size_and_observations():
    members = frozenset(f"{n:06}.SZ" for n in range(800))
    intervals = [(date(2024, 1, 2), date(9999, 12, 31), "first_observation", members)]
    document = membership_document(
        intervals, observation_dates=[date(2024, 1, 31)], revision_id="r1"
    )
    assert document["schema"] == "quantlab_index_membership_v1"
    assert document["snapshots"][0]["complete"] is False
    assert document["snapshots"][0]["known_at"] is None
    assert document["semantics"] != "published_effective_intervals"
    short = frozenset(f"{n:06}.SZ" for n in range(799))
    with pytest.raises(ValueError, match="800"):
        membership_document(
            [(date(2024, 1, 2), date(9999, 12, 31), "first_observation", short)],
            observation_dates=[date(2024, 1, 31)],
            revision_id="r1",
        )


def test_availability_declares_post_close_publication():
    frame = availability_frame(
        [(date(2024, 1, 2), "a.json"), (date(2024, 1, 3), "b.json")]
    )
    assert list(frame.columns) == ["trade_date", "known_at", "source_id", "revision_id"]
    assert (frame.known_at.dt.hour == 17).all()
    assert frame.known_at.dt.tz is not None
    assert frame.trade_date.is_unique
    with pytest.raises(ValueError, match="duplicate"):
        availability_frame([(date(2024, 1, 2), "a.json"), (date(2024, 1, 2), "b.json")])


def test_fee_eras_cover_the_research_interval_with_sourced_boundaries():
    eras = fee_eras(date(2018, 1, 1), date(2026, 9, 10))
    assert [e.start for e in eras] == [
        date(2018, 1, 1),
        date(2022, 4, 29),
        date(2023, 8, 28),
    ]
    assert eras[-1].sell_stamp_rate == "0.0005"
    assert eras[-2].additional_fee_rate == "0.00001"
    assert all(e.known_at <= e.start for e in eras)


def test_user_declared_commission_is_wan0p86_with_expected_values():
    """万0.86 = 0.0086% = 0.000086 (a prior draft used 0.00086, ten times too large).

    Hand expectations: CNY 100,000 -> 8.60 yuan commission; CNY 20,000 -> the
    CNY 5 stock minimum. Stamp and transfer fees are separate and excluded.
    """
    from decimal import ROUND_HALF_UP

    codes = ["600000.SH", "000001.SZ"]
    document = execution_policy_document(
        codes,
        fee_eras(date(2018, 1, 1), date(2026, 9, 18)),
        start=date(2018, 1, 1),
        end=date(2026, 9, 18),
    )
    for policy in document["policies"]:
        fees = policy["fees"]
        assert fees["commission_rate"] == "0.000086", fees
        assert fees["minimum_commission_fen"] == 500
        assert fees["scenario_id"] == "user_declared_wan0p86_stock_min5_v1"
    rate = Decimal("0.000086")
    for notional_fen, expected in ((10_000_000, 860), (2_000_000, 500)):
        commission = max(Decimal(notional_fen) * rate, Decimal(500)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
        assert commission == expected


def test_execution_policy_covers_every_instrument_and_day():
    codes = [f"600{n:03}.SH" for n in range(10)] + ["000001.SZ", "300750.SZ", "688981.SH"]
    document = execution_policy_document(
        codes,
        fee_eras(date(2018, 1, 1), date(2026, 9, 10)),
        start=date(2018, 1, 1),
        end=date(2026, 9, 10),
    )
    policies = document["policies"]
    for day in (date(2018, 6, 1), date(2022, 4, 29), date(2023, 8, 28), date(2026, 9, 10)):
        for code in codes:
            matches = [
                p
                for p in policies
                if code in p["instruments"]
                and date.fromisoformat(p["start"]) <= day <= date.fromisoformat(p["end"])
            ]
            assert len(matches) == 1, (day, code, len(matches))
    star = next(p for p in policies if "688981.SH" in p["instruments"])
    assert star["rules"]["buy_minimum"] == 200
    assert star["rules"]["buy_increment"] == 1
    chinext = next(p for p in policies if "300750.SZ" in p["instruments"])
    assert chinext["rules"]["buy_minimum"] == 100
    # Every fee component must be DECLARED (zero where absent); a None field
    # would make the quantity kernel stop with fee_components_unknown.
    from decimal import Decimal

    from quantlab.research.quantity_kernel import ResearchFeeScenario

    for policy in policies:
        raw = dict(policy["fees"])
        for key in (
            "commission_rate",
            "buy_stamp_rate",
            "sell_stamp_rate",
            "additional_fee_rate",
            "adverse_slippage_rate",
        ):
            if raw[key] is not None:
                raw[key] = Decimal(str(raw[key]))
        fees = ResearchFeeScenario(
            **{
                **raw,
                "effective_from": date.fromisoformat(raw["effective_from"]),
                "effective_through": date.fromisoformat(raw["effective_through"]),
            }
        )
        assert fees.complete, policy["start"]
    with pytest.raises(ValueError, match="board"):
        board_group("832317.BJ")


def test_corporate_events_extract_cash_and_share_ratios():
    """cash_div is the after-tax net; cash_div_tax is pre-tax and never net.

    A combined cash+share row yields BOTH events; share events settle on the
    vendor listing date (div_listdate), not the cash pay date.
    """
    rows = pd.DataFrame(
        [
            {
                "ex_date": "2023-06-20",
                "record_date": "2023-06-19",
                "pay_date": "2023-06-20",
                "div_listdate": None,
                "cash_div": 0.96,
                "cash_div_tax": 0.96,
                "stk_div": 0.0,
            },
            {
                "ex_date": "2022-06-20",
                "record_date": "2022-06-17",
                "pay_date": "2022-06-20",
                "div_listdate": "2022-06-21",
                "cash_div": 1.0,
                "cash_div_tax": 0.9,
                "stk_div": 0.5,
            },
            {
                "ex_date": "2022-06-20",
                "record_date": "2022-06-17",
                "pay_date": "2022-06-20",
                "div_listdate": "2022-06-21",
                "cash_div": 1.0,
                "cash_div_tax": 0.9,
                "stk_div": 0.5,
            },
            {
                "ex_date": "2021-06-20",
                "record_date": "2021-06-17",
                "pay_date": "2021-06-26",
                "div_listdate": None,
                "cash_div": None,
                "cash_div_tax": 1.0,
                "stk_div": 0.0,
            },
            {
                "ex_date": "2021-08-20",
                "record_date": "2021-08-25",
                "pay_date": "2021-08-26",
                "div_listdate": None,
                "cash_div": 1.0,
                "cash_div_tax": 1.0,
                "stk_div": 0.0,
            },
        ]
    )
    events, skipped, unresolved = corporate_events(
        rows, "000001.SZ", start=date(2021, 1, 1), end=date(2024, 1, 1), source_id="s"
    )
    kinds = {e["kind"] for e in events}
    assert kinds == {"cash_dividend", "bonus_shares"}
    cash = next(e for e in events if e["kind"] == "cash_dividend")
    assert Decimal(cash["net_cash_per_share_fen"]) == Decimal("96")
    combined_cash = next(
        e
        for e in events
        if e["kind"] == "cash_dividend" and e["ex_date"] == "2022-06-20"
    )
    assert combined_cash["settlement_date"] == "2022-06-20"
    share = next(e for e in events if e["kind"] == "bonus_shares")
    assert (share["share_numerator"], share["share_denominator"]) == (1, 2)
    assert share["settlement_date"] == "2022-06-21"
    assert any("duplicate" in item for item in skipped)
    assert any("chronology" in item for item in skipped)
    assert any("pre-tax" in item for item in skipped)
    pretax = [u for u in unresolved if u["reason"] == "after_tax_cash_missing_pretax_only"]
    assert pretax and pretax[0]["ex_date"] == "2021-06-20"


def test_corporate_share_event_without_listing_date_is_skipped():
    rows = pd.DataFrame(
        [
            {
                "ex_date": "2022-06-20",
                "record_date": "2022-06-17",
                "pay_date": "2022-06-20",
                "div_listdate": None,
                "cash_div": None,
                "cash_div_tax": None,
                "stk_div": 0.5,
            }
        ]
    )
    events, skipped, unresolved = corporate_events(
        rows, "000001.SZ", start=date(2021, 1, 1), end=date(2024, 1, 1), source_id="s"
    )
    assert events == []
    assert any("without div_listdate" in item for item in skipped)


def test_industry_intervals_respect_recorded_exits():
    """Stints cover [in_date, out_date): a recorded exit ends coverage.

    The days between a recorded exit and the next assignment stay UNKNOWN
    (disclosed, never bridged with either label), and an open stint runs to
    the project end.
    """
    rows = pd.DataFrame(
        [
            {
                "con_code": "000001.SZ",
                "in_date": "20180101",
                "out_date": "20211210",
                "l1_name": "bank",
            },
            {
                "con_code": "000001.SZ",
                "in_date": "20211213",
                "out_date": None,
                "l1_name": "nonbank",
            },
        ]
    )
    intervals, issues = industry_intervals(
        rows, {"000001.SZ"}, end=date(2026, 9, 10), taxonomy="SW"
    )
    assert len(intervals) == 2
    assert intervals[0]["end"] == "2021-12-09"
    assert intervals[1]["start"] == "2021-12-13"
    assert intervals[1]["end"] == "2026-09-10"
    assert any("vacancy_unknown:2021-12-10:2021-12-12" in i for i in issues)
    # A recorded exit on the final stint also ends coverage instead of
    # extending to the project end.
    rows_closed = pd.DataFrame(
        [
            {
                "con_code": "000001.SZ",
                "in_date": "20180101",
                "out_date": "20201231",
                "l1_name": "bank",
            }
        ]
    )
    closed, _ = industry_intervals(
        rows_closed, {"000001.SZ"}, end=date(2026, 9, 10), taxonomy="SW"
    )
    assert closed[0]["end"] == "2020-12-30"


def test_snapshot_intervals_break_on_observation_gaps():
    """Discontinuous daily observations are never stitched into one interval."""
    rows = pd.DataFrame(
        [
            {
                "con_code": "688088.SH",
                "in_date": "20230601",
                "out_date": "20230609",
                "l1_name": "software",
            },
            {
                "con_code": "688088.SH",
                "in_date": "20230925",
                "out_date": None,
                "l1_name": "software",
            },
        ]
    )
    intervals, issues = industry_intervals(
        rows, {"688088.SH"}, end=date(2026, 9, 10), taxonomy="SW"
    )
    assert len(intervals) == 2
    assert intervals[0]["end"] == "2023-06-08"
    assert intervals[1]["start"] == "2023-09-25"


def test_merge_industry_sources_fills_gaps_without_overlap():
    primary = [
        {
            "instrument_id": "000001.SZ",
            "start": "2021-12-13",
            "end": "2026-09-10",
            "industry": "SW:nonbank",
        }
    ]
    fallback = [
        {
            "instrument_id": "000001.SZ",
            "start": "2015-01-01",
            "end": "2022-01-05",
            "industry": "snapshot:bank",
        }
    ]
    merged, issues = merge_industry_sources(primary, fallback, end=date(2026, 9, 10))
    assert issues == []
    starts = {r["start"] for r in merged}
    assert "2015-01-01" in starts and "2021-12-13" in starts
    filler = next(r for r in merged if r["industry"].startswith("snapshot:"))
    assert filler["end"] == "2021-12-12"
    ordered = sorted(merged, key=lambda r: r["start"])
    for previous, nxt in zip(ordered, ordered[1:], strict=False):
        assert nxt["start"] > previous["end"]


def test_coverage_intervals_keep_explicit_completeness():
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    coverage = coverage_intervals(
        sessions, source_id="s", revision_id="r", complete=False
    )
    assert len(coverage) == 1
    assert coverage[0]["start"] == "2024-01-02"
    assert coverage[0]["end"] == "2024-01-05"
    assert coverage[0]["complete"] is False
    holed = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 2, 20), date(2024, 2, 21)]
    assert len(coverage_intervals(holed, source_id="s", revision_id="r", complete=True)) == 2
    # A missing weekday must not acquire coverage merely because the gap is short.
    holed = [date(2024, 1, 2), date(2024, 1, 4)]
    assert len(coverage_intervals(holed, source_id="s", revision_id="r", complete=True)) == 2
