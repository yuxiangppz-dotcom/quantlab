"""Historical membership, research-to-account bridges and operational failure cases."""

import copy
import json
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest
from test_pipeline import ROOT, SyntheticProvider, engine

from quantlab.data.models import StockSTStatus, SuspensionRecord
from quantlab.data.storage import ParquetStorage
from quantlab.pipeline.features import build_bundle
from quantlab.pipeline.ingestion import synchronize
from quantlab.pipeline.market import market_day
from quantlab.pipeline.strategy import load_strategy
from quantlab.pipeline.universe import compile_universe
from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.io import write_json
from quantlab.research.ml.portfolio import buffered_target


@pytest.fixture
def history(tmp_path, request):
    from test_pipeline import history as original

    return original.__wrapped__(tmp_path, request)


@pytest.mark.parametrize(
    "endpoint,count", [("get_stock_st_by_date", 1000), ("get_suspensions_by_date", 5000)]
)
def test_truncated_event_response_cannot_commit(tmp_path, monkeypatch, endpoint, count):
    provider = SyntheticProvider()
    day = date(2024, 1, 2)
    row = StockSTStatus("000001.SZ", day, "ST", "ST", "ST", "synthetic")
    if "suspensions" in endpoint:
        row = SuspensionRecord("000001.SZ", day, "S", None, "synthetic")
    monkeypatch.setattr(provider, endpoint, lambda d: [row] * count)
    storage, receipts = ParquetStorage(tmp_path / "canonical"), tmp_path / "receipts"
    with pytest.raises(ValueError, match="provider limit"):
        synchronize(provider, storage, receipts, day, day, indices=("000906.SH",))
    assert not (receipts / "sessions" / f"{day}.json").exists()
    assert not storage.daily_bars_exists(day)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": "decision_blocked:no model", "forward_decision": False}, 2),
        ({"status": "paused_accounting_only", "forward_decision": False}, 2),
        ({"status": "account_settled_no_forward_decision", "forward_decision": False}, 2),
        ({"status": "ready", "forward_decision": True}, 0),
    ],
)
def test_daily_exit_code_matches_decision(monkeypatch, tmp_path, payload, expected):
    from quantlab.pipeline import cli

    records = []
    monkeypatch.setattr(cli, "dispatch", lambda args: payload)
    monkeypatch.setattr(cli, "_operation", lambda a, r, state: records.append(state))
    assert (
        cli.main(["--project", str(tmp_path / "p.json"), "daily", "--as-of", "2024-01-02"])
        == expected
    )
    assert records == ["blocked" if expected else "completed"]


@pytest.fixture
def universe_history(tmp_path):
    class FullProvider(SyntheticProvider):
        def get_securities(self):
            base = super().get_securities()[0]
            return [
                replace(base, instrument_id=f"{n:06}.SZ", symbol=f"{n:06}") for n in range(1, 802)
            ]

        def get_stock_st_by_date(self, day):
            return [StockSTStatus("000002.SZ", day, "ST", "ST", "ST", "synthetic")]

    storage, receipts = ParquetStorage(tmp_path / "canonical"), tmp_path / "receipts"
    days = pd.bdate_range("2024-01-02", periods=3)
    synchronize(
        FullProvider(), storage, receipts, days[0].date(), days[-1].date(), indices=("000906.SH",)
    )
    provenance = {
        "known_at": "2023-12-01T00:00:00+08:00",
        "source_id": "synthetic",
        "revision_id": "1",
    }
    membership = {
        "schema": "quantlab_index_membership_v1",
        "index": "000906.SH",
        "semantics": "published_effective_intervals",
        "snapshots": [
            {
                **provenance,
                "start": "2024-01-02",
                "end": "2024-01-03",
                "complete": True,
                "members": [f"{n:06}.SZ" for n in range(1, 801)],
            },
            {
                **provenance,
                "start": "2024-01-04",
                "end": "2024-01-04",
                "complete": True,
                "members": [f"{n:06}.SZ" for n in range(2, 802)],
            },
        ],
    }
    write_json(tmp_path / "members.json", membership)
    intervals = [
        {
            **provenance,
            "instrument_id": f"{n:06}.SZ",
            "start": "2020-01-01",
            "end": "2025-01-01",
            "industry": "synthetic",
        }
        for n in range(1, 802)
    ]
    write_json(tmp_path / "industry.json", {"intervals": intervals})
    write_json(
        tmp_path / "events.json",
        {
            "schema": "quantlab_event_coverage_v1",
            "stock_st": [
                {**provenance, "start": "2024-01-01", "end": "2025-01-01", "complete": True}
            ],
        },
    )
    pd.DataFrame(
        {
            "trade_date": days,
            "known_at": days.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=17),
            "source_id": "synthetic",
            "revision_id": "1",
        }
    ).to_parquet(tmp_path / "availability.parquet")
    strategy = {
        "membership": tmp_path / "members.json",
        "industries": tmp_path / "industry.json",
        "event_coverage": tmp_path / "events.json",
        "min_listing_sessions": 1,
        "liquidity_window": 2,
        "min_median_amount_cny": 100000,
        "policy_sha256": "a" * 64,
    }
    return storage, receipts, strategy, days


