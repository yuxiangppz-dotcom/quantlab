"""Synthetic source-to-artifact integration, fault recovery and temporal boundaries."""

import json
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DailyPriceLimit,
    IndexDailyBar,
    Security,
    SuspensionRecord,
    TradingCalendar,
)
from quantlab.data.storage import ParquetStorage
from quantlab.data.transport import ObservedClient, ProviderRequestError
from quantlab.pipeline.features import build_bundle
from quantlab.pipeline.ingestion import synchronize, verify_session
from quantlab.pipeline.market import market_day
from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.io import verify_bundle

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


class SyntheticProvider:
    def __init__(self):
        self.calls = 0
        self.bad = None

    def get_trading_calendar(self, start, end):
        return [
            TradingCalendar(exchange, d.date(), d.weekday() < 5)
            for d in pd.date_range(start, end)
            for exchange in ("SSE", "SZSE")
        ]

    def get_securities(self):
        return [
            Security(
                f"{n:06}.SZ",
                f"{n:06}",
                "synthetic",
                "SZSE",
                "SZ",
                "主板",
                "L",
                date(2000, 1, 1),
                None,
            )
            for n in (1, 2)
        ]

    def get_daily_bars_by_date(self, day):
        self.calls += 1
        return [
            DailyBar(s.instrument_id, day, 10, 11, 9, 10, 10, 10000, 100000)
            for s in self.get_securities()
        ]

    def get_adj_factors_by_date(self, day):
        return [AdjFactor(s.instrument_id, day, 1) for s in self.get_securities()]

    def get_daily_basic_by_date(self, day):
        return [DailyBasic(s.instrument_id, day, 0.01, 1e9, 8e8) for s in self.get_securities()]

    def get_index_daily(self, code, start, end):
        return [IndexDailyBar(code, start, 100, 101, 99, 100, 100, 1000, 10000)]

    def get_daily_price_limits_by_date(self, day):
        if day == self.bad:
            return []
        return [
            DailyPriceLimit(
                s.instrument_id, day, 10, 11, 9, "SZSE", "synthetic", f"{day}-{s.instrument_id}"
            )
            for s in self.get_securities()
        ]

    def get_stock_st_by_date(self, day):
        return []

    def get_suspensions_by_date(self, day):
        return []


def test_transport_retries_redacts_and_records_actual_response(tmp_path):
    class Client:
        count = 0

        def daily(self, **kwargs):
            self.count += 1
            if self.count < 3:
                raise TimeoutError("secret-token")
            return pd.DataFrame({"ts_code": ["000001.SZ"]})

    # Credentials in an error must never be printed; generic timeouts remain retryable.
    client = Client()

    def transient(**kwargs):
        client.count += 1
        if client.count < 3:
            raise TimeoutError("temporary")
        return pd.DataFrame({"ts_code": ["000001.SZ"]})

    client.daily = transient
    wrapped = ObservedClient(client, archive=tmp_path, sleep=lambda _: None)
    assert len(wrapped.daily(trade_date="20240102")) == 1
    assert client.count == 3
    receipt = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert receipt["observed_at"] and not receipt["historical_publication_certified"]

    def denied(**kwargs):
        raise RuntimeError("permission denied secret-token")

    client.daily = denied
    with pytest.raises(ProviderRequestError, match="permission_denied") as error:
        wrapped.daily(trade_date="20240102")
    assert "secret-token" not in str(error.value)


def test_ingestion_validates_before_commit_recovers_and_detects_tampering(tmp_path):
    provider, storage, receipts = (
        SyntheticProvider(),
        ParquetStorage(tmp_path / "canonical"),
        tmp_path / "receipts",
    )
    first, second = date(2024, 1, 2), date(2024, 1, 3)
    provider.bad = second
    with pytest.raises(ValueError, match="empty"):
        synchronize(provider, storage, receipts, first, second, indices=("000300.SH",))
    verify_session(storage, receipts, first)
    assert not storage.daily_bars_exists(second)
    provider.bad = None
    synchronize(provider, storage, receipts, first, second, indices=("000300.SH",))
    before = provider.calls
    synchronize(provider, storage, receipts, first, second, indices=("000300.SH",))
    assert provider.calls == before
    storage.daily_bars_path(first).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="partition changed"):
        verify_session(storage, receipts, first)


