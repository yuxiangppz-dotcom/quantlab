"""Command-line entry point for QuantLab Daily."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime

from quantlab.daily.service import (
    PROJECT_ROOT,
    SHANGHAI,
    generate_daily_snapshot,
    inspect_data_status,
)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _doctor(as_json: bool) -> int:
    status = inspect_data_status()
    payload = {
        "product": "QuantLab Daily",
        "version": "v1-development",
        "project_root": str(PROJECT_ROOT),
        "python": sys.version.split()[0],
        "tushare_token_available": bool(os.environ.get("TUSHARE_TOKEN")),
        "data": status,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("QuantLab Daily doctor")
        print(f"  project: {payload['project_root']}")
        print(f"  Python: {payload['python']}")
        print(f"  Tushare token available: {payload['tushare_token_available']}")
        print(f"  data status: {status['status']}")
        print(f"  effective as-of: {status['effective_as_of']}")
        print(f"  calendar through: {status['latest_calendar_date']}")
        for issue in status["issues"]:
            print(f"  WARNING: {issue}")
    return 0 if status["effective_as_of"] else 2


def _daily(as_of: date | None) -> int:
    snapshot = generate_daily_snapshot(as_of)
    report = snapshot.report
    print("reused cached snapshot" if snapshot.reused else "generated daily snapshot")
    print(f"  requested as-of: {report['requested_as_of']}")
    print(f"  effective as-of: {report['effective_as_of']}")
    print(f"  data status: {report['data_status']['status']}")
    print(f"  ranking: {snapshot.ranking_path}")
    print(f"  target: {snapshot.target_path}")
    print(f"  report: {snapshot.report_path}")
    print(f"  html: {snapshot.html_path}")
    if report["data_status"]["status"] != "complete":
        print("  WARNING: cached/data date is not asserted to be today's completed close")
    return 0


def _update(through: date | None, include_context: bool) -> int:
    from quantlab.daily.update import run_incremental_update
    from quantlab.data import ParquetStorage, TushareProvider

    provider = TushareProvider()
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    result = run_incremental_update(
        provider,
        storage,
        through or datetime.now(SHANGHAI).date(),
        include_context=include_context,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def _research(alpha: str) -> int:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_alpha_research.py"),
        "--alpha",
        alpha,
    ]
    try:
        return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode
    except KeyboardInterrupt:
        return 130


def _ui(port: int) -> int:
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "src" / "quantlab" / "ui" / "app.py"),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    try:
        return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantlab", description="Local A-share daily research and decision tool."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Inspect environment and local data freshness.")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    update = sub.add_parser("update", help="Explicitly update missing Canonical daily data.")
    update.add_argument("--through", type=_date, help="Requested end date (YYYY-MM-DD).")
    update.add_argument(
        "--no-context", action="store_true", help="Skip ST/suspension context endpoints."
    )
    daily = sub.add_parser("daily", help="Generate an offline daily ranking snapshot.")
    daily.add_argument("--as-of", type=_date, help="Requested local date (YYYY-MM-DD).")
    research = sub.add_parser("research", help="Run a registered research experiment.")
    research.add_argument("--alpha", default="momentum_20d", choices=("momentum_20d",))
    ui = sub.add_parser("ui", help="Start the local-only Streamlit UI.")
    ui.add_argument("--port", type=int, default=8501)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "doctor":
        code = _doctor(args.json)
    elif args.command == "update":
        code = _update(args.through, not args.no_context)
    elif args.command == "daily":
        code = _daily(args.as_of)
    elif args.command == "research":
        code = _research(args.alpha)
    elif args.command == "ui":
        code = _ui(args.port)
    else:  # pragma: no cover - argparse enforces this
        raise AssertionError(args.command)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