def compile_fixture(tmp_path, fixture, name="universe"):
    storage, receipts, strategy, days = fixture
    return compile_universe(
        storage,
        receipts,
        tmp_path / "availability.parquet",
        strategy,
        tmp_path / name,
        days[1].date(),
        days[2].date(),
        hour=18,
        code_changes_path=ROOT / "config/security_code_changes.csv",
    )


def test_membership_is_historical_and_removed_holdings_remain(universe_history, tmp_path):
    industry = json.loads((tmp_path / "industry.json").read_text())
    industry["intervals"][0]["end"] = "2024-01-03"
    (tmp_path / "industry.json").write_text(json.dumps(industry))
    output = compile_fixture(tmp_path, universe_history)
    frame = pd.read_parquet(output / "context.parquet").set_index(["trade_date", "instrument_id"])
    assert frame.loc[("2024-01-03", "000001.SZ"), "eligible"]
    assert not frame.loc[("2024-01-04", "000001.SZ"), "eligible"]
    assert frame.loc[("2024-01-04", "000001.SZ"), "soft_exit"]
    assert not frame.loc[("2024-01-04", "000001.SZ"), "must_exit"]
    assert pd.isna(frame.loc[("2024-01-04", "000001.SZ"), "industry"])
    assert frame.loc[("2024-01-04", "000801.SZ"), "eligible"]
    assert frame.loc[("2024-01-04", "000002.SZ"), "must_exit"]
    assert not frame.loc[("2024-01-04", "000002.SZ"), "eligible"]
    assert compile_fixture(tmp_path, universe_history) == output


@pytest.mark.parametrize("problem", ["late", "incomplete", "unknown_st", "overlap", "monthly"])
def test_unproven_universe_cannot_be_published(universe_history, tmp_path, problem):
    path = tmp_path / "members.json"
    value = json.loads(path.read_text())
    if problem == "late":
        value["snapshots"][0]["known_at"] = "2024-01-05T00:00:00+08:00"
    elif problem == "incomplete":
        value["snapshots"][0]["members"].pop()
    elif problem == "overlap":
        value["snapshots"][0]["end"] = "2024-01-04"
    elif problem == "monthly":
        value["semantics"] = "monthly_weight_snapshots"
    else:
        event = json.loads((tmp_path / "events.json").read_text())
        event["stock_st"][0]["complete"] = False
        (tmp_path / "events.json").write_text(json.dumps(event))
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        compile_fixture(tmp_path, universe_history)
    assert not (tmp_path / "universe").exists()


