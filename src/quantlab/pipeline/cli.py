"""One user-facing project entry point; ML subcommands remain advanced tools."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from quantlab.pipeline import workflow
from quantlab.pipeline.config import load_project


def parser():
    p = argparse.ArgumentParser(prog="quantlab pipeline")
    p.add_argument("--project", type=Path, required=True)
    sub = p.add_subparsers(dest="action", required=True)
    for action in (
        "plan",
        "status",
        "health",
        "universe",
        "init-study",
        "build",
        "train",
        "replay",
        "baseline",
        "report",
        "stress",
    ):
        child = sub.add_parser(action)
        if action in {"train", "replay", "baseline", "stress"}:
            child.add_argument("--resume", action="store_true")
    sync = sub.add_parser("sync")
    sync.add_argument("--start", type=date.fromisoformat)
    sync.add_argument("--end", type=date.fromisoformat)
    sync.add_argument(
        "--execute", action="store_true", help="Authorize provider requests and Canonical writes"
    )
    sync.add_argument("--adopt-existing", action="store_true")
    observations = sub.add_parser("sync-index-observations")
    observations.add_argument("--execute", action="store_true")
    for key in ("start", "end"):
        observations.add_argument(f"--{key}", type=date.fromisoformat, required=True)
    for action in ("init-account", "daily"):
        sub.add_parser(action).add_argument("--as-of", type=date.fromisoformat, required=True)
    daily = sub.choices["daily"]
    daily.add_argument("--sync", action="store_true")
    daily.add_argument("--execute", action="store_true")
    register = sub.add_parser("register")
    register.add_argument("--fold", required=True)
    register.add_argument(
        "--model", required=True, choices=["ridge", "lightgbm", "binary", "lambdarank"]
    )
    activate = sub.add_parser("activate")
    activate.add_argument("--model-id", required=True)
    activate.add_argument("--effective-from", required=True, type=date.fromisoformat)
    sub.add_parser("release").add_argument("--model-id", required=True)
    sub.add_parser("backup").add_argument("--destination", type=Path, required=True)
    sub.add_parser("verify-backup").add_argument("--path", type=Path, required=True)
    sub.add_parser("monitor").add_argument("--as-of", type=date.fromisoformat, required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--as-of", type=date.fromisoformat, required=True)
    refresh.add_argument("--parent-model-id", required=True)
    refresh.add_argument("--resume", action="store_true")
    revision = sub.add_parser("revision-plan")
    for key in ("start", "end"):
        revision.add_argument(f"--{key}", type=date.fromisoformat, required=True)
    pause = sub.add_parser("account-state")
    pause.add_argument("--set", choices=["paused", "running"])
    return p


def dispatch(args):
    project = load_project(args.project)
    workspace = project["workspace"]
    if args.action == "refresh":
        from quantlab.pipeline.refresh import refresh

        return refresh(project, args.as_of, args.parent_model_id, resume=args.resume)
    if args.action == "monitor":
        from quantlab.pipeline.monitoring import monitor

        return monitor(project, args.as_of)
    if args.action == "sync-index-observations":
        if not args.execute:
            raise ValueError("index observations require --execute for provider requests")
        from quantlab.data.tushare_provider import TushareProvider
        from quantlab.pipeline.observations import index_observations

        provider = TushareProvider(archive=project["raw"], interval=project["provider_interval"])
        return index_observations(
            provider._pro, project["raw"] / "csi800_weights", args.start, args.end
        )
    if args.action in {"health", "backup", "verify-backup", "revision-plan"}:
        from quantlab.pipeline import operations

        if args.action == "health":
            return operations.health(project)
        if args.action == "backup":
            return operations.backup(args.project, args.destination)
        if args.action == "verify-backup":
            return operations.verify_backup(args.path)
        return operations.revision_plan(project, args.start, args.end)
    if args.action in {"plan", "status"}:
        result = workflow.status(project)
        result["sequence"] = [
            "sync",
            "universe",
            "init-study",
            "build",
            "train",
            "replay",
            "report",
            "stress",
            "register",
            "release",
            "activate",
            "init-account",
            "sync + daily",
        ]
        result["decision_hour_shanghai"] = workflow.load_config(project["ml_config"]).decision_hour
        return result
    if args.action == "sync":
        if not args.execute:
            raise ValueError(
                "sync requires --execute: this stage calls TuShare and writes Canonical"
            )
        return workflow.ingest(
            project,
            args.start or date.fromisoformat(project["start"]) - timedelta(days=365),
            args.end or date.fromisoformat(project["end"]),
            adopt_existing=args.adopt_existing,
        )
    if args.action == "build":
        return workflow.build(project)
    if args.action == "universe":
        return {"status": "complete", "path": str(workflow.universe_inputs(project))}
    if args.action == "init-study":
        from quantlab.pipeline.strategy import load_strategy
        from quantlab.research.ml.study import initialize

        strategy = load_strategy(project)
        if not strategy:
            raise ValueError("project v2 strategy required")
        from quantlab.research.alpha158_store import exclusive_job
        from quantlab.research.ml.io import write_json

        with exclusive_job(workspace / "study-initialization"):
            keys = ("development_end", "holdout_start", "holdout_end")
            path = workspace / "study/study.json"
            if path.exists():
                result = json.loads(path.read_text())
                if any(result[k] != strategy["study"][k] for k in keys):
                    raise ValueError("existing study dates differ from strategy")
            else:
                result = initialize(
                    workspace / "study", *[date.fromisoformat(strategy["study"][k]) for k in keys]
                )
            binding = workspace / "study/strategy.json"
            expected = {"sha256": strategy["strategy_sha256"]}
            if binding.exists():
                if json.loads(binding.read_text()) != expected:
                    raise ValueError("existing study strategy differs")
            else:
                write_json(binding, expected)
        return result
    if args.action == "stress":
        from quantlab.pipeline.research import stress

        return stress(project, resume=args.resume)
    if args.action in {"train", "replay", "baseline", "report"}:
        return workflow.research_stage(project, args.action, resume=getattr(args, "resume", False))
    if args.action in {"register", "activate"}:
        from quantlab.research.ml import serving

        if args.action == "register":
            return serving.register_model(
                workspace / "training", args.fold, args.model, project["registry"]
            )
        strategy = workflow.load_strategy(project)
        if strategy:
            from quantlab.pipeline.research import verify_release

            verify_release(project["registry"], args.model_id, strategy["strategy_sha256"])
        return serving.activate_model(project["registry"], args.model_id, args.effective_from)
    if args.action == "release":
        from quantlab.pipeline.research import release_model

        return release_model(project, args.model_id)
    from quantlab.research.ml import service
    from quantlab.research.ml.runner import code_identity

    if args.action == "account-state":
        if args.set:
            service.set_paused(project["account"], args.set == "paused")
        return service.inspect_service(project["account"])
    if args.action == "init-account":
        from quantlab.research.ml.io import sha256, verify_bundle
        from quantlab.research.quantity_scheduler import RawCloseMark

        bundle = workspace / "bundle"
        verify_bundle(bundle)
        storage = workflow.ParquetStorage(project["canonical"])
        workflow.verify_session(storage, project["receipts"], args.as_of)
        marks = tuple(
            RawCloseMark(b.instrument_id, args.as_of, workflow_exact(b.close))
            for b in storage.load_daily_bars_by_date(args.as_of)
        )
        return service.initialize(
            project["account"],
            workflow.load_config(project["ml_config"]),
            json.loads((bundle / "calendar.json").read_text()),
            marks,
            args.as_of,
            project["capital_cny"] * 100,
            sha256(bundle / "feature_contract.json"),
            release_strategy_sha256=(workflow.load_strategy(project) or {}).get("strategy_sha256"),
        )
    if args.sync:
        if not args.execute:
            raise ValueError("daily --sync requires --execute for provider/Canonical access")
        workflow.ingest(project, args.as_of, args.as_of)
    inputs = workflow.daily_inputs(project, args.as_of)
    return service.run_day(
        project["account"],
        inputs,
        project["registry"],
        args.as_of,
        code=code_identity(workflow.ROOT),
        verify_code=lambda: code_identity(workflow.ROOT),
    )


def workflow_exact(value):
    from quantlab.pipeline.market import exact_integer

    return exact_integer(value, 100)


def _operation(args, result, outcome):
    if args.action in {"plan", "status", "health", "verify-backup", "revision-plan"}:
        return
    from quantlab.research.ml.io import sha256, write_json

    try:
        project = load_project(args.project)
        write_json(
            project["workspace"] / "operations" / f"{uuid4().hex}.json",
            {
                "recorded_at": datetime.now(UTC).isoformat(),
                "action": args.action,
                "asof": str(getattr(args, "as_of", "")),
                "status": outcome,
                "project_sha256": sha256(args.project),
                "reason": result.get("reason", result.get("status"))
                if isinstance(result, dict)
                else None,
            },
        )
    except (ValueError, OSError):
        # Console/nonzero exit remains the authority if configuration or disk is unavailable.
        pass


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (ValueError, RuntimeError, OSError, MemoryError) as exc:
        result = {
            "status": "blocked",
            "stage": args.action,
            "error": type(exc).__name__,
            "reason": str(exc),
        }
        _operation(args, result, "blocked")
        print(json.dumps(result, ensure_ascii=False))
        return 2
    blocked = isinstance(result, dict) and (
        str(result.get("status", "")).startswith(("blocked", "decision_blocked", "alert"))
        or (args.action == "daily" and result.get("forward_decision") is not True)
    )
    _operation(args, result, "blocked" if blocked else "completed")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 2 if blocked else 0
