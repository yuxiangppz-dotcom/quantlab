#!/usr/bin/env python3
"""Download and store A-share market data from Tushare.

Usage:
    uv run python scripts/update_market_data.py --start 2026-01-01 --end 2026-09-04 \
        --securities --calendar
    uv run python scripts/update_market_data.py --start 2026-08-01 --end 2026-09-04 --daily-all
    uv run python scripts/update_market_data.py --start 2026-08-01 --end 2026-09-04 \
        --daily-all --force
"""

from __future__ import annotations

import argparse
from datetime import date

from quantlab.data import (
    ParquetStorage,
    TushareProvider,
    sync_adj_factor_history,
    sync_daily_history,
)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update local market data from Tushare.")
    parser.add_argument(
        "--start",
        required=True,
        type=_parse_date,
        help="Start date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end",
        required=True,
        type=_parse_date,
        help="End date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--securities",
        action="store_true",
        help="Download the security master list.",
    )
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="Download the trading calendar.",
    )
    parser.add_argument(
        "--daily-all",
        action="store_true",
        help="Download full-market daily bars for every open trading day.",
    )
    parser.add_argument(
        "--adj-factor",
        action="store_true",
        help="Download full-market adjustment factors for every open trading day.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download data even if the local file already exists.",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=[],
        help="Instrument IDs to download daily bars for (e.g. 600519.SH 000001.SZ).",
    )
    args = parser.parse_args()

    if args.start > args.end:
        parser.error(f"--start ({args.start}) must be <= --end ({args.end})")

    if not (args.securities or args.calendar or args.daily_all or args.adj_factor or args.symbols):
        parser.error(
            "at least one of --securities/--calendar/--daily-all/--adj-factor/--symbols "
            "is required"
        )

    provider = TushareProvider()
    storage = ParquetStorage()

    if args.securities:
        securities = provider.get_securities()
        storage.upsert_securities(securities)
        print(f"securities: upserted {len(securities)} rows -> {storage.securities_path}")

    if args.calendar:
        calendar = provider.get_trading_calendar(args.start, args.end)
        storage.upsert_trading_calendar(calendar)
        print(f"calendar: upserted {len(calendar)} rows -> {storage.calendar_path}")

    if args.daily_all:
        result = sync_daily_history(provider, storage, args.start, args.end, force=args.force)
        print(
            f"daily: {result.total} open days, {result.synced} downloaded, "
            f"{result.skipped} skipped, {result.filtered} placeholders filtered"
        )
    elif args.symbols:
        bars = provider.get_daily_bars(args.symbols, args.start, args.end)
        paths = storage.save_daily_bars(bars)
        print(f"daily: saved {len(bars)} rows -> {', '.join(str(p) for p in paths)}")

    if args.adj_factor:
        result = sync_adj_factor_history(
            provider, storage, args.start, args.end, force=args.force
        )
        print(
            f"adj_factor: {result.total} open days, {result.synced} downloaded, "
            f"{result.skipped} skipped"
        )


if __name__ == "__main__":
    main()
