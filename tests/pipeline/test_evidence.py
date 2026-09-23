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


@pytest.mark.parametrize(
    "exit_day,expected,issue",
    [
        ("20200605", [("2020-06-01", "2020-06-04", "SW:old"),
                      ("2020-06-12", "2020-06-30", "SW:new")], "vacancy_unknown"),
        ("20200612", [("2020-06-01", "2020-06-11", "SW:old"),
                      ("2020-06-12", "2020-06-30", "SW:new")], None),
        ("20200620", [("2020-06-01", "2020-06-11", "SW:old"),
                      ("2020-06-12", "2020-06-19", None),
                      ("2020-06-20", "2020-06-30", "SW:new")], "conflict_overlap"),
        ("20200611", [("2020-06-01", "2020-06-10", "SW:old"),
                      ("2020-06-12", "2020-06-30", "SW:new")], "vacancy_unknown"),
    ],
)
def test_industry_intervals_boundary_semantics(exit_day, expected, issue):
    rows = pd.DataFrame([
        dict(con_code="A", in_date="20200601", out_date=exit_day, l1_name="old"),
        dict(con_code="A", in_date="20200612", out_date=None, l1_name="new"),
    ])
    intervals, issues = industry_intervals(rows, {"A"}, end=date(2020, 6, 30), taxonomy="SW")
    assert [(r["start"], r["end"], r["industry"]) for r in intervals] == expected
    assert bool(issues) == (issue is not None)
    if issue:
        assert all(issue in item for item in issues)


def test_nested_and_open_conflicts_survive_fallback_and_order():
    rows = pd.DataFrame([
        dict(con_code="A", in_date="20200601", out_date=None, l1_name="old"),
        dict(con_code="A", in_date="20200605", out_date="20200620", l1_name="new"),
        dict(con_code="A", in_date="20200608", out_date="20200610", l1_name="third"),
    ])
    end = date(2020, 6, 30)
    primary, _ = industry_intervals(rows, {"A"}, end=end, taxonomy="SW")
    reverse, _ = industry_intervals(rows.iloc[::-1], {"A"}, end=end, taxonomy="SW")
    assert primary == reverse
    fallback = [dict(instrument_id="A", start="2020-06-01", end=str(end), industry="fallback")]
    merged, _ = merge_industry_sources(primary, fallback, end=end)
    for day in pd.date_range("2020-06-01", str(end)):
        active = [r for r in merged if r["start"] <= str(day.date()) <= r["end"]]
        assert len(active) == 1
        assert active[0]["industry"] == (None if 5 <= day.day <= 19 else "SW:old")
        if 5 <= day.day <= 19:
            assert active[0]["conflicting_evidence"]
    same = rows.iloc[:2].copy()
    same["l1_name"] = "old"
    known, issues = industry_intervals(same, {"A"}, end=end, taxonomy="SW")
    assert not issues
    assert all(r["industry"] == "SW:old" for r in known)


@pytest.mark.parametrize("calendar", [{}, {date(2023, 6, 1): True},
    {date(2023, 6, 1): True, date(2023, 6, 2): True, date(2023, 9, 25): True}])
def test_real_snapshot_builder_never_bridges_unknown_sessions(tmp_path, evidence_builder, calendar):
    project = {"canonical": tmp_path / "canonical"}
    raw = tmp_path / "evidence/raw/bak_basic"
    raw.mkdir(parents=True)
    pd.DataFrame({"trade_date": ["20230601", "20230925"],
                  "industry": ["software", "software"]}).to_parquet(raw / "A.parquet")
    fills = evidence_builder._bak_basic_fills(project, ["A"], False, calendar)
    merged, _ = merge_industry_sources([], fills, end=date(2023, 9, 30))
    assert [(r["start"], r["end"]) for r in merged] == [
        ("2023-06-01", "2023-06-01"), ("2023-09-25", "2023-09-25")]


def test_snapshot_explicit_holiday_bridge_and_duplicate_conflict(tmp_path, evidence_builder):
    project = {"canonical": tmp_path / "canonical"}
    raw = tmp_path / "evidence/raw/bak_basic"
    raw.mkdir(parents=True)
    pd.DataFrame({"trade_date": ["20230602", "20230605", "20230605"],
                  "industry": ["software", "software", "bank"]}).to_parquet(raw / "A.parquet")
    fills = evidence_builder._bak_basic_fills(project, ["A"], False,
        {date(2023, 6, 3): False, date(2023, 6, 4): False})
    merged, _ = merge_industry_sources([], fills, end=date(2023, 6, 30))
    assert [(r["start"], r["end"], r["industry"]) for r in merged] == [
        ("2023-06-02", "2023-06-04", "bak_basic:software"),
        ("2023-06-05", "2023-06-05", None)]


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


def test_empty_industry_stint_and_invalid_exit_are_not_open_coverage():
    rows = pd.DataFrame([dict(con_code="A", in_date="20200601", out_date="20200601", l1_name="x")])
    intervals, issues = industry_intervals(rows, {"A"}, end=date(2020, 6, 30), taxonomy="SW")
    assert not intervals
    assert any("empty_half_open_interval" in item for item in issues)
    rows["out_date"] = "bad-date"
    with pytest.raises(ValueError, match="invalid industry boundary"):
        industry_intervals(rows, {"A"}, end=date(2020, 6, 30), taxonomy="SW")