@pytest.fixture
def history(tmp_path, request):
    days = pd.bdate_range("2024-01-02", periods=getattr(request, "param", 75))
    provider, storage, receipts = (
        SyntheticProvider(),
        ParquetStorage(tmp_path / "canonical"),
        tmp_path / "receipts",
    )
    synchronize(
        provider, storage, receipts, days[0].date(), days[-1].date(), indices=("000300.SH",)
    )
    context = pd.DataFrame(
        [
            {
                "trade_date": day,
                "instrument_id": s.instrument_id,
                "eligible": True,
                "industry": "test",
                "known_at": day.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15),
                "source_id": "synthetic-pit",
                "revision_id": "fixture",
            }
            for day in days[60:]
            for s in provider.get_securities()
        ]
    )
    context.to_parquet(tmp_path / "context.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": days,
            "known_at": days.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=17),
            "source_id": "synthetic-close",
            "revision_id": "fixture",
        }
    ).to_parquet(tmp_path / "availability.parquet", index=False)
    return storage, receipts, days


def engine(raw, sessions, securities, contract, scratch, *, code_changes=()):
    frame = raw[["trade_date", "instrument_id"]].copy()
    return pd.concat(
        [frame, pd.DataFrame({f["name"]: np.ones(len(frame)) for f in contract["features"]})],
        axis=1,
    )


def test_shared_bridge_has_exact_contract_and_rejects_late_source(history, tmp_path):
    storage, receipts, days = history
    args = (storage, receipts, tmp_path / "context.parquet", tmp_path / "availability.parquet")
    config = MLConfig(decision_hour=18)
    output = tmp_path / "bundle"
    build_bundle(*args, output, days[60].date(), days[-1].date(), config, root=ROOT, engine=engine)
    manifest = verify_bundle(output)
    assert str(ROOT / "config/security_code_changes.csv") in manifest["provenance"]["inputs"]
    assert len(pd.read_parquet(output / "features.parquet")) == 30
    with pytest.raises(ValueError, match="cutoff"):
        build_bundle(
            *args,
            tmp_path / "early",
            days[60].date(),
            days[-1].date(),
            replace(config, decision_hour=16),
            root=ROOT,
            engine=engine,
        )
    build_bundle(
        *args,
        tmp_path / "late",
        days[60].date(),
        days[-1].date(),
        config,
        root=ROOT,
        engine=engine,
        forward=True,
    )
    late = pd.read_parquet(tmp_path / "late/features.parquet")
    assert (pd.to_datetime(late.feature_available_at, utc=True).dt.year >= 2026).all()


def test_market_adapter_preserves_units_and_missing_policies_block(history, tmp_path, monkeypatch):
    storage, receipts, days = history
    corporate = tmp_path / "corporate.json"
    corporate.write_text(
        json.dumps(
            {
                "coverage": {
                    "source_id": "synthetic",
                    "start": str(days[0].date()),
                    "end": str(days[-1].date()),
                },
                "events": [],
            }
        )
    )
    policy = {
        "policies": [
            {
                "instruments": ["000001.SZ", "000002.SZ"],
                "start": "2024-01-01",
                "end": "2024-12-31",
                "known_at": "2024-01-01T00:00:00+08:00",
                "source_id": "synthetic",
                "participation": "0.01",
                "rules": None,
                "fees": None,
            }
        ]
    }
    sessions = [d.date() for d in days]
    result = market_day(
        storage, receipts, sessions, sessions[60], {"000001.SZ"}, policy, corporate, hour=18
    )
    assert result["marks"][0]["price_fen"] == 1000
    assert result["contexts"][0]["prior20_amount_fen"] == 10000000
    assert result["contexts"][0]["session_volume_shares"] == 10000
    assert result["contexts"][0]["fees"] is None  # unknown stays unknown, kernel blocks
    monkeypatch.setattr(
        storage,
        "load_suspensions_v1_by_date",
        lambda day: [SuspensionRecord("000001.SZ", day, "R", None, "synthetic-resumption")],
    )
    resumed = market_day(
        storage, receipts, sessions, sessions[60], {"000001.SZ"}, policy, corporate, hour=18
    )
    assert resumed["contexts"][0]["market_open"] is True
    with pytest.raises(ValueError, match="execution policy"):
        market_day(
            storage, receipts, sessions, sessions[60], {"000003.SZ"}, policy, corporate, hour=18
        )