def test_universe_state_survives_shared_feature_builder(history, tmp_path):
    storage, receipts, days = history
    context = pd.read_parquet(tmp_path / "context.parquet")
    context["can_open"], context["must_exit"], context["soft_exit"] = False, False, False
    context["eligibility_reason"], context["universe_policy_sha256"] = "liquidity", "a" * 64
    retired = context.loc[context.instrument_id.eq("000001.SZ")].copy()
    retired["instrument_id"], retired["eligible"], retired["soft_exit"] = "RETIRED.SZ", False, True
    context = pd.concat([context, retired], ignore_index=True)
    context.to_parquet(tmp_path / "context.parquet", index=False)
    build_bundle(
        storage,
        receipts,
        tmp_path / "context.parquet",
        tmp_path / "availability.parquet",
        tmp_path / "bundle",
        days[60].date(),
        days[-1].date(),
        MLConfig(decision_hour=18),
        root=ROOT,
        engine=engine,
        batch_sessions=5,
    )
    frame = pd.read_parquet(tmp_path / "bundle/features.parquet")
    assert not frame.can_open.any()
    assert frame.loc[frame.instrument_id.eq("RETIRED.SZ"), "soft_exit"].all()
    assert not frame.loc[frame.instrument_id.eq("RETIRED.SZ"), "eligible"].any()
    assert (
        json.loads((tmp_path / "bundle/feature_contract.json").read_text())[
            "universe_policy_sha256"
        ]
        == "a" * 64
    )


def test_retired_unheld_market_scope_does_not_create_prices(history, tmp_path):
    storage, receipts, days = history
    path = tmp_path / "actions.json"
    write_json(
        path,
        {
            "coverage": {
                "source_id": "test",
                "start": str(days[0].date()),
                "end": str(days[-1].date()),
            },
            "events": [],
        },
    )
    sessions = list(days.date)
    payload = market_day(
        storage,
        receipts,
        sessions,
        sessions[60],
        {"RETIRED.SZ"},
        {"policies": []},
        path,
        hour=18,
        sparse_scope=True,
    )
    assert payload["marks"] == payload["contexts"] == []
    with pytest.raises(ValueError, match="execution policy"):
        market_day(
            storage,
            receipts,
            sessions,
            sessions[60],
            {"RETIRED.SZ"},
            {"policies": []},
            path,
            hour=18,
        )


def test_liquidity_blocks_entries_without_forcing_sale_and_soft_exit_waits():
    cross = pd.DataFrame(
        {
            "instrument_id": ["A", "B"],
            "eligible": [False, True],
            "industry": ["x", "x"],
            "score": [np.nan, 1.0],
            "can_open": [False, False],
            "must_exit": [False, False],
            "soft_exit": [True, False],
        }
    )
    config = MLConfig(
        max_positions=2,
        entry_rank=2,
        exit_rank=4,
        max_replacements=1,
        max_weight=0.5,
        max_industry_weight=1.0,
        min_hold_sessions=10,
    )
    day = date(2024, 1, 2)
    young = buffered_target(day, cross, {"A": 0.4}, {"A": 1}, config)
    assert young.target.positions[0].instrument_id == "A"
    mature = buffered_target(day, cross, {"A": 0.4}, {"A": 11}, config)
    assert not mature.target.positions  # B is liquid-ineligible; remain cash.
    cross.loc[0, "must_exit"] = True
    forced = buffered_target(day, cross, {"A": 0.4}, {"A": 1}, config)
    assert not forced.target.positions and forced.risk_reduction_one_way_turnover == 0.2


