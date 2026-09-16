"""Unified ML research and daily paper accounts; no broker execution authority."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.research.ml.artifacts import verify_completed
from quantlab.research.ml.config import load_config
from quantlab.research.ml.data import calendar_index, monthly_folds, validate_features
from quantlab.research.ml.io import (
    read_corporate_actions,
    read_market_days,
    research_output,
    seal_bundle,
    sha256,
    verify_bundle,
)
from quantlab.research.ml.panel import audit_window, read_range
from quantlab.research.ml.runner import code_identity, run_scenarios, run_training
from quantlab.research.quantity_scheduler import RawCloseMark

ROOT = Path(__file__).resolve().parents[4]


def parser():
    main = argparse.ArgumentParser(prog="quantlab ml", description=__doc__)
    sub = main.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export-history", help="Bridge sealed history and explicit PIT context")
    for name in ("history", "pit-context", "output"):
        export.add_argument(f"--{name}", type=Path, required=True)
    prepare = sub.add_parser(
        "prepare-inputs", help="Copy and seal explicitly supplied research files"
    )
    for name in ("features", "prices", "calendar", "feature-names", "provenance", "output"):
        prepare.add_argument(f"--{name}", type=Path, required=True)
    prepare.add_argument("--lineage", type=Path)
    prepare.add_argument("--feature-contract", type=Path)
    for command in ("check", "train", "replay", "shadow"):
        child = sub.add_parser(command)
        child.add_argument("--bundle", type=Path, required=True)
        child.add_argument("--config", type=Path, default=ROOT / "config/ml_daily_v2.json")
        child.add_argument("--start", type=date.fromisoformat, required=True)
        child.add_argument("--end", type=date.fromisoformat, required=True)
        child.add_argument("--require-lineage", action="store_true")
        if command != "check":
            child.add_argument("--output", type=Path, required=True)
            child.add_argument("--resume", action="store_true")
        if command == "train":
            child.add_argument("--study", type=Path)
            child.add_argument("--final-holdout", action="store_true")
        if command in {"replay", "shadow"}:
            child.add_argument(
                "--run" if command == "replay" else "--signals", type=Path, required=True
            )
            for name in ("market-days", "initial-marks", "corporate-actions"):
                child.add_argument(f"--{name}", type=Path, required=True)
            child.add_argument(
                "--capital-cny", type=int, nargs="+", default=[50000, 200000, 1000000]
            )
    register = sub.add_parser(
        "register", help="Register one completed fold/model as a research artifact"
    )
    register.add_argument("--run", type=Path, required=True)
    register.add_argument("--fold", required=True)
    register.add_argument(
        "--model", choices=["ridge", "lightgbm", "binary", "lambdarank"], required=True
    )
    register.add_argument("--registry", type=Path, required=True)
    activate = sub.add_parser(
        "activate", help="Timestamp a model's shadow activation; no broker authority"
    )
    activate.add_argument("--registry", type=Path, required=True)
    activate.add_argument("--model-id", required=True)
    activate.add_argument("--effective-from", type=date.fromisoformat, required=True)
    predict = sub.add_parser("predict", help="Predict a daily feature snapshot without any labels")
    for name in ("registry", "features", "calendar", "output"):
        predict.add_argument(f"--{name}", type=Path, required=True)
    predict.add_argument("--decision-hour", type=int, default=16)
    predict.add_argument("--as-of", type=date.fromisoformat, required=True)
    report = sub.add_parser("report")
    for name in ("replay", "benchmark", "output"):
        report.add_argument(f"--{name}", type=Path, required=True)
    report.add_argument("--training", type=Path)
    report.add_argument("--exposures", type=Path)
    study = sub.add_parser("init-study")
    study.add_argument("--output", type=Path, required=True)
    for name in ("development-end", "holdout-start", "holdout-end"):
        study.add_argument(f"--{name}", type=date.fromisoformat, required=True)
    doctor = sub.add_parser("status", help="Verify completion receipts, inputs and registry state")
    doctor.add_argument("--path", type=Path, required=True)
    init = sub.add_parser("init-service", help="Initialize an isolated, flat paper account")
    for name in ("output", "config", "calendar", "initial-marks", "feature-contract"):
        init.add_argument(f"--{name}", type=Path, required=True)
    init.add_argument("--as-of", type=date.fromisoformat, required=True)
    init.add_argument("--capital-cny", type=int, default=200000)
    init.add_argument("--max-model-age-days", type=int, default=45)
    daily = sub.add_parser("run-day", help="Settle yesterday's orders and freeze today's decision")
    for name in ("service", "inputs", "registry"):
        daily.add_argument(f"--{name}", type=Path, required=True)
    daily.add_argument("--as-of", type=date.fromisoformat, required=True)
    state = sub.add_parser("service-state", help="Inspect or pause/resume new paper decisions")
    state.add_argument("--service", type=Path, required=True)
    state.add_argument("--set", choices=("paused", "active"))
    service_report = sub.add_parser(
        "service-report", help="Report settled daily account vs benchmark"
    )
    for name in ("service", "benchmark", "output"):
        service_report.add_argument(f"--{name}", type=Path, required=True)
    return main


def execute_replay(args, manifest, config, sessions):
    from quantlab.research.ml.serving import archived_signals

    first = sessions.get_loc(pd.Timestamp(args.start))
    last = sessions.get_loc(pd.Timestamp(args.end))
    if first < 1 or last >= len(sessions) - 1 or first > last:
        raise ValueError("replay needs previous-decision and following-session padding")
    identity = code_identity(ROOT)
    if args.action == "replay":
        verify_completed(args.run)
        intent = json.loads((args.run / "intent.json").read_text())
        if intent["config_fingerprint"] != config.fingerprint or intent["inputs"] != manifest:
            raise ValueError("replay inputs/config differ from completed training")
        scores = pd.read_parquet(args.run / "scores.parquet")
        signal_binding = {"training_completion_sha256": sha256(args.run / "completed.json")}
    else:
        scores, archive = archived_signals(
            args.signals, [d.date() for d in sessions[first - 1 : last]]
        )
        signal_binding = {"forward_archive": archive}
    hashes = {
        key: sha256(getattr(args, key))
        for key in ("market_days", "initial_marks", "corporate_actions")
    }
    market_days = read_market_days(args.market_days)
    corporate = read_corporate_actions(args.corporate_actions, args.start, args.end)
    raw = json.loads(args.initial_marks.read_text())
    marks = tuple(
        RawCloseMark(r["instrument_id"], date.fromisoformat(r["session"]), r["price_fen"])
        for r in raw
    )
    import pyarrow.parquet as pq

    universe_columns = ["trade_date", "instrument_id", "eligible", "industry"]
    universe_columns += [
        k
        for k in ("can_open", "must_exit", "soft_exit")
        if k in pq.read_schema(args.bundle / "features.parquet").names
    ]
    universe = read_range(
        args.bundle / "features.parquet",
        sessions[first - 1],
        args.end,
        columns=universe_columns,
        max_bytes=config.max_matrix_bytes,
    )

    def final_check():
        if (
            any(sha256(getattr(args, k)) != v for k, v in hashes.items())
            or verify_bundle(args.bundle) != manifest
            or code_identity(ROOT) != identity
        ):
            raise ValueError("replay inputs/code changed during execution")
        if args.action == "replay":
            verify_completed(args.run)
        else:
            _, current = archived_signals(
                args.signals, [d.date() for d in sessions[first - 1 : last]]
            )
            if current != archive:
                raise ValueError("forward archive changed during replay")

    return run_scenarios(
        scores,
        universe,
        tuple(d.date() for d in sessions),
        market_days,
        marks,
        start=args.start,
        end=args.end,
        capitals_fen=[c * 100 for c in args.capital_cny],
        config=config,
        output=args.output,
        corporate_actions=corporate,
        resume=args.resume,
        binding_extra={"code": identity, "inputs": hashes, **signal_binding},
        final_check=final_check,
        strategy_mode="archived_forward_signals" if args.action == "shadow" else "backtest",
    )


def dispatch(args):
    if hasattr(args, "output"):
        research_output(args.output)
    if args.action == "service-report":
        from quantlab.research.ml.service_view import build_service_report

        return build_service_report(args.service, args.benchmark, args.output)
    if args.action in {"init-service", "run-day", "service-state"}:
        from quantlab.research.ml import service

        if args.action == "init-service":
            raw = json.loads(args.initial_marks.read_text())
            marks = tuple(
                RawCloseMark(r["instrument_id"], date.fromisoformat(r["session"]), r["price_fen"])
                for r in raw
            )
            return service.initialize(
                args.output,
                load_config(args.config),
                json.loads(args.calendar.read_text()),
                marks,
                args.as_of,
                args.capital_cny * 100,
                sha256(args.feature_contract),
                max_model_age_days=args.max_model_age_days,
            )
        if args.action == "run-day":
            return service.run_day(
                args.service,
                args.inputs,
                args.registry,
                args.as_of,
                code=code_identity(ROOT),
                verify_code=lambda: code_identity(ROOT),
            )
        if args.set:
            service.set_paused(args.service, args.set == "paused")
        return service.inspect_service(args.service)
    if args.action == "export-history":
        from quantlab.research.ml.history import export_history

        return export_history(args.history, args.pit_context, args.output, root=ROOT)
    if args.action == "prepare-inputs":
        if bool(args.lineage) != bool(args.feature_contract):
            raise ValueError("lineage and feature contract must be supplied together")
        args.output.mkdir(parents=True, exist_ok=False)
        files = {
            "features.parquet": args.features,
            "prices.parquet": args.prices,
            "calendar.json": args.calendar,
            "feature_names.json": args.feature_names,
        }
        if args.lineage:
            files.update(
                {
                    "pit_lineage.parquet": args.lineage,
                    "feature_contract.json": args.feature_contract,
                }
            )
        for name, path in files.items():
            before = sha256(path)
            shutil.copyfile(path, args.output / name)
            if sha256(args.output / name) != before or sha256(path) != before:
                raise ValueError("input changed during snapshot preparation")
        return seal_bundle(args.output, provenance=json.loads(args.provenance.read_text()))
    if args.action == "register":
        from quantlab.research.ml.serving import register_model

        return register_model(args.run, args.fold, args.model, args.registry)
    if args.action == "activate":
        from quantlab.research.ml.serving import activate_model

        return activate_model(args.registry, args.model_id, args.effective_from)
    if args.action == "predict":
        from quantlab.research.ml.serving import predict_day

        return predict_day(
            args.registry,
            args.features,
            args.calendar,
            args.as_of,
            args.output,
            code=code_identity(ROOT),
            verify_code=lambda: code_identity(ROOT),
            decision_hour=args.decision_hour,
        )
    if args.action == "report":
        from quantlab.research.ml.reporting import build_report

        return build_report(
            args.replay,
            args.benchmark,
            args.output,
            training=args.training,
            exposures_path=args.exposures,
        )
    if args.action == "init-study":
        from quantlab.research.ml.study import initialize

        return initialize(args.output, args.development_end, args.holdout_start, args.holdout_end)
    if args.action == "status":
        path = args.path
        if (path / "service.json").exists():
            from quantlab.research.ml.service import inspect_service

            return inspect_service(path)
        if (path / "invalidated.json").exists():
            return {"status": "invalidated_use_new_output", "resume_allowed": False}
        if (path / "completed.json").exists():
            receipt = verify_completed(path)
            return {
                "status": "completed_artifacts_verified",
                "files": len(receipt["artifacts"]),
                "performance_eligible": False,
            }
        if (path / "manifest.json").exists():
            return {"status": "input_hashes_verified", "manifest": verify_bundle(path)}
        if (path / "intent.json").exists():
            return {
                "status": "incomplete_use_resume_with_identical_inputs",
                "failures": len(list((path / "failures").glob("*.json"))),
                "checkpoints": len(list(path.glob("*/sessions/*.json"))),
            }
        if (path / "models").exists():
            models = []
            for folder in sorted((path / "models").iterdir()):
                verify_completed(folder)
                models.append(json.loads((folder / "registration.json").read_text())["model_id"])
            return {
                "status": "registry_artifacts_verified",
                "models": models,
                "activations": len(list((path / "activations").glob("*.json"))),
            }
        raise ValueError("unrecognized or uninitialized research artifact")
    manifest = verify_bundle(args.bundle)
    config = load_config(args.config)
    if args.require_lineage and "pit_lineage.parquet" not in manifest["files"]:
        raise ValueError("source lineage required but missing")
    sessions = calendar_index(json.loads((args.bundle / "calendar.json").read_text()))
    if args.action == "train":
        if args.final_holdout and not args.study:
            raise ValueError("final holdout requires a predeclared study")
        if args.study:
            from quantlab.research.ml.study import reserve

            reserve(
                args.study,
                args.start,
                args.end,
                {
                    "config": config.payload(),
                    "manifest": manifest,
                    "code": code_identity(ROOT),
                    "start": str(args.start),
                    "end": str(args.end),
                    "output": str(args.output.resolve()),
                },
                final_holdout=args.final_holdout,
            )
        return run_training(
            args.bundle,
            args.config,
            args.output,
            args.start,
            args.end,
            root=ROOT,
            resume=args.resume,
        )
    if args.action in {"replay", "shadow"}:
        return execute_replay(args, manifest, config, sessions)
    names = json.loads((args.bundle / "feature_names.json").read_text())
    folds = monthly_folds(sessions, args.start, args.end, config)
    rows = feature_rows = unknown_rows = 0
    statuses = set()
    for month in sessions.to_period("M").unique():
        days = sessions[sessions.to_period("M") == month]
        raw = read_range(
            args.bundle / "features.parquet", days[0], days[-1], max_bytes=config.max_matrix_bytes
        )
        if raw.empty:
            continue
        frame = validate_features(raw, names, sessions, config)
        statuses.add(audit_window(args.bundle, frame, names, config)["status"])
        rows += len(frame)
        feature_rows += int(frame.feature_ok.sum())
        unknown_rows += int(frame.eligible.isna().sum())
    if not rows:
        raise ValueError("empty feature history")
    return {
        "status": "input_contract_valid_not_data_certification",
        "rows": rows,
        "feature_eligible_rows": feature_rows,
        "unknown_universe_rows": unknown_rows,
        "lineage_status": sorted(statuses),
        "folds": len(folds),
        "fits": len(folds) * len(config.models),
        "config_fingerprint": config.fingerprint,
    }


def main(argv=None):
    args = parser().parse_args(argv)
    payload = dispatch(args)
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
    if args.action == "run-day" and not payload["forward_decision"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