def test_prepared_transaction_recovers_without_provider_refetch(tmp_path, monkeypatch):
    from quantlab.pipeline import ingestion

    provider = SyntheticProvider()
    storage, receipts = ParquetStorage(tmp_path / "canonical"), tmp_path / "receipts"
    day = date(2024, 1, 2)
    original = ingestion._commit_pending

    def interrupted(*args):
        raise RuntimeError("synthetic crash before commit")

    monkeypatch.setattr(ingestion, "_commit_pending", interrupted)
    with pytest.raises(RuntimeError, match="crash"):
        synchronize(provider, storage, receipts, day, day, indices=("000300.SH",))
    assert not storage.daily_bars_exists(day)
    before = provider.calls
    monkeypatch.setattr(ingestion, "_commit_pending", original)
    synchronize(provider, storage, receipts, day, day, indices=("000300.SH",))
    assert provider.calls == before
    verify_session(storage, receipts, day)


def test_batched_and_single_feature_paths_agree(history, tmp_path):
    storage, receipts, days = history
    args = (storage, receipts, tmp_path / "context.parquet", tmp_path / "availability.parquet")
    config = MLConfig(decision_hour=18)
    for name, batch in [("whole", 63), ("batch", 5)]:
        build_bundle(
            *args,
            tmp_path / name,
            days[60].date(),
            days[-1].date(),
            config,
            root=ROOT,
            engine=engine,
            batch_sessions=batch,
        )
    for name in ("features.parquet", "prices.parquet", "pit_lineage.parquet"):
        left, right = (pd.read_parquet(tmp_path / folder / name) for folder in ("whole", "batch"))
        sort = ["trade_date", "instrument_id"] + (["source_id"] if "source_id" in left else [])
        pd.testing.assert_frame_equal(
            left.sort_values(sort).reset_index(drop=True),
            right.sort_values(sort).reset_index(drop=True),
        )


def test_publication_cutoff_is_bound_to_sealed_payload(tmp_path):
    from quantlab.research.ml.artifacts import complete, record_publication, verify_publication

    folder = tmp_path / "published"
    folder.mkdir()
    (folder / "prediction.json").write_text(json.dumps({"decision_hour": 18}))
    complete(folder)
    day = date(2024, 1, 2)
    record_publication(folder, day, lambda: pd.Timestamp("2024-01-02T17:30:00+08:00"), 18)
    assert verify_publication(folder, day)["forward_eligible"]
    path = folder / "published.json"
    receipt = json.loads(path.read_text())
    receipt["decision_hour"] = 23
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="sealed policy"):
        verify_publication(folder, day)


@pytest.mark.skipif(
    __import__("os").environ.get("QUANTLAB_TEST_OPTIONAL_RESEARCH") != "1",
    reason="pinned optional Qlib runtime",
)
def test_native_history_daily_feature_parity(history, tmp_path):
    from quantlab.pipeline.features import native_features

    storage, receipts, days = history
    args = (storage, receipts, tmp_path / "context.parquet", tmp_path / "availability.parquet")
    config = MLConfig(decision_hour=18)
    build_bundle(
        *args,
        tmp_path / "native_history",
        days[60].date(),
        days[-1].date(),
        config,
        root=ROOT,
        engine=native_features,
    )
    build_bundle(
        *args,
        tmp_path / "native_daily",
        days[-1].date(),
        days[-1].date(),
        config,
        root=ROOT,
        engine=native_features,
    )
    history_frame = pd.read_parquet(tmp_path / "native_history/features.parquet")
    daily_frame = pd.read_parquet(tmp_path / "native_daily/features.parquet")
    pd.testing.assert_frame_equal(
        history_frame.loc[history_frame.trade_date.eq(days[-1])].reset_index(drop=True),
        daily_frame.reset_index(drop=True),
        rtol=1e-5,
        atol=1e-6,
    )