def test_known_halt_carry_is_only_a_mark_and_action_requires_explicit_valuation(
    history, tmp_path, monkeypatch
):
    storage, receipts, days = history
    sessions = [d.date() for d in days]
    day = sessions[60]
    original = storage.load_daily_bars_by_date
    monkeypatch.setattr(
        storage, "load_daily_bars_by_date", lambda d: [] if d == day else original(d)
    )
    monkeypatch.setattr(
        storage,
        "load_suspensions_v1_by_date",
        lambda d: [SuspensionRecord("000001.SZ", d, "S", None, "test")],
    )
    policy = {
        "policies": [
            {
                "instruments": ["000001.SZ"],
                "start": "2024-01-01",
                "end": "2025-01-01",
                "known_at": "2024-01-01T00:00:00+08:00",
                "source_id": "test",
                "participation": ".01",
                "rules": None,
                "fees": None,
            }
        ],
        "stale_valuation": {
            "mode": "last_raw_close_known_halt",
            "max_sessions": 5,
            "known_at": "2024-01-01T00:00:00+08:00",
            "source_id": "test",
        },
    }
    corporate = {
        "coverage": {"source_id": "test", "start": "2024-01-01", "end": "2025-01-01"},
        "events": [],
    }
    path = tmp_path / "corporate.json"
    path.write_text(json.dumps(corporate))
    result = market_day(storage, receipts, sessions, day, {"000001.SZ"}, policy, path, hour=18)
    assert result["marks"][0]["price_fen"] == 1000
    assert result["contexts"][0]["raw_close_fen"] is None
    assert result["contexts"][0]["market_open"] is False
    corporate["events"] = [
        {
            "event_id": "d",
            "instrument_id": "000001.SZ",
            "kind": "cash_dividend",
            "record_date": str(sessions[59]),
            "ex_date": str(day),
            "settlement_date": str(day),
            "source_id": "test",
            "net_cash_per_share_fen": "10",
        }
    ]
    path.write_text(json.dumps(corporate))
    assert not market_day(storage, receipts, sessions, day, {"000001.SZ"}, policy, path, hour=18)[
        "marks"
    ]


def test_release_rejects_cash_only_missing_risk_and_partial_replays():
    from quantlab.pipeline.research import assess_reports

    strategy = {
        "capital_scenarios_cny": [200000],
        "release": {"min_sessions": 60, "max_drawdown": 0.25, "min_relative_return": 0},
    }
    report = {
        "scenario_statuses": [
            {
                "model": "ridge",
                "capital_fen": 20000000,
                "stop_reason": None,
                "valid_through": "2024-12-31",
            }
        ],
        "scenarios": [
            {
                "scenario": "ridge-20000000fen",
                "first_session": "2024-01-02",
                "last_session": "2024-12-31",
                "excluded_sessions": 0,
                "sessions": 250,
                "max_drawdown": -0.1,
                "relative_wealth_return": 0.1,
                "risk_breach_days": 0,
                "mean_gross_exposure": 0.7,
                "style_unknown_days": 0,
            }
        ],
        "signal_uncertainty": {"ridge": {"status": "computed", "mean": 0.03}},
        "exposures_sha256": "hash",
    }
    assert not assess_reports([("base", report)], "ridge", strategy, "2024-01-02", "2024-12-31")
    broken = copy.deepcopy(report)
    broken["scenarios"][0]["mean_gross_exposure"] = 0
    broken["scenarios"][0]["style_unknown_days"] = 2
    broken["scenario_statuses"][0]["stop_reason"] = "missing_mark"
    assert (
        len(assess_reports([("stress", broken)], "ridge", strategy, "2024-01-02", "2024-12-31"))
        == 3
    )


def test_default_strategy_is_csi800_and_phase_does_not_redefine_investment_policy():
    from quantlab.pipeline.config import load_project

    project = load_project(ROOT / "config/project.example.json")
    strategy = load_strategy(project)
    assert project["benchmark"] == strategy["index"] == "000906.SH"
    assert strategy["capital_scenarios_cny"] == [50000, 200000, 1000000]


def test_monitor_excludes_immature_labels_but_counts_missing_endpoints():
    from quantlab.pipeline.monitoring import mature_panel

    days = pd.bdate_range("2024-01-02", periods=8)
    config = MLConfig(horizon_sessions=2)
    scores = pd.DataFrame(
        {
            "trade_date": [days[0], days[0], days[4]],
            "instrument_id": ["A", "B", "A"],
            "score": [1.0, 2.0, 3.0],
        }
    )
    prices = pd.DataFrame(
        {"trade_date": days, "instrument_id": "A", "adj_close": np.arange(10, 18.0)}
    )
    mature = mature_panel(scores, prices, days, config, days[4].date())
    assert len(mature) == 2
    assert mature.raw_label.notna().sum() == 1
    prices.loc[prices.trade_date > days[4], "adj_close"] *= 100
    pd.testing.assert_frame_equal(
        mature, mature_panel(scores, prices, days, config, days[4].date())
    )


