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
    read_market_days,
    research_output,
    sha256,
    verify_bundle,
    write_json,
)
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
        if command == "replay":
            child.add_argument("--run", type=Path, required=True)
            child.add_argument("--market-days", type=Path, required=True)
            child.add_argument("--initial-marks", type=Path, required=True)
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
            args.bundle, args.config, args.output, args.start, args.end, root=ROOT
        )
    else:
        manifest = verify_bundle(args.bundle)
        config = load_config(args.config)
        sessions = calendar_index(json.loads((args.bundle / "calendar.json").read_text()))
        if args.action == "check":
            names = json.loads((args.bundle / "feature_names.json").read_text())
            frame = validate_features(
                pd.read_parquet(args.bundle / "features.parquet"), names, sessions, config
            )
            folds = monthly_folds(sessions, args.start, args.end, config)
            payload = {
                "status": "input_contract_valid_not_data_certification",
                "rows": len(frame),
                "feature_eligible_rows": int(frame.feature_ok.sum()),
                "unknown_universe_rows": int(frame.eligible.isna().sum()),
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
            raw = json.loads(args.initial_marks.read_text())
            initial_marks = tuple(
                RawCloseMark(r["instrument_id"], date.fromisoformat(r["session"]), r["price_fen"])
                for r in raw
            )
            identity = code_identity(ROOT)
            scores = pd.read_parquet(args.run / "scores.parquet")
            universe = pd.read_parquet(
                args.bundle / "features.parquet",
                columns=["trade_date", "instrument_id", "eligible", "industry"],
            )
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
            )
            if (
                sha256(args.market_days) != market_hash
                or sha256(args.initial_marks) != initial_hash
            ):
                raise ValueError("replay evidence changed during execution")
            write_json(
                args.output / "binding.json",
                {
                    "code": identity,
                    "training_completion_sha256": sha256(args.run / "completed.json"),
                    "market_days_sha256": market_hash,
                    "initial_marks_sha256": initial_hash,
                    "config_fingerprint": config.fingerprint,
                    "historical_data_certified": False,
                },
            )
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
