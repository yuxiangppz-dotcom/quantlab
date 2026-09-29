"""Explicitly synthetic fixtures, confined to a caller-owned temporary directory."""

from datetime import date, timedelta
from pathlib import Path

from quantlab.data.models import AdjFactor, DailyBar, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage


def make_demo_market(root: Path) -> date:
    storage = ParquetStorage(root)
    start = date(2026, 1, 5)
    days = [
        start + timedelta(days=i) for i in range(40) if (start + timedelta(days=i)).weekday() < 5
    ][:21]
    storage.save_securities(
        [
            Security(
                f"60000{i}.SH",
                f"60000{i}",
                f"合成演示{i}（非真实标的）",
                "SSE",
                "SH",
                "主板",
                "L",
                date(2020, 1, 1),
                None,
            )
            for i in range(8)
        ]
    )
    storage.save_trading_calendar([TradingCalendar("SSE", day, True) for day in days])
    for index, day in enumerate(days):
        bars, factors = [], []
        for i in range(8):
            previous = 10 * (1 + 0.004 * (i + 1)) ** max(0, index - 1)
            close = 10 * (1 + 0.004 * (i + 1)) ** index
            amount = 200_000_000 * (2 if index == 20 else 1)
            bars.append(
                DailyBar(
                    f"60000{i}.SH",
                    day,
                    previous,
                    close * 1.01,
                    previous * 0.99,
                    close,
                    previous,
                    amount / close,
                    amount,
                )
            )
            factors.append(AdjFactor(f"60000{i}.SH", day, 1.0))
        storage.save_daily_bars_by_date(bars, day)
        storage.save_adj_factors_by_date(factors, day)
    return days[-1]