def test_offline_classification_cache_missing_or_changed_blocks(
    tmp_path, evidence_builder, cached_classifications, monkeypatch,
):
    from quantlab.data import tushare_provider

    def forbidden(*args, **kwargs):
        pytest.fail("offline evidence compilation attempted a provider request")

    monkeypatch.setattr(tushare_provider, "TushareProvider", forbidden)
    project = {"canonical": tmp_path / "canonical"}
    with pytest.raises(ValueError, match="cached L1 classification missing"):
        evidence_builder._sw_cache(project, ["A"], False)
    folder = cached_classifications(tmp_path)
    path = folder / "SW2021.json"
    path.write_text(path.read_text().replace('"bank"', '"changed"'))
    with pytest.raises(ValueError, match="archive hash mismatch"):
        evidence_builder._sw_cache(project, ["A"], False)


def test_isolated_cli_refuses_existing_evidence(tmp_path, evidence_builder, monkeypatch):
    import sys

    output = tmp_path / "new-evidence"
    output.mkdir()
    artifact = output / "industry_intervals.json"
    artifact.write_text("old immutable evidence")
    monkeypatch.setattr(
        evidence_builder, "load_project", lambda p: {"canonical": tmp_path / "canonical"}
    )
    monkeypatch.setattr(sys, "argv", ["builder", "--project", "unused.json", "industries",
                                     "--output-dir", str(output)])
    with pytest.raises(SystemExit, match="evidence already exists"):
        evidence_builder.main()
    assert artifact.read_text() == "old immutable evidence"


def test_future_open_stint_does_not_break_historical_compilation():
    rows = pd.DataFrame([dict(con_code="A", in_date="20260101", out_date=None, l1_name="bank")])
    intervals, _ = industry_intervals(rows, {"A"}, end=date(2025, 12, 31), taxonomy="SW")
    assert intervals == []


def test_snapshot_null_label_cannot_become_industry_none_string(tmp_path, evidence_builder):
    raw = tmp_path / "evidence/raw/bak_basic"
    raw.mkdir(parents=True)
    pd.DataFrame({"trade_date": ["20230601", "20230602", "20230605"],
                  "industry": ["bank", None, "bank"]}).to_parquet(raw / "A.parquet")
    fills = evidence_builder._bak_basic_fills({"canonical": tmp_path / "canonical"}, ["A"],
        False, {date(2023, 6, 2): True, date(2023, 6, 3): False, date(2023, 6, 4): False})
    merged, _ = merge_industry_sources([], fills, end=date(2023, 6, 30))
    assert [(r["start"], r["end"], r["industry"]) for r in merged] == [
        ("2023-06-01", "2023-06-01", "bak_basic:bank"),
        ("2023-06-05", "2023-06-05", "bak_basic:bank")]


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_distribution_removes_all_candidate_legs(reverse):
    # A combined first row must not survive a disagreement in either leg.
    # Repeating the first row after the conflict must not resurrect it.
    row = dict(ex_date="2023-06-20", record_date="2023-06-19",
               pay_date="2023-06-20", div_listdate="2023-06-21",
               cash_div=1.0, cash_div_tax=1.0, stk_div=0.5)
    conflict = dict(row, cash_div=0.4)
    other = dict(row, ex_date="2023-07-20", record_date="2023-07-19",
                 pay_date="2023-07-20", div_listdate="2023-07-21")
    rows = [row, conflict, row, other]
    if reverse:
        rows.reverse()
    events, _, unresolved = corporate_events(
        pd.DataFrame(rows), "000001.SZ", start=date(2023, 1, 1),
        end=date(2023, 12, 31), source_id="synthetic",
    )
    assert {(e["ex_date"], e["kind"]) for e in events} == {
        ("2023-07-20", "cash_dividend"), ("2023-07-20", "bonus_shares")}
    assert any(u["reason"] == "conflicting_duplicate" and
               u["ex_date"] == "2023-06-20" for u in unresolved)


def test_conflicting_distribution_with_parsed_timestamp_dates_cannot_emit_event():
    # Cached dividend parquet dates are Timestamp, unlike the string dates in
    # the synthetic test above. They must use the same normalized group key.
    first = dict(ex_date=pd.Timestamp("2022-12-22"),
                 record_date=pd.Timestamp("2022-12-21"),
                 pay_date=pd.NaT, div_listdate=pd.Timestamp("2022-12-22"),
                 cash_div=0, cash_div_tax=0, stk_div=0.628)
    second = dict(first, stk_div=0.62)
    events, _, unresolved = corporate_events(
        pd.DataFrame([first, second]), "002122.SZ", start=date(2022, 1, 1),
        end=date(2022, 12, 31), source_id="synthetic",
    )
    assert events == []
    assert any(item["reason"] == "conflicting_duplicate" for item in unresolved)
    assert all(item["record_date"] == "2022-12-21" for item in unresolved)


