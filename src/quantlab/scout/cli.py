#!/usr/bin/env python3
"""Run the independent short-line research assistant; never sends trading orders."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from quantlab.data.storage import ParquetStorage
from quantlab.scout.demo import make_demo_market, make_demo_sources
from quantlab.scout.hot import collect_hot_rank, save_hot_snapshot
from quantlab.scout.market import inspect_market_data
from quantlab.scout.models import SHANGHAI
from quantlab.scout.pipeline import read_config, run_scout
from quantlab.scout.ranking_tracking import observe_ranking, summarize_ranking
from quantlab.scout.tracking import observe_run, summarize_tracking


def _percent(value: float | None) -> str:
    return f"{value:.2%}" if value is not None else "待观察"


def _main() -> int:
    root = Path.cwd()
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--doctor", action="store_true", help="Check configuration without network")
    modes.add_argument("--demo", action="store_true", help="Synthetic offline walkthrough")
    modes.add_argument("--offline", action="store_true", help="Read local data; no AI or network")
    modes.add_argument(
        "--live", action="store_true", help="Read sources and call the configured paid AI API"
    )
    modes.add_argument("--track-run", type=Path, help="Original run directory to observe forward")
    modes.add_argument("--tracking-summary", action="store_true", help="Aggregate frozen reports")
    modes.add_argument(
        "--track-ranking",
        type=Path,
        help="Observe all frozen deep candidates without modifying TopN tracking",
    )
    modes.add_argument(
        "--ranking-summary", action="store_true", help="Date-balanced H5 ranking diagnostics"
    )
    modes.add_argument(
        "--hot-snapshot", action="store_true", help="Save one public hot-rank snapshot"
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--canonical-dir", type=Path, default=root / "data/canonical")
    parser.add_argument("--output-dir", type=Path, default=root / "data/scout/runs")
    parser.add_argument("--session", type=date.fromisoformat)
    parser.add_argument("--clues", type=Path, help="JSON list of user-supplied clues")
    parser.add_argument("--sectors", type=Path, help="Timestamped stock-to-sector mapping JSON")
    parser.add_argument(
        "--disclosures", type=Path, help="Timestamped TuShare disclosure JSON export"
    )
    parser.add_argument("--comments", type=Path, help="Timestamped user comment sample JSON")
    parser.add_argument("--hot-file", type=Path, help="Use a saved recent AKShare hot snapshot")
    parser.add_argument("--portfolio-file", type=Path, help="Timestamped holdings/watchlist JSON")
    args = parser.parse_args()
    config = read_config(args.config)
    if args.track_ranking:
        print(
            observe_ranking(
                args.track_ranking, args.canonical_dir, args.output_dir.parent / "ranking_tracking"
            )
        )
        return 0
    if args.ranking_summary:
        print(
            summarize_ranking(
                args.output_dir,
                args.output_dir.parent / "ranking_tracking",
                args.output_dir.parent / "ranking-summary.json",
            )
        )
        return 0
    if args.doctor:
        ai_provider = config["provider"]
        model_name = (
            (config["model"] or "deepseek-flash")
            if ai_provider == "deepseek"
            else (
                (config["model"] or "glm-5.3")
                if ai_provider == "zai"
                else (os.environ.get("OPENAI_MODEL") or config["model"])
            )
        )
        market_data = inspect_market_data(
            ParquetStorage(args.canonical_dir), datetime.now(SHANGHAI)
        )
        print(
            json.dumps(
                {
                    "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
                    "zai_key_present": bool(os.environ.get("ZAI_API_KEY")),
                    "deepseek_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
                    "ai_provider": ai_provider,
                    "model_configured": bool(model_name),
                    "tushare_token_present": bool(os.environ.get("TUSHARE_TOKEN")),
                    "canonical_dir_exists": args.canonical_dir.is_dir(),
                    "market_data": market_data,
                    "rss_count": len(config["rss"]),
                    "disclosure_sessions": config["disclosure_sessions"],
                    "disclosure_queries_max": config["disclosure_sessions"] * 3
                    if config["tushare_disclosures"]
                    else 0,
                    "announcement_queries_max": min(config["candidate_limit"], 8)
                    if config["tushare_announcements"]
                    else 0,
                    "cninfo_queries_max": min(config["candidate_limit"], 8) + 1
                    if config["cninfo_announcements"]
                    else 0,
                    "cninfo_pdf_downloads_max": min(config["candidate_limit"], 8)
                    if config["cninfo_announcements"] and config["cninfo_pdf_bodies"]
                    else 0,
                    "kpl_queries_max": 1 if config["tushare_kpl_limit"] else 0,
                    "kpl_target_stocks_max": min(config["candidate_limit"], 8)
                    if config["tushare_kpl_limit"]
                    else 0,
                    "comments": "user JSON import only; no connected platform feed",
                    "provider_permissions": "not tested; doctor makes no network requests",
                    "next": (
                        "--live needs today's completed market partitions"
                        if not market_data["live_partition_files_present"]
                        else "Local partition files are present; --live also needs credentials"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.track_run:
        path = observe_run(args.track_run, args.canonical_dir, args.output_dir.parent / "tracking")
        print(path)
        return 0
    if args.tracking_summary:
        path = summarize_tracking(
            args.output_dir,
            args.output_dir.parent / "tracking",
            args.output_dir.parent / "tracking-summary.json",
        )
        summary = json.loads(path.read_text(encoding="utf-8"))
        lines = [
            "# Scout 冻结报告前瞻观察",
            "",
            f"覆盖目标交易日：{summary['covered_report_dates']}；"
            f"无重点关注日期：{summary['no_focus_dates']}。",
            "",
            "以下是目标日开盘至固定终点收盘的复权价格观察，不是成交收益。",
            "",
            "| 分组 | 期限(交易日) | 原候选 | 可计算 | 上涨比例 | 平均 | 中位 | 缺失/待成熟 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for group, label in (("focus", "重点"), ("watch", "观察")):
            for horizon in (1, 3, 5, 10):
                row = summary["summary"][group][str(horizon)]
                missing = (
                    "、".join(
                        f"{reason}:{count}" for reason, count in row["missing_reasons"].items()
                    )
                    or "无"
                )
                lines.append(
                    f"| {label} | {horizon} | {row['original_candidates']} | "
                    f"{row['calculable']} | {_percent(row['positive_fraction'])} | "
                    f"{_percent(row['mean'])} | {_percent(row['median'])} | {missing} |"
                )
        lines.extend(["", "仅计开盘前最后一份有效冻结报告；旧口径报告另存，不混算。", ""])
        markdown = path.with_suffix(".md")
        markdown.write_text("\n".join(lines), encoding="utf-8")
        print(json.dumps({"json": str(path), "markdown": str(markdown)}, ensure_ascii=False))
        return 0
    if args.hot_snapshot:
        directory = args.output_dir.parent / "hot_snapshots"
        previous = None
        if directory.is_dir():
            for prior in sorted(directory.glob("*.json"), reverse=True):
                try:
                    previous = json.loads(prior.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    continue
        now = datetime.now(SHANGHAI)
        snapshot, coverage = collect_hot_rank(now, True, previous)
        if snapshot is None:
            print(json.dumps({"source": coverage.source, "status": coverage.status}))
            return 2
        path = directory / f"{now:%Y%m%dT%H%M%S%f}.json"
        save_hot_snapshot(path, snapshot)
        print(json.dumps({"path": str(path), "count": coverage.count}, ensure_ascii=False))
        return 0
    if args.demo:
        if (
            args.session
            or args.clues
            or args.sectors
            or args.disclosures
            or args.comments
            or args.hot_file
            or args.portfolio_file
        ):
            parser.error("--demo cannot mix real session or imported sources")
        with tempfile.TemporaryDirectory(prefix="quantlab-scout-demo-") as directory:
            session = make_demo_market(Path(directory))
            disclosures_path, comments_path = make_demo_sources(Path(directory), session)
            run_dir, report = run_scout(
                Path(directory),
                args.output_dir,
                config,
                session=session,
                demo=True,
                disclosures_path=disclosures_path,
                comments_path=comments_path,
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
            disclosures_path=args.disclosures,
            comments_path=args.comments,
            hot_file=args.hot_file,
            portfolio_file=args.portfolio_file,
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


def main() -> int:
    """Console entry point with sanitized configuration errors."""
    try:
        return _main()
    except (ValueError, OSError, KeyError) as exc:
        # No secrets or remote response bodies in CLI diagnostics.
        print(
            f"Scout stopped ({type(exc).__name__}): check configuration/input; see docs/scout_zh.md"
        )
        return 2
