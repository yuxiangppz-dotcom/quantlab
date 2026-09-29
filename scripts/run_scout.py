#!/usr/bin/env python3
"""Run the independent short-line research assistant; never sends trading orders."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from quantlab.scout.demo import make_demo_market
from quantlab.scout.pipeline import read_config, run_scout
from quantlab.scout.tracking import observe_run

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--doctor", action="store_true", help="Check configuration without network")
    modes.add_argument("--demo", action="store_true", help="Synthetic offline walkthrough")
    modes.add_argument("--offline", action="store_true", help="Read local data; no AI or network")
    modes.add_argument(
        "--live", action="store_true", help="Read providers and call paid OpenAI API"
    )
    modes.add_argument("--track-run", type=Path, help="Original run directory to observe forward")
    parser.add_argument("--config", type=Path, default=ROOT / "config/scout.json")
    parser.add_argument("--canonical-dir", type=Path, default=ROOT / "data/canonical")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/scout/runs")
    parser.add_argument("--session", type=date.fromisoformat)
    parser.add_argument("--clues", type=Path, help="JSON list of user-supplied clues")
    parser.add_argument("--sectors", type=Path, help="Timestamped stock-to-sector mapping JSON")
    args = parser.parse_args()
    config = read_config(args.config)
    if args.doctor:
        print(
            json.dumps(
                {
                    "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
                    "model_configured": bool(os.environ.get("OPENAI_MODEL") or config["model"]),
                    "tushare_token_present": bool(os.environ.get("TUSHARE_TOKEN")),
                    "canonical_dir_exists": args.canonical_dir.is_dir(),
                    "rss_count": len(config["rss"]),
                    "provider_permissions": "not tested; doctor makes no network requests",
                    "next": "--demo needs no credentials; --live needs fresh local market data",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.track_run:
        path = observe_run(args.track_run, args.canonical_dir, args.output_dir)
        print(path)
        return 0
    if args.demo:
        if args.session or args.clues or args.sectors:
            parser.error("--demo cannot mix real session, clues, or sectors")
        with tempfile.TemporaryDirectory(prefix="quantlab-scout-demo-") as directory:
            session = make_demo_market(Path(directory))
            run_dir, report = run_scout(
                Path(directory),
                args.output_dir,
                config,
                session=session,
                demo=True,
            )
    else:
        run_dir, report = run_scout(
            args.canonical_dir,
            args.output_dir,
            config,
            online=args.live,
            session=args.session,
            clues_path=args.clues,
            sectors_path=args.sectors,
        )
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(run_dir / "report.md"),
                "run_id": report["run_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if report["status"] == "incomplete" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        # No secrets or remote response bodies in CLI diagnostics.
        print(
            f"Scout stopped ({type(exc).__name__}): check configuration/input; see docs/scout_zh.md"
        )
        raise SystemExit(2) from None
