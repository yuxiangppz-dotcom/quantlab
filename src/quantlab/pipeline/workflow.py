"""Project stages delegate to shared data/features/ML/accounting implementations."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from quantlab.data.storage import ParquetStorage
from quantlab.pipeline.features import build_bundle
from quantlab.pipeline.ingestion import calendar_days, synchronize, verify_session
from quantlab.pipeline.market import load_policy, market_day
from quantlab.pipeline.strategy import load_strategy
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml import service
from quantlab.research.ml.artifacts import complete, verify_completed
from quantlab.research.ml.config import load_config
from quantlab.research.ml.io import sha256, verify_bundle, write_json

ROOT = Path(__file__).resolve().parents[3]


def prepare_market(project, bundle, output, start, end):
    """No hand-built market.json/initial marks/benchmark are needed downstream."""
    manifest = verify_bundle(bundle)
    sources = {
        str(project[k]): sha256(project[k]) for k in ("execution_policy", "corporate_actions")
    }
    policy = load_policy(project["execution_policy"])
    storage = ParquetStorage(project["canonical"])
    sessions = [date.fromisoformat(d) for d in json.loads((bundle / "calendar.json").read_text())]
    features = pd.read_parquet(bundle / "features.parquet", columns=["trade_date", "instrument_id"])
    modern = load_strategy(project) is not None
    universe = set(features.instrument_id)
    by_day = (
        {
            pd.Timestamp(day).date(): set(group.instrument_id)
            for day, group in features.groupby("trade_date")
        }
        if modern
        else {}
    )
    days = [d for d in sessions if start <= d <= end]
    config = load_config(project["ml_config"])
    if not days or sessions.index(days[0]) == 0:
        raise ValueError("market interval needs prior initialization session")
    initial = sessions[sessions.index(days[0]) - 1]
    with exclusive_job(output.parent):
        import hashlib

        from quantlab.research.ml.runner import code_identity

        receipts_digest = hashlib.sha256(
            json.dumps(
                sorted(
                    (p.name, sha256(p))
                    for p in sorted(
                        (Path(project["receipts"]) / "sessions").glob("*.json")
                    )
                )
            ).encode()
        ).hexdigest()
        if output.exists():
            verify_completed(output)
            expected = {
                "sources": sources,
                "bundle": manifest,
                "start": str(start),
                "end": str(end),
                "receipts": receipts_digest,
                "code": code_identity(ROOT),
            }
            if json.loads((output / "intent.json").read_text()) != expected:
                raise ValueError("market input changed; choose a new workspace")
            return output
        with TemporaryDirectory(dir=output.parent) as temp:
            stage = Path(temp) / "market"
            stage.mkdir()
            with (stage / "market.jsonl").open("x") as stream:
                for day in days:
                    payload = market_day(
                        storage,
                        project["receipts"],
                        sessions,
                        day,
                        by_day.get(day, set()) if modern else universe,
                        policy,
                        project["corporate_actions"],
                        hour=config.decision_hour,
                        sparse_scope=modern,
                    )
                    stream.write(json.dumps(payload) + "\n")
            first = market_day(
                storage,
                project["receipts"],
                sessions,
                initial,
                by_day.get(initial, set()) if modern else universe,
                policy,
                project["corporate_actions"],
                hour=config.decision_hour,
                sparse_scope=modern,
            )
            write_json(stage / "initial_marks.json", first["marks"])
            benchmarks = []
            for day in days:
                verify_session(storage, project["receipts"], day)
                matches = [
                    r
                    for r in storage.load_index_daily_by_date(day)
                    if r.instrument_id == project["benchmark"]
                ]
                if len(matches) != 1 or matches[0].pre_close <= 0:
                    raise ValueError("benchmark evidence missing")
                row = matches[0]
                benchmarks.append(
                    {"session": str(day), "benchmark_return": row.close / row.pre_close - 1}
                )
            pd.DataFrame(benchmarks).to_parquet(stage / "benchmark.parquet", index=False)
            write_json(
                stage / "benchmark_metadata.json",
                {
                    "instrument_id": project["benchmark"],
                    "return_basis": "price_index"
                    if project["benchmark"] == "000906.SH"
                    else "unverified",
                    "construction": "daily_close_divided_by_previous_close_minus_one",
                    "dividends_reinvested": False if project["benchmark"] == "000906.SH" else None,
                    "comparable_to_net_dividend_portfolio": False,
                },
            )
            shutil.copyfile(project["corporate_actions"], stage / "corporate_actions.json")
            for path, digest in sources.items():
                if sha256(path) != digest:
                    raise ValueError("execution evidence changed during preparation")
            if verify_bundle(bundle) != manifest:
                raise ValueError("feature bundle changed during preparation")
            write_json(
                stage / "intent.json",
                {
                    "sources": sources,
                    "bundle": manifest,
                    "start": str(start),
                    "end": str(end),
                    "receipts": receipts_digest,
                    "code": code_identity(ROOT),
                },
            )
            complete(stage)
            stage.rename(output)
    return output


def build(project, *, start=None, end=None, output=None, forward=False):
    start = start or date.fromisoformat(project["start"])
    end = end or date.fromisoformat(project["end"])
    output = output or project["workspace"] / "bundle"
    config = load_config(project["ml_config"])
    context = project["context"]
    if load_strategy(project):
        context = universe_inputs(project, start, end) / "context.parquet"
    return build_bundle(
        ParquetStorage(project["canonical"]),
        project["receipts"],
        context,
        project["availability"],
        output,
        start,
        end,
        config,
        root=ROOT,
        forward=forward,
    )


def universe_inputs(project, start=None, end=None):
    from quantlab.pipeline.universe import compile_universe

    start = start or date.fromisoformat(project["start"])
    end = end or date.fromisoformat(project["end"])
    return compile_universe(
        ParquetStorage(project["canonical"]),
        project["receipts"],
        project["availability"],
        load_strategy(project),
        project["workspace"] / "universe" / f"{start}_{end}",
        start,
        end,
        hour=load_config(project["ml_config"]).decision_hour,
        code_changes_path=ROOT / "config/security_code_changes.csv",
    )


def research_stage(project, action, *, resume=False):
    from quantlab.research.ml.cli import dispatch, parser

    workspace = project["workspace"]
    strategy = load_strategy(project)
    common = [
        "--bundle",
        str(workspace / "bundle"),
        "--config",
        str(project["ml_config"]),
        "--start",
        project["test_start"],
        "--end",
        project["test_end"],
        "--require-lineage",
    ]
    if action == "train":
        argv = ["train", *common, "--output", str(workspace / "training")]
        if strategy:
            binding = json.loads((workspace / "study/strategy.json").read_text())
            if binding["sha256"] != strategy["strategy_sha256"]:
                raise ValueError(
                    "strategy changed after study registration; start a new declared study"
                )
            argv += ["--study", str(workspace / "study")]
            if strategy["study"]["phase"] == "final_holdout":
                argv += ["--final-holdout"]
    elif action in {"replay", "baseline"}:
        if action == "replay" and strategy:
            research_stage(project, "baseline", resume=resume)
        sessions = json.loads((workspace / "bundle/calendar.json").read_text())
        first = next(d for d in sessions if d >= project["test_start"])
        execution_start = sessions[sessions.index(first) + 1]
        common[common.index("--start") + 1] = execution_start
        market = prepare_market(
            project,
            workspace / "bundle",
            workspace / "market",
            date.fromisoformat(execution_start),
            date.fromisoformat(project["test_end"]),
        )
        argv = [
            action,
            *common,
            *(["--run", str(workspace / "training")] if action == "replay" else []),
            "--market-days",
            str(market / "market.jsonl"),
            "--initial-marks",
            str(market / "initial_marks.json"),
            "--corporate-actions",
            str(market / "corporate_actions.json"),
            "--capital-cny",
            *[
                str(n)
                for n in (
                    strategy["capital_scenarios_cny"] if strategy else [project["capital_cny"]]
                )
            ],
            "--output",
            str(workspace / action),
        ]
    else:
        argv = [
            "report",
            "--replay",
            str(workspace / "replay"),
            "--benchmark",
            str(workspace / "market/benchmark.parquet"),
            "--training",
            str(workspace / "training"),
            "--output",
            str(workspace / "report"),
        ]
        if strategy:
            argv += [
                "--exposures",
                str(universe_inputs(project) / "exposures.parquet"),
                "--baseline-replay",
                str(workspace / "baseline"),
                "--bundle",
                str(workspace / "bundle"),
                "--study",
                str(workspace / "study"),
            ]
            factor = workspace / "factor-baseline-momentum20d"
            if (factor / "completed.json").exists():
                argv += ["--factor-replay", str(factor)]
    if resume and action in {"train", "replay", "baseline"}:
        argv.append("--resume")
    return dispatch(parser().parse_args(argv))


def daily_inputs(project, asof):
    """Build today's features from the same source and contract as historical training."""
    workspace = project["workspace"]
    output = workspace / "daily_inputs" / str(asof)
    storage = ParquetStorage(project["canonical"])
    config = load_config(project["ml_config"])
    policy_hash, corporate_hash = (
        sha256(project[k]) for k in ("execution_policy", "corporate_actions")
    )
    with exclusive_job(workspace / "daily_inputs"):
        if output.exists():
            # Inputs are immutable; service validates their full manifest on each retry.
            service.read_inputs(output, asof, service.load_service(project["account"]))
            return output
        with TemporaryDirectory(dir=workspace / "daily_inputs") as temp:
            temp = Path(temp)
            bundle = temp / "bundle"
            build(project, start=asof, end=asof, output=bundle, forward=True)
            sessions = [
                date.fromisoformat(d) for d in json.loads((bundle / "calendar.json").read_text())
            ]
            features = pd.read_parquet(bundle / "features.parquet")
            universe = set(
                features.loc[features.can_open, "instrument_id"]
                if "can_open" in features
                else features.instrument_id
            )
            # Held names need marks and execution contexts even after universe removal.
            metadata = service.load_service(project["account"])
            state, _, previous = service.account_head(project["account"], metadata)
            universe.update(lot.instrument_id for lot in state["book"].lots)
            # Yesterday's pending buys still need today's execution evidence even
            # when the name has already left today's feature universe.
            if previous:
                previous, _ = service.resolved_decision(
                    project["account"], state["book"].asof_date, previous
                )
                if previous["plan"]:
                    universe.update(o.instrument_id for o in previous["plan"]["orders"])
            payload = market_day(
                storage,
                project["receipts"],
                sessions,
                asof,
                universe,
                load_policy(project["execution_policy"]),
                project["corporate_actions"],
                hour=config.decision_hour,
            )
            stage = temp / "inputs"
            stage.mkdir()
            for name in ("features.parquet", "calendar.json"):
                shutil.copyfile(bundle / name, stage / name)
            write_json(stage / "market.json", payload)
            shutil.copyfile(project["corporate_actions"], stage / "corporate_actions.json")
            if policy_hash != sha256(project["execution_policy"]) or corporate_hash != sha256(
                stage / "corporate_actions.json"
            ):
                raise ValueError("daily execution inputs changed while building")
            write_json(
                stage / "manifest.json",
                {
                    "asof": str(asof),
                    "source_id": "canonical_shared_pipeline_v1",
                    "available_at": datetime.now(UTC).isoformat(),
                    "feature_contract_sha256": sha256(bundle / "feature_contract.json"),
                    "files": {name: sha256(stage / name) for name in service.INPUT_FILES},
                    "provenance": verify_bundle(bundle),
                    "execution_policy_sha256": policy_hash,
                },
            )
            service.read_inputs(stage, asof, metadata)
            stage.rename(output)
    return output


