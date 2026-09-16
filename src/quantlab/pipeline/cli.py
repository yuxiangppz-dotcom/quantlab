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
    for action in ("plan", "status", "build", "train", "replay", "report"):
        child = sub.add_parser(action)
        if action in {"train", "replay"}:
            child.add_argument("--resume", action="store_true")
    sync = sub.add_parser("sync")
    sync.add_argument("--start", type=date.fromisoformat)
    sync.add_argument("--end", type=date.fromisoformat)
    sync.add_argument(
        "--execute", action="store_true", help="Authorize provider requests and Canonical writes"
    )
    sync.add_argument("--adopt-existing", action="store_true")
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
    pause = sub.add_parser("account-state")
    pause.add_argument("--set", choices=["paused", "running"])
    return p


def dispatch(args):
    project = load_project(args.project)
    workspace = project["workspace"]
    if args.action in {"plan", "status"}:
        result = workflow.status(project)
        result["sequence"] = [
            "sync",
            "build",
            "train",
            "replay",
            "report",
            "register",
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
    if args.action in {"train", "replay", "report"}:
        return workflow.research_stage(project, args.action, resume=getattr(args, "resume", False))
    if args.action in {"register", "activate"}:
        from quantlab.research.ml import serving

        if args.action == "register":
            return serving.register_model(
                workspace / "training", args.fold, args.model, project["registry"]
            )
        return serving.activate_model(project["registry"], args.model_id, args.effective_from)
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
    if args.action in {"plan", "status"}:
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
                "reason": result.get("reason") if isinstance(result, dict) else None,
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
    _operation(args, result, "completed")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0
