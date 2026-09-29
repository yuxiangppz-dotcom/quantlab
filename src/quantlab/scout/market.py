"""Read existing canonical files without updating them or asserting execution feasibility."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, Candidate, finite


def latest_completed_session(storage: ParquetStorage, now: datetime) -> date:
    local = now.astimezone(SHANGHAI)
    calendar = storage.load_trading_calendar()
    if not calendar or max(x.trade_date for x in calendar) < local.date():
        raise ValueError("Trading calendar missing/stale; update local data first")
    # Conservative daily-data cutoff; intraday bars are not accepted as daily bars.
    dates = {
        x.trade_date
        for x in calendar
        if x.is_open
        and x.exchange == "SSE"
        and (x.trade_date < local.date() or local.time() >= time(18))
        and x.trade_date <= local.date()
    }
    if not dates:
        raise ValueError("No completed trading session in calendar")
    return max(dates)


def scan_market(
    canonical_dir: Path, session: date, min_amount: float = 100_000_000
) -> tuple[dict[str, Candidate], dict]:
    storage = ParquetStorage(canonical_dir)
    days = sorted(
        {
            x.trade_date
            for x in storage.load_trading_calendar()
            if x.is_open and x.exchange == "SSE" and x.trade_date <= session
        }
    )[-21:]
    if len(days) != 21 or days[-1] != session:
        raise ValueError("Need 21 trading sessions ending on requested session")
    securities = storage.load_securities()
    if not securities:
        raise ValueError("Missing securities master")
    histories: dict[str, list] = {}
    factors: dict[tuple[str, date], float] = {}
    for day in days:
        bars = storage.load_daily_bars_by_date(day)
        adj = storage.load_adj_factors_by_date(day)
        if not bars or not adj:
            raise ValueError(f"Missing daily/adj_factor partition: {day}")
        for bar in bars:
            histories.setdefault(bar.instrument_id, []).append(bar)
        factors.update({(x.instrument_id, day): x.adj_factor for x in adj})
    basics = {x.instrument_id: x for x in storage.load_daily_basic_by_date(session)}
    limits = {x.instrument_id: x for x in storage.load_daily_price_limits_by_date(session)}
    candidates: dict[str, Candidate] = {}
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for security in securities:
        code = security.instrument_id
        if (
            security.market not in {"SH", "SZ"}
            or security.board != "主板"
            or security.list_status != "L"
            or "ST" in security.name.upper()
            or "退" in security.name
            or (session - security.list_date).days < 120
        ):
            reject("universe")
            continue
        bars = histories.get(code, [])
        if len(bars) != 21 or [x.trade_date for x in bars] != days:
            reject("incomplete_history")
            continue
        if any(
            not all(finite(v) for v in (b.open, b.high, b.low, b.close, b.amount, b.volume))
            or b.low <= 0
            or b.volume <= 0
            or b.amount <= 0
            or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
            for b in bars
        ):
            reject("invalid_bars")
            continue
        adjustments = [factors.get((code, b.trade_date)) for b in bars]
        if any(not finite(x) or x <= 0 for x in adjustments):
            reject("missing_adjustment")
            continue
        last = bars[-1]
        if last.amount < min_amount:
            reject("liquidity_filter")
            continue
        closes = [b.close * f for b, f in zip(bars, adjustments, strict=True)]
        high20 = max(b.high * f for b, f in zip(bars[:-1], adjustments[:-1], strict=True))
        ret1 = closes[-1] / closes[-2] - 1
        ret5 = closes[-1] / closes[-6] - 1
        ret20 = closes[-1] / closes[0] - 1
        amount_ratio = last.amount / (sum(b.amount for b in bars[-6:-1]) / 5)
        close_location = (
            (last.close - last.low) / (last.high - last.low) if last.high > last.low else None
        )
        basic = basics.get(code)
        limit = limits.get(code)
        up_limit = getattr(limit, "up_limit", None)
        cautions = ["证券主表为当前快照；不构成历史时点回测", "未验证次日可成交性"]
        if last.high == last.low:
            cautions.append("当日一价行情，不能假设排队可成交")
        if ret5 > 0.25:
            cautions.append("近5日涨幅较大，关注预期透支")
        if not basic:
            cautions.append("缺少当日换手率")
        if not limit:
            cautions.append("缺少当日涨跌停价；未推算涨停")
        metrics = {
            "return_1d": ret1,
            "return_5d": ret5,
            "return_20d": ret20,
            "amount_cny": last.amount,
            "amount_ratio_5d": amount_ratio,
            "close_location": close_location,
            "breakout_20d": closes[-1] / high20 - 1,
            "close": last.close,
            "turnover_rate_pct": basic.turnover_rate * 100 if basic else None,
            "up_limit": up_limit if finite(up_limit) else None,
        }
        if any(v is not None and not finite(v) for v in metrics.values()):
            reject("invalid_metrics")
            continue
        candidates[code] = Candidate(code, security.name, metrics, 0)
        candidates[code].cautions = cautions
        candidates[code].evidence_ids = [f"market:{code}"]
    if not candidates:
        raise ValueError("No eligible stocks with complete valid history")
    frame = pd.DataFrame({k: v.metrics for k, v in candidates.items()}).T
    # Transparent heuristic baseline, NOT a fitted model or a probability.
    scores = (
        sum(
            frame[col].rank(pct=True)
            for col in ("return_1d", "return_5d", "amount_ratio_5d", "breakout_20d")
        )
        / 4
    )
    for code, candidate in candidates.items():
        candidate.score = float(scores[code])
        m = candidate.metrics
        if m["return_1d"] > 0.02 and m["amount_ratio_5d"] > 1.2:
            candidate.routes.append("量价异动")
        if m["breakout_20d"] >= 0 and m["return_5d"] > 0:
            candidate.routes.append("趋势突破")
    return candidates, {
        "session": session.isoformat(),
        "history_start": days[0].isoformat(),
        "eligible_count": len(candidates),
        "rejected": rejected,
        "daily_basic_count": len(basics),
        "price_limit_count": len(limits),
        "security_master": "current_snapshot_not_historical_PIT",
        "median_return_1d": float(frame.return_1d.median()),
        "positive_fraction": float((frame.return_1d > 0).mean()),
        "score_definition": "mean percentile rank of 1d/5d return, amount ratio, breakout",
    }


def add_sectors(
    universe: dict[str, Candidate], memberships: dict[str, str], quota: int = 8
) -> list[str]:
    """Use current externally supplied memberships; never invent absent sectors."""
    groups: dict[str, list[Candidate]] = {}
    for code, sector in memberships.items():
        if code in universe and sector:
            groups.setdefault(sector, []).append(universe[code])
    strong = []
    for sector, members in groups.items():
        if len(members) < 3:
            continue
        breadth = sum(x.metrics["return_1d"] > 0 for x in members) / len(members)
        mean_return = sum(x.metrics["return_1d"] for x in members) / len(members)
        if breadth >= 0.6 and mean_return > 0.01:
            strong.append((mean_return, sector, members))
    selected = []
    for _, sector, members in sorted(strong, key=lambda x: (-x[0], x[1])):
        for candidate in sorted(members, key=lambda x: (-x.score, x.instrument_id))[:2]:
            candidate.routes.append(f"板块领先:{sector}")
            selected.append(candidate.instrument_id)
            if len(selected) >= quota:
                return selected
    return selected
