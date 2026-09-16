"""Offline CLI. No command downloads data, writes Canonical, or submits orders."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.research.ml.config import load_config
from quantlab.research.ml.data import calendar_index, monthly_folds, validate_features
from quantlab.research.ml.io import (
    read_corporate_actions,
    read_market_days,
    research_output,
    sha256,
    verify_bundle,
)
from quantlab.research.ml.panel import read_range
from quantlab.research.ml.runner import code_identity, run_scenarios, run_training
from quantlab.research.quantity_scheduler import RawCloseMark

ROOT = Path(__file__).resolve().parents[4]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quantlab ml", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser(
        "export-history", help="Bridge sealed history plus explicit PIT context."
    )
    export.add_argument("--history", type=Path, required=True)
    export.add_argument("--pit-context", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    for command in ("check", "train", "replay"):
        child = sub.add_parser(command)
        child.add_argument("--bundle", type=Path, required=True)
        child.add_argument("--config", type=Path, default=ROOT / "config/ml_daily_v2.json")
        child.add_argument("--start", type=date.fromisoformat, required=True)
        child.add_argument("--end", type=date.fromisoformat, required=True)
        if command != "check":
            child.add_argument("--output", type=Path, required=True)
            child.add_argument("--resume", action="store_true")
        if command == "replay":
            child.add_argument("--run", type=Path, required=True)
            child.add_argument("--market-days", type=Path, required=True)
            child.add_argument("--initial-marks", type=Path, required=True)
            child.add_argument("--corporate-actions", type=Path, required=True)
            child.add_argument(
                "--capital-cny", type=int, nargs="+", default=[50000, 200000, 1000000]
            )
    args = parser.parse_args(argv)
    if hasattr(args, "output"):
        research_output(args.output)
    if args.action == "export-history":
        from quantlab.research.ml.history import export_history

        payload = export_history(args.history, args.pit_context, args.output, root=ROOT)
    elif args.action == "train":
        payload = run_training(
            args.bundle,
            args.config,
            args.output,
            args.start,
            args.end,
            root=ROOT,
            resume=args.resume,
        )
    else:
        manifest = verify_bundle(args.bundle)
        config = load_config(args.config)
        sessions = calendar_index(json.loads((args.bundle / "calendar.json").read_text()))
        if args.action == "check":
            names = json.loads((args.bundle / "feature_names.json").read_text())
            folds = monthly_folds(sessions, args.start, args.end, config)
            rows = feature_rows = unknown_rows = 0
            for month in sessions.to_period("M").unique():
                days = sessions[sessions.to_period("M") == month]
                raw = read_range(
                    args.bundle / "features.parquet",
                    days[0],
                    days[-1],
                    max_bytes=config.max_matrix_bytes,
                )
                if raw.empty:
                    continue
                frame = validate_features(raw, names, sessions, config)
                rows += len(frame)
                feature_rows += int(frame.feature_ok.sum())
                unknown_rows += int(frame.eligible.isna().sum())
            payload = {
                "status": "input_contract_valid_not_data_certification",
                "rows": rows,
                "feature_eligible_rows": feature_rows,
                "unknown_universe_rows": unknown_rows,
                "folds": len(folds),
                "fits": len(folds) * len(config.models),
                "config_fingerprint": config.fingerprint,
            }
        else:
            completed = json.loads((args.run / "completed.json").read_text())
            for name, digest in completed["artifacts"].items():
                path = (args.run / name).resolve()
                if not path.is_relative_to(args.run.resolve()) or sha256(path) != digest:
                    raise ValueError("training artifact path or fingerprint mismatch")
            intent = json.loads((args.run / "intent.json").read_text())
            if intent["config_fingerprint"] != config.fingerprint or intent["inputs"] != manifest:
                raise ValueError("replay inputs/config differ from the completed training run")
            market_hash, initial_hash = sha256(args.market_days), sha256(args.initial_marks)
            market_days = read_market_days(args.market_days)
            corporate_hash = sha256(args.corporate_actions)
            corporate_actions = read_corporate_actions(args.corporate_actions, args.start, args.end)
            raw = json.loads(args.initial_marks.read_text())
            initial_marks = tuple(
                RawCloseMark(r["instrument_id"], date.fromisoformat(r["session"]), r["price_fen"])
                for r in raw
            )
            identity = code_identity(ROOT)
            scores = pd.read_parquet(args.run / "scores.parquet")
            universe = read_range(
                args.bundle / "features.parquet",
                sessions[sessions.get_loc(pd.Timestamp(args.start)) - 1],
                args.end,
                columns=["trade_date", "instrument_id", "eligible", "industry"],
            )
            binding = {
                "code": identity,
                "training_completion_sha256": sha256(args.run / "completed.json"),
                "market_days_sha256": market_hash,
                "initial_marks_sha256": initial_hash,
                "corporate_actions_sha256": corporate_hash,
                "config_fingerprint": config.fingerprint,
                "historical_data_certified": False,
            }

            def final_check():
                if (
                    sha256(args.market_days) != market_hash
                    or sha256(args.initial_marks) != initial_hash
                    or sha256(args.corporate_actions) != corporate_hash
                    or verify_bundle(args.bundle) != manifest
                    or code_identity(ROOT) != identity
                ):
                    raise ValueError("replay inputs/code changed during execution")

            payload = run_scenarios(
                scores,
                universe,
                tuple(d.date() for d in sessions),
                market_days,
                initial_marks,
                start=args.start,
                end=args.end,
                capitals_fen=[c * 100 for c in args.capital_cny],
                config=config,
                output=args.output,
                corporate_actions=corporate_actions,
                resume=args.resume,
                binding_extra=binding,
                final_check=final_check,
            )
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
