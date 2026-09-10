"""Command-line entry point for QuantLab Daily."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

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
    from quantlab.data import ParquetStorage
    from quantlab.data.enrichment import inspect_enrichment_status

    status = inspect_data_status()
    effective = date.fromisoformat(status["effective_as_of"]) if status["effective_as_of"] else None
    payload = {
        "product": "QuantLab Daily",
        "version": "1.1.0",
        "project_root": str(PROJECT_ROOT),
        "python": sys.version.split()[0],
        "tushare_token_available": bool(os.environ.get("TUSHARE_TOKEN")),
        "data": status,
        "enrichment": inspect_enrichment_status(
            ParquetStorage(PROJECT_ROOT / "data" / "canonical"), effective
        ),
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
        for endpoint, item in payload["enrichment"].items():
            print(f"  {endpoint}: {item['status']}")
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


def _update(
    through: date | None,
    include_context: bool,
    include_enrichment: bool,
    financial_period: date | None,
    dividend_instruments: tuple[str, ...],
) -> int:
    from quantlab.daily.update import run_incremental_update
    from quantlab.data import ParquetStorage, TushareProvider

    provider = TushareProvider()
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    result = run_incremental_update(
        provider,
        storage,
        through or datetime.now(SHANGHAI).date(),
        include_context=include_context,
        include_enrichment=include_enrichment,
        financial_period=financial_period,
        dividend_instruments=dividend_instruments,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def _research(alpha: str | None, config: str | None) -> int:
    if config:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_daily_factor_research.py"),
            "--config",
            config,
        ]
    else:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_alpha_research.py"),
            "--alpha",
            alpha or "momentum_20d",
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


def _shadow() -> int:
    from quantlab.research.forward_shadow import (
        evaluate_matured_forward_shadows,
        generate_forward_shadow,
    )

    results = generate_forward_shadow()
    evaluations = evaluate_matured_forward_shadows()
    for result in results:
        action = "reused" if result.reused else "created"
        print(f"{action} {result.model_id}: {result.prediction_dir}")
    print(f"new matured evaluations: {len(evaluations)}")
    return 0


def _accept(account_id: str) -> int:
    from quantlab.daily.acceptance import run_v1_acceptance

    json_path, html_path, payload = run_v1_acceptance(account_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"acceptance JSON: {json_path}")
    print(f"acceptance HTML: {html_path}")
    return 0 if payload["product_ready"] else 2


def _portfolio(action: str, account_id: str, source: str | None, cash: str | None) -> int:
    from quantlab.personal import (
        build_reference_plan,
        build_tracking_valuation,
        create_demo_account,
        import_account_csv,
        import_manual_cash_flows,
        import_manual_fills,
        load_tracking_summary,
        materialize_valuation_checkpoint,
        preview_manual_cash_flows,
        preview_manual_fills,
    )

    if action == "demo":
        path = create_demo_account(account_id, cash_cny=cash or "200000.00")
        print(f"created demo account snapshot: {path}")
    elif action == "import":
        if source is None:  # pragma: no cover - argparse enforces this
            raise ValueError("--file is required")
        path = import_account_csv(Path(source))
        print(f"imported account snapshot: {path}")
    elif action == "plan":
        json_path, csv_path, payload = build_reference_plan(account_id)
        print("generated reference-only plan")
        print(f"  signal date: {payload['signal_date']}")
        print(f"  intended next session: {payload['intended_next_session']}")
        print(f"  status: {payload['status']}")
        print(f"  plan: {json_path}")
        print(f"  csv: {csv_path}")
    elif action in {"fills-preview", "fills-import"}:
        if source is None:  # pragma: no cover - argparse enforces this
            raise ValueError("--file is required")
        if action == "fills-preview":
            payload = preview_manual_fills(account_id, Path(source))
        else:
            path, payload = import_manual_fills(account_id, Path(source))
            payload["journal_path"] = str(path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif action in {"cash-preview", "cash-import"}:
        if source is None:  # pragma: no cover - argparse enforces this
            raise ValueError("--file is required")
        if action == "cash-preview":
            payload = preview_manual_cash_flows(account_id, Path(source))
        else:
            path, payload = import_manual_cash_flows(account_id, Path(source))
            payload["journal_path"] = str(path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif action == "mark":
        path, payload, reused = materialize_valuation_checkpoint(account_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"valuation checkpoint: {path}")
        print(f"reused: {reused}")
    elif action == "track":
        payload = {
            "account": load_tracking_summary(account_id),
            "valuation": build_tracking_valuation(account_id),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:  # pragma: no cover - argparse enforces this
        raise AssertionError(action)
    return 0


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
    update.add_argument(
        "--enrichment",
        action="store_true",
        help="Fetch stk_limit and explicitly scoped financial/dividend enrichment.",
    )
    update.add_argument(
        "--financial-period",
        type=_date,
        help="Reporting period end to observe with fina_indicator_vip.",
    )
    update.add_argument(
        "--dividend-instrument",
        action="append",
        default=[],
        help="Explicit A-share instrument to observe for dividend context (repeatable).",
    )
    daily = sub.add_parser("daily", help="Generate an offline daily ranking snapshot.")
    daily.add_argument("--as-of", type=_date, help="Requested local date (YYYY-MM-DD).")
    research = sub.add_parser("research", help="Run a registered research experiment.")
    research_choice = research.add_mutually_exclusive_group()
    research_choice.add_argument("--alpha", choices=("momentum_20d",))
    research_choice.add_argument("--config", help="Versioned Daily v1 research config.")
    portfolio = sub.add_parser("portfolio", help="Import an account or build a reference plan.")
    portfolio_sub = portfolio.add_subparsers(dest="portfolio_action", required=True)
    portfolio_demo = portfolio_sub.add_parser("demo", help="Create/reset the 200k demo account.")
    portfolio_demo.add_argument("--account-id", default="demo_200k")
    portfolio_demo.add_argument("--cash", default="200000.00", help="Initial demo cash in CNY.")
    portfolio_import = portfolio_sub.add_parser(
        "import", help="Validate and import an account CSV."
    )
    portfolio_import.add_argument("--file", required=True)
    portfolio_plan = portfolio_sub.add_parser("plan", help="Build a next-session reference plan.")
    portfolio_plan.add_argument("--account-id", required=True)
    for action, help_text in (
        ("fills-preview", "Validate a broker-fill CSV without writing."),
        ("fills-import", "Atomically import a validated broker-fill CSV."),
        ("cash-preview", "Validate an external cash-flow CSV without writing."),
        ("cash-import", "Atomically import validated external cash-flow facts."),
    ):
        import_parser = portfolio_sub.add_parser(action, help=help_text)
        import_parser.add_argument("--account-id", required=True)
        import_parser.add_argument("--file", required=True)
    portfolio_mark = portfolio_sub.add_parser(
        "mark", help="Materialize an immutable raw-close account valuation checkpoint."
    )
    portfolio_mark.add_argument("--account-id", required=True)
    portfolio_track = portfolio_sub.add_parser("track", help="Show replayed account state.")
    portfolio_track.add_argument("--account-id", required=True)
    ui = sub.add_parser("ui", help="Start the local-only Streamlit UI.")
    ui.add_argument("--port", type=int, default=8501)
    accept = sub.add_parser("accept", help="Run local Daily v1 end-to-end acceptance.")
    accept.add_argument("--account-id", default="demo_200k")
    sub.add_parser("shadow", help="Freeze forward-only scores; append matured diagnostics.")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "doctor":
        code = _doctor(args.json)
    elif args.command == "update":
        code = _update(
            args.through,
            not args.no_context,
            args.enrichment,
            args.financial_period,
            tuple(args.dividend_instrument),
        )
    elif args.command == "daily":
        code = _daily(args.as_of)
    elif args.command == "research":
        code = _research(args.alpha, args.config)
    elif args.command == "portfolio":
        code = _portfolio(
            args.portfolio_action,
            getattr(args, "account_id", ""),
            getattr(args, "file", None),
            getattr(args, "cash", None),
        )
    elif args.command == "ui":
        code = _ui(args.port)
    elif args.command == "accept":
        code = _accept(args.account_id)
    elif args.command == "shadow":
        code = _shadow()
    else:  # pragma: no cover - argparse enforces this
        raise AssertionError(args.command)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