def test_model_outage_preserves_sell_only_hard_risk_plan(tmp_path, monkeypatch):
    from quantlab.research.ml import service, serving
    from quantlab.research.ml.corporate import new_state
    from quantlab.research.quantity_kernel import ResearchBook, ResearchLot

    calendar = list(pd.bdate_range("2024-01-02", periods=3).date)
    asof = calendar[1]
    clock = pd.Timestamp(asof).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=17)
    monkeypatch.setattr(serving, "now", lambda: clock)

    def unavailable(*args):
        raise ValueError("model expired")

    monkeypatch.setattr(serving, "selected_model", unavailable)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    pd.DataFrame(
        {
            "trade_date": [pd.Timestamp(asof)],
            "instrument_id": ["A"],
            "eligible": [False],
            "can_open": [False],
            "must_exit": [True],
            "soft_exit": [False],
            "industry": ["x"],
            "feature_available_at": [clock],
        }
    ).to_parquet(inputs / "features.parquet")
    book = ResearchBook(asof, 100000, (ResearchLot("a", "A", 100, calendar[0], calendar[1]),))
    config = MLConfig(decision_hour=18)
    plan, status, _, _ = service._plan_decision(
        tmp_path / "account",
        inputs,
        tmp_path / "registry",
        asof,
        {"book": book, "marks": {"A": 1000}, "corporate_state": new_state()},
        calendar,
        {
            "config": config.payload(),
            "inception": str(calendar[0]),
            "release_strategy_sha256": "a" * 64,
        },
        {},
        signal=tmp_path / "signal",
        code={"synthetic": True},
        verify_code=None,
    )
    assert status.startswith("decision_blocked:model expired")
    assert status.endswith("risk_reduction_only")
    assert len(plan["orders"]) == 1 and plan["orders"][0].side == "sell"


def test_index_monthly_observation_is_not_certified_membership(tmp_path):
    from quantlab.data.transport import ObservedClient
    from quantlab.pipeline.observations import index_observations

    class Client:
        calls = 0

        def index_weight(self, **kwargs):
            self.calls += 1
            return pd.DataFrame(
                {
                    "index_code": "000906.SH",
                    "trade_date": "20240103",
                    "con_code": [f"{n:06}.SZ" for n in range(1, 801)],
                    "weight": 0.125,
                }
            )

    client = Client()
    observed = ObservedClient(client, archive=tmp_path / "raw", interval=0)
    first = index_observations(
        observed, tmp_path / "observations", date(2024, 1, 1), date(2024, 1, 31)
    )
    assert not first["effective_membership_certified"]
    assert (
        index_observations(observed, tmp_path / "observations", date(2024, 1, 1), date(2024, 1, 31))
        == first
    )
    assert client.calls == 1


def test_backup_detects_tampering_and_refuses_recursive_destination(tmp_path):
    from quantlab.pipeline.config import PATHS
    from quantlab.pipeline.operations import backup, verify_backup

    project = json.loads((ROOT / "config/project.example.json").read_text())
    project.pop("strategy")
    project["schema"] = "quantlab_project_v1"
    roots = {"canonical", "raw", "receipts", "workspace", "account", "registry"}
    for key in PATHS:
        project[key] = key
        path = tmp_path / key
        if key in roots:
            path.mkdir()
            (path / "fixture.txt").write_text("synthetic recovery snapshot")
        else:
            path.write_text("synthetic fixture")
    source = tmp_path / "project.json"
    source.write_text(json.dumps(project))
    with pytest.raises(ValueError, match="disjoint"):
        backup(source, tmp_path / "account/copy")
    result = backup(source, tmp_path / "backup")
    assert result["status"] == "complete" and result["restore_drill_completed"] is False
    (tmp_path / "backup/account/fixture.txt").write_text("tampered")
    with pytest.raises(ValueError):
        verify_backup(tmp_path / "backup")
