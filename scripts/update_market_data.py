#!/usr/bin/env python3
"""Download and store A-share market data from Tushare.

Usage:
    uv run python scripts/update_market_data.py --start 2026-01-01 --end 2026-09-04
    uv run python scripts/update_market_data.py --start 2026-01-01 --end 2026-09-04 \
        --symbols 600519.SH 000001.SZ
"""

from __future__ import annotations

import argparse
from datetime import date

from quantlab.data import ParquetStorage, TushareProvider


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
        "--symbols",
        nargs="*",
        default=[],
        help="Instrument IDs to download daily bars for (e.g. 600519.SH 000001.SZ).",
    )
    args = parser.parse_args()

    if args.start > args.end:
        parser.error(f"--start ({args.start}) must be <= --end ({args.end})")

    provider = TushareProvider()
    storage = ParquetStorage()

    securities = provider.get_securities()
    storage.save_securities(securities)
    print(f"securities: saved {len(securities)} rows -> {storage.securities_path}")

    calendar = provider.get_trading_calendar(args.start, args.end)
    storage.save_trading_calendar(calendar)
    print(f"calendar: saved {len(calendar)} rows -> {storage.calendar_path}")

    if args.symbols:
        bars = provider.get_daily_bars(args.symbols, args.start, args.end)
        paths = storage.save_daily_bars(bars)
        joined = ", ".join(str(path) for path in paths)
        print(f"daily: saved {len(bars)} rows -> {joined}")
    else:
        print("daily: skipped (use --symbols 600519.SH 000001.SZ to download daily bars)")


if __name__ == "__main__":
    main()