def ingest(project, start, end, *, adopt_existing=False):
    from quantlab.data.tushare_provider import TushareProvider

    provider = TushareProvider(archive=project["raw"], interval=project["provider_interval"])
    return synchronize(
        provider,
        ParquetStorage(project["canonical"]),
        project["receipts"],
        start,
        end,
        indices=tuple(project["indices"]),
        adopt_existing=adopt_existing,
        code_changes_path=ROOT / "config/security_code_changes.csv",
    )


def status(project):
    result = {"execution_authority": False, "stages": {}, "missing_evidence": []}
    strategy = load_strategy(project)
    if strategy:
        result["strategy"] = {
            "index": strategy["index"],
            "policy_sha256": strategy["policy_sha256"],
            "phase": strategy["study"]["phase"],
        }
        for key in ("membership", "industries", "event_coverage"):
            if not strategy[key].is_file():
                result["missing_evidence"].append(key)
    for key in (() if strategy else ("context",)) + (
        "availability",
        "execution_policy",
        "corporate_actions",
    ):
        if not project[key].is_file():
            result["missing_evidence"].append(key)
    for stage in ("bundle", "training", "market", "replay", "report", "account"):
        folder = project["account"] if stage == "account" else project["workspace"] / stage
        try:
            if stage == "account" and folder.exists():
                result["stages"][stage] = service.inspect_service(folder)
            elif folder.exists():
                verify_bundle(folder) if stage == "bundle" else verify_completed(folder)
                result["stages"][stage] = "verified"
            else:
                result["stages"][stage] = "not_created"
        except (ValueError, FileNotFoundError, KeyError) as exc:
            result["stages"][stage] = {"status": "blocked", "reason": str(exc)}
    operations = [
        json.loads(p.read_text()) for p in (project["workspace"] / "operations").glob("*.json")
    ]
    result["latest_operation"] = (
        max(operations, key=lambda r: r["recorded_at"]) if operations else None
    )
    storage = ParquetStorage(project["canonical"])
    result["data"] = {"verified_sessions": 0, "failed": []}
    for path in sorted((project["receipts"] / "sessions").glob("*.json")):
        try:
            verify_session(storage, project["receipts"], date.fromisoformat(path.stem))
            result["data"]["verified_sessions"] += 1
        except (ValueError, FileNotFoundError, KeyError) as exc:
            result["data"]["failed"].append({"session": path.stem, "reason": str(exc)})
    if storage.calendar_path.exists():
        calendar = storage.load_trading_calendar()
        days = calendar_days(
            calendar, date.fromisoformat(project["start"]), date.fromisoformat(project["end"])
        )
        result["data"]["missing_sessions"] = [
            str(d) for d in days if not (project["receipts"] / "sessions" / f"{d}.json").exists()
        ]
    return result