@pytest.mark.parametrize("history", [125], indirect=True)
def test_project_data_train_replay_report_and_account_chain(history, tmp_path, monkeypatch):
    from quantlab.pipeline import workflow
    from quantlab.pipeline.cli import dispatch, parser
    from quantlab.research.ml import cli as ml_cli
    from quantlab.research.ml import runner, service
    from quantlab.research.ml.io import write_json

    monkeypatch.setattr(runner, "code_identity", lambda root: {"synthetic": True})
    monkeypatch.setattr(ml_cli, "code_identity", lambda root: {"synthetic": True})
    storage, receipts, days = history
    context = pd.read_parquet(tmp_path / "context.parquet")
    context["can_open"], context["must_exit"], context["soft_exit"] = True, False, False
    context["eligibility_reason"], context["universe_policy_sha256"] = "synthetic", "a" * 64
    context.to_parquet(tmp_path / "context.parquet", index=False)
    config = MLConfig(
        decision_hour=18,
        horizon_sessions=2,
        train_sessions=22,
        validation_sessions=10,
        min_cross_section=2,
        models=("ridge",),
        min_data_in_leaf=1,
        max_positions=2,
        entry_rank=2,
        exit_rank=4,
        max_replacements=1,
        max_weight=0.5,
        max_industry_weight=1,
        max_one_way_turnover=1,
        min_hold_sessions=0,
        min_trade_fen=0,
    )
    write_json(tmp_path / "ml.json", config.payload())
    corporate = {
        "coverage": {
            "source_id": "synthetic",
            "start": str(days[0].date()),
            "end": str(days[-1].date()),
        },
        "events": [],
    }
    write_json(tmp_path / "corporate.json", corporate)
    dates = {"effective_from": "2024-01-01", "effective_through": "2024-12-31"}
    policy = {
        "schema": "quantlab_execution_evidence_v1",
        "policies": [
            {
                "instruments": ["000001.SZ", "000002.SZ"],
                "start": "2024-01-01",
                "end": "2024-12-31",
                "known_at": "2024-01-01T00:00:00+08:00",
                "source_id": "synthetic",
                "participation": "0.01",
                "rules": {
                    **dates,
                    "scenario_id": "synthetic",
                    "buy_minimum": 100,
                    "buy_increment": 100,
                    "sell_minimum": 100,
                    "sell_increment": 100,
                    "max_order_quantity": 1000000,
                    "full_position_odd_exit": True,
                },
                "fees": {
                    **dates,
                    "scenario_id": "synthetic",
                    "commission_rate": "0.0003",
                    "minimum_commission_fen": 500,
                    "buy_stamp_rate": "0",
                    "sell_stamp_rate": "0.0005",
                    "additional_fee_rate": "0",
                    "additional_fee_fixed_fen": 0,
                    "adverse_slippage_rate": "0.001",
                },
            }
        ],
    }
    write_json(tmp_path / "policy.json", policy)
    project = {
        "schema": "quantlab_project_v1",
        "canonical": "canonical",
        "raw": "raw",
        "receipts": "receipts",
        "workspace": "workspace",
        "account": "account",
        "registry": "registry",
        "ml_config": "ml.json",
        "context": "context.parquet",
        "availability": "availability.parquet",
        "execution_policy": "policy.json",
        "corporate_actions": "corporate.json",
        "indices": ["000300.SH"],
        "benchmark": "000300.SH",
        "start": str(days[60].date()),
        "end": str(days[-1].date()),
        "test_start": str(days[108].date()),
        "test_end": str(days[116].date()),
        "capital_cny": 200000,
        "provider_interval": 0,
    }
    path = tmp_path / "project.json"
    write_json(path, project)
    original = workflow.build_bundle

    def synthetic_features(*args, **kwargs):
        return original(*args, **kwargs, engine=engine)

    monkeypatch.setattr(workflow, "build_bundle", synthetic_features)

    def run(*argv):
        return dispatch(parser().parse_args(["--project", str(path), *argv]))

    run("build")
    # Use the actual study reservation path for a synthetic end-to-end report.
    from quantlab.research.ml.study import initialize

    initialize(tmp_path / "workspace/study", days[116].date(), days[117].date(), days[124].date())
    ml_cli.dispatch(
        ml_cli.parser().parse_args(
            [
                "train",
                "--bundle",
                str(tmp_path / "workspace/bundle"),
                "--config",
                str(tmp_path / "ml.json"),
                "--start",
                project["test_start"],
                "--end",
                project["test_end"],
                "--study",
                str(tmp_path / "workspace/study"),
                "--output",
                str(tmp_path / "workspace/training"),
            ]
        )
    )
    run("baseline")
    run("replay")
    run("report")
    enriched = ml_cli.dispatch(
        ml_cli.parser().parse_args(
            [
                "report",
                "--replay",
                str(tmp_path / "workspace/replay"),
                "--benchmark",
                str(tmp_path / "workspace/market/benchmark.parquet"),
                "--training",
                str(tmp_path / "workspace/training"),
                "--baseline-replay",
                str(tmp_path / "workspace/baseline"),
                "--bundle",
                str(tmp_path / "workspace/bundle"),
                "--study",
                str(tmp_path / "workspace/study"),
                "--output",
                str(tmp_path / "workspace/enriched-report"),
            ]
        )
    )
    assert enriched["selection_diagnostics"]["inventory"]["registered_candidate_count"] == 1
    assert len(enriched["signal_decay"]["horizons"]) == 4
    assert all(
        r["status"] == "compared" for r in enriched["same_pool_baseline"]["scenarios"].values()
    )
    # Exercise the real multi-capital/cost orchestration, without a provider or
    # replacing the quantity ledger. Only source evidence is synthetic here.
    from quantlab.pipeline import refresh as project_refresh
    from quantlab.pipeline import research as project_research
    from quantlab.pipeline.config import load_project
    from quantlab.research.ml import serving
    from quantlab.research.ml.io import sha256

    parsed = load_project(path)
    frame = pd.read_parquet(tmp_path / "context.parquet")
    exposure_dir = tmp_path / "synthetic_exposures"
    exposure_dir.mkdir()
    pd.DataFrame(
        {
            "session": frame.trade_date.dt.strftime("%Y-%m-%d"),
            "instrument_id": frame.instrument_id,
            "available_at": frame.known_at,
            "log_size": 20.0,
        }
    ).to_parquet(exposure_dir / "exposures.parquet")
    frozen_market = sha256(tmp_path / "workspace/market/market.jsonl")
    with monkeypatch.context() as patch:
        patch.setattr(
            project_research,
            "load_strategy",
            lambda p: {
                "strategy_sha256": "a" * 64,
                "capital_scenarios_cny": [50000, 200000],
                "slippage_bps": [5, 20],
            },
        )
        patch.setattr(workflow, "universe_inputs", lambda p: exposure_dir)
        stress = project_research.stress(parsed)
    assert len(stress["scenarios"]) == 2
    assert sha256(tmp_path / "workspace/market/market.jsonl") == frozen_market
    for row in stress["scenarios"]:
        result = json.loads((__import__("pathlib").Path(row["report"]) / "report.json").read_text())
        assert len(result["scenarios"]) == 2
        assert all(
            r["status"] == "compared" for r in result["same_pool_baseline"]["scenarios"].values()
        )
        assert all(x["last_session"] == project["test_end"] for x in result["scenarios"])

    parent = serving.register_model(
        tmp_path / "workspace/training", days[108].strftime("%Y-%m"), "ridge", tmp_path / "registry"
    )
    with monkeypatch.context() as patch:
        patch.setattr(project_refresh, "load_strategy", lambda p: {"strategy_sha256": "a" * 64})
        patch.setattr(
            project_refresh,
            "verify_release",
            lambda *a: {
                "evidence": {
                    str(tmp_path / "workspace/training/completed.json"): sha256(
                        tmp_path / "workspace/training/completed.json"
                    )
                }
            },
        )
        candidate = project_refresh.refresh(parsed, days[123].date(), parent["model_id"])
    assert candidate["status"] == "blocked"  # Constant synthetic factors cannot pass validation IC.
    assert candidate["model_id"] != parent["model_id"]
    assert not (tmp_path / "registry/activations").exists()
    summary = run("status")
    assert all(
        summary["stages"][x] == "verified" for x in ("bundle", "training", "replay", "report")
    )
    run("init-account", "--as-of", str(days[117].date()))
    result = run("daily", "--as-of", str(days[117].date()))
    assert not result["forward_decision"]  # A historical catch-up must not fabricate a live signal.
    run("daily", "--as-of", str(days[118].date()))
    assert service.inspect_service(tmp_path / "account")["sessions"] == 2