def test_corporate_reader_rejects_event_in_unresolved_conflict_group(tmp_path):
    import json

    from quantlab.research.ml.io import read_corporate_actions

    artifact = tmp_path / "corporate_actions.json"
    event = {
        "event_id": "shr:002122.SZ:2022-12-22",
        "instrument_id": "002122.SZ",
        "kind": "bonus_shares",
        "record_date": "2022-12-21",
        "ex_date": "2022-12-22",
        "settlement_date": "2022-12-22",
        "source_id": "vendor",
        "share_numerator": 157,
        "share_denominator": 250,
    }
    coverage = {
        "source_id": "vendor", "start": "2022-01-01", "end": "2022-12-31",
        "unresolved": [{
            "instrument_id": "002122.SZ", "ex_date": "2022-12-22",
            "record_date": "2022-12-21", "reason": "conflicting_duplicate",
        }],
    }
    artifact.write_text(json.dumps({"coverage": coverage, "events": [event]}))
    with pytest.raises(ValueError, match="conflicting corporate event remains executable"):
        read_corporate_actions(artifact, date(2022, 12, 22), date(2022, 12, 22))
    artifact.write_text(json.dumps({"coverage": coverage, "events": []}))
    assert read_corporate_actions(artifact, date(2022, 12, 22), date(2022, 12, 22)) == ()


def test_membership_observation_uses_code_valid_on_observation_day(
    tmp_path, evidence_builder,
):
    import json

    root = tmp_path / "raw" / "csi800_weights"
    folder = root / "sample"
    folder.mkdir(parents=True)
    pd.DataFrame({
        "trade_date": ["20231229", "20250228"],
        "con_code": ["302132.SZ", "302132.SZ"],
    }).to_parquet(folder / "weights.parquet", index=False)
    (folder / "observation.json").write_text(json.dumps({"raw_responses": []}))
    observations = evidence_builder._load_observations({"raw": tmp_path / "raw"})
    assert observations == [
        (date(2023, 12, 29), frozenset({"300114.SZ"})),
        (date(2025, 2, 28), frozenset({"302132.SZ"})),
    ]
    assert pd.read_parquet(folder / "weights.parquet").con_code.tolist() == [
        "302132.SZ", "302132.SZ",
    ]
    pd.DataFrame({
        "trade_date": ["20231229", "20231229"],
        "con_code": ["300114.SZ", "302132.SZ"],
    }).to_parquet(folder / "weights.parquet", index=False)
    with pytest.raises(ValueError, match="collapsed observation"):
        evidence_builder._load_observations({"raw": tmp_path / "raw"})


@pytest.mark.parametrize("command,filename", [
    ("membership", "csi800_membership.json"),
    ("availability", "source_availability.parquet"),
    ("execution-policy", "execution_policy.json"),
    ("event-coverage", "event_coverage.json"),
])
def test_all_evidence_cli_outputs_are_isolated(
    tmp_path, evidence_builder, monkeypatch, command, filename,
):
    import hashlib
    import json
    import sys

    day = date(2024, 1, 2)
    project = {"canonical": tmp_path / "canonical", "raw": tmp_path / "raw",
               "receipts": tmp_path / "ingestion", "start": str(day), "end": str(day)}
    (project["raw"] / "csi800_weights").mkdir(parents=True)
    sessions = project["receipts"] / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{day}.json").write_text("{}")
    original = tmp_path / "evidence" / filename
    original.parent.mkdir()
    original.write_bytes(b"old evidence must remain unchanged")
    output = tmp_path / "isolated"
    members = frozenset(f"{i:06d}.SZ" for i in range(800))
    monkeypatch.setattr(evidence_builder, "load_project", lambda _: project)
    monkeypatch.setattr(evidence_builder, "_load_observations", lambda _: [(day, members)])
    monkeypatch.setattr(evidence_builder, "_calendar", lambda _: [day])
    monkeypatch.setattr(evidence_builder, "verify_session", lambda *a: None)

    class Storage:
        def __init__(self, *args):
            pass

        def load_stock_st_v1_by_date(self, day):
            return []

    monkeypatch.setattr(evidence_builder, "ParquetStorage", Storage)
    monkeypatch.setattr(sys, "argv", ["builder", "--project", "unused", command,
                                     "--output-dir", str(output)])
    evidence_builder.main()
    artifact = output / filename
    assert artifact.is_file()
    assert original.read_bytes() == b"old evidence must remain unchanged"
    receipt_name = "membership" if command == "membership" else artifact.stem
    receipt = json.loads((output / "receipts" / f"{receipt_name}.json").read_text())
    assert receipt["artifact"] == str(artifact)
    assert receipt["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    if command == "membership":
        document = json.loads(artifact.read_text())
        assert document["semantics"] == "monthly_observations_not_effective_membership"
        assert document["snapshots"][0]["complete"] is False
    if command == "event-coverage":
        assert json.loads(artifact.read_text())["stock_st"][0]["complete"] is False
    before = artifact.read_bytes()
    with pytest.raises(SystemExit, match="evidence already exists"):
        evidence_builder.main()
    assert artifact.read_bytes() == before
