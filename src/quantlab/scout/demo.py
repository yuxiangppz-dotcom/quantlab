"""Explicitly synthetic fixtures, confined to a caller-owned temporary directory."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from quantlab.data.models import AdjFactor, DailyBar, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI


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


def make_demo_sources(root: Path, session: date) -> tuple[Path, Path]:
    """Fake disclosures and comments for the explicitly synthetic offline walkthrough."""
    now = datetime.now(SHANGHAI) - timedelta(seconds=1)
    code = "600007.SH"
    common = {"ts_code": code, "trade_date": session.strftime("%Y%m%d")}
    seat = {
        **common,
        "reason": "合成单日演示",
        "exalter": "合成席位（非真实机构）",
        "buy": 10_000_000,
        "sell": 2_000_000,
        "net_buy": 8_000_000,
    }
    rows = {
        "top_list": [
            {
                **common,
                "reason": "合成单日演示",
                "l_buy": 10_000_000,
                "l_sell": 2_000_000,
                "net_amount": 8_000_000,
                "amount": 4e8,
            }
        ],
        "top_inst": [{**seat, "side": "0"}, {**seat, "side": "1"}],
        "block_trade": [
            {
                **common,
                "price": 12,
                "vol": 10,
                "amount": 120,
                "buyer": "合成买方",
                "seller": "合成卖方",
            }
        ],
    }
    disclosure_path = root / "synthetic_disclosures.json"
    disclosure_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshots": [
                    {
                        "dataset": dataset,
                        "trade_date": session.isoformat(),
                        "retrieved_at": now.isoformat(),
                        "published_at": None,
                        "rows": records,
                    }
                    for dataset, records in rows.items()
                ],
            },
            ensure_ascii=False,
        )
    )
    comments_path = root / "synthetic_comments.json"
    comments_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "合成评论（非真实平台数据）",
                "sampling_method": "synthetic_demo",
                "retrieved_at": now.isoformat(),
                "window_start": (now - timedelta(hours=2)).isoformat(),
                "window_end": now.isoformat(),
                "comments": [
                    {
                        "comment_id": str(i),
                        "instrument_id": code,
                        "published_at": (now - timedelta(hours=1)).isoformat(),
                        "text": "合成演示：这里是一条待核实题材讨论，不代表真实消息。",
                        "author_id": f"synthetic-{i}",
                    }
                    for i in range(2)
                ],
            },
            ensure_ascii=False,
        )
    )
    return disclosure_path, comments_path
