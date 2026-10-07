"""Explicitly synthetic three-stage walkthrough; no provider or model requests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from jsonschema import validate

from quantlab.data.models import AdjFactor, DailyBar, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.demo import make_demo_market
from quantlab.scout.models import SHANGHAI, Coverage, Evidence
from quantlab.scout.pipeline import DEFAULT_CONFIG, run_scout
from quantlab.scout.ranking_tracking import observe_ranking
from quantlab.scout.report import write_report


def synthetic_analysis() -> dict:
    return {
        "novelty": "price_only",
        "event_ids": [],
        "incremental_change": "没有确认新增公司事件",
        "economic_link": "量价假设，未据此推断经济受益",
        "exposure": "price_only",
        "importance": "事件规模未知",
        "scale_fact_ids": [],
        "h5_mechanism": "仅假设短期相对强势延续，需前瞻观察",
        "next_observation_date": None,
        "next_node_basis": "没有已核实日程",
        "next_node_is_hypothesis": True,
    }


def synthetic_comparisons(candidates: list[dict]) -> list[dict]:
    # An artificial ordering deliberately differs from the discovery score.
    ordered = sorted(candidates, key=lambda c: c["instrument_id"], reverse=True)
    comparator = ordered[-1]["instrument_id"]
    result = []
    for index, candidate in enumerate(ordered):
        code = candidate["instrument_id"]
        result.append(
            {
                "instrument_id": code,
                "primary_type": "trend_continuation",
                "type_labels": ["trend_continuation"],
                "rank": index + 1,
                "final_status": "focus" if index < 2 else "watch" if index < 5 else "unselected",
                "comparator_id": comparator if index < 5 else None,
                "comparison_strength": "weak",
                "evidence_reliability": "program_facts",
                "trade_conditions": {
                    "known": "只有历史日线",
                    "unknown": "未来开盘和实际可成交性未知",
                },
                "thesis": "合成示例假设趋势仍保持；仅用于测试比较结构",
                "risk": "量价延续可能失败，行业共振不能解释为公司事件催化",
                "invalidation": "若原有趋势停止保持则停止关注",
                "difference": f"与{comparator}相比，本例人为指定不同研究优先级；没有验证投资价值",
                "independent_basis": "承认只有量价假设，未补造公告利好",
                "unknowns": "真实收益、公司新事件和成交条件均待观察",
                "analysis": synthetic_analysis(),
                "evidence_ids": [f"market:{code}"],
                "fact_ids": [],
                "quant_claims": [],
            }
        )
    # When the input actually includes an event or an improved pullback, show
    # that type in the example. Generic contract fixtures remain trend-only.
    for row in result:
        candidate = next(c for c in candidates if c["instrument_id"] == row["instrument_id"])
        record = candidate.get("opportunity_record", {})
        if record.get("events"):
            row.update(
                primary_type="event_update",
                type_labels=["event_update"],
                final_status="watch",
                comparator_id=None,
                difference="本例无同类未选者，比较覆盖不足，保留事件线索观察",
            )
            row["analysis"] = {
                **synthetic_analysis(),
                "novelty": "unknown",
                "event_ids": [record["events"][0]["record_id"]],
                "incremental_change": "合成订单材料首次采集，市场新颖性未知",
                "exposure": "direct",
                "economic_link": "合成订单直接对象，规模和利润贡献未知",
            }
        elif record.get("pullback_qualified"):
            row.update(
                primary_type="pullback_improvement",
                type_labels=["pullback_improvement"],
                comparator_id=None,
                difference="原有正趋势与先跌后改善仅为合成量价依据",
            )
    # Keep the demonstration's TopN limit, including the event watch row.
    extra = max(0, sum(r["final_status"] == "watch" for r in result) - 5)
    for row in reversed(result):
        if extra and row["final_status"] == "watch" and row["primary_type"] != "event_update":
            row["final_status"] = "unselected"
            extra -= 1
    for row in result:
        others = [
            r
            for r in result
            if r["instrument_id"] != row["instrument_id"]
            and r["primary_type"] == row["primary_type"]
            and r["final_status"] == "unselected"
        ]
        if row["final_status"] != "unselected":
            row["comparator_id"] = (
                min(others, key=lambda r: abs(r["rank"] - row["rank"]))["instrument_id"]
                if others
                else None
            )
            row["difference"] = (
                f"与{row['comparator_id']}比较是假设性的人工示例"
                if others
                else "本例没有同类未选者，比较覆盖不足"
            )
    return result


def make_opportunity_example(root: Path) -> dict:
    """Creates new synthetic files only. Its observations are excluded from real summaries."""
    root.mkdir(parents=True, exist_ok=False)
    canonical = root / "synthetic_canonical"
    session = make_demo_market(canonical)
    storage = ParquetStorage(canonical)
    template = storage.load_securities()[0]
    codes = [f"600{i:03d}.SH" for i in range(180)]
    storage.save_securities(
        [
            replace(template, instrument_id=c, symbol=c[:6], name=f"合成企业{i:03d}（非真实标的）")
            for i, c in enumerate(codes)
        ]
    )
    old_days = [r.trade_date for r in storage.load_trading_calendar()]
    for day in old_days:
        originals = storage.load_daily_bars_by_date(day)
        storage.save_daily_bars_by_date(
            [replace(originals[i % 8], instrument_id=c) for i, c in enumerate(codes)], day
        )
        storage.save_adj_factors_by_date([AdjFactor(c, day, 1.0) for c in codes], day)
    # A single explicit pullback then improvement, with its old trend retained.
    previous_close = next(
        b.close
        for b in storage.load_daily_bars_by_date(old_days[-3])
        if b.instrument_id == "600009.SH"
    )
    declined = previous_close * 0.97
    for day, opening, closing in (
        (old_days[-2], previous_close, declined),
        (session, declined, declined * 1.03),
    ):
        storage.save_daily_bars_by_date(
            [
                replace(
                    b,
                    open=opening,
                    close=closing,
                    high=max(opening, closing) * 1.01,
                    low=min(opening, closing) * 0.99,
                )
                if b.instrument_id == "600009.SH"
                else b
                for b in storage.load_daily_bars_by_date(day)
            ],
            day,
        )
    future = [
        session + timedelta(days=i)
        for i in range(1, 18)
        if (session + timedelta(days=i)).weekday() < 5
    ][:10]
    storage.save_trading_calendar(
        [TradingCalendar("SSE", day, True) for day in [*old_days, *future]]
    )
    previous = {b.instrument_id: b.close for b in storage.load_daily_bars_by_date(session)}
    for index, day in enumerate(future):
        rows = []
        for i, code in enumerate(codes):
            opening = previous[code] * 1.08
            closing = opening * (1 + (index + 1) * (0.004 + (i % 5) * 0.002))
            rows.append(
                DailyBar(
                    code,
                    day,
                    opening,
                    closing * 1.01,
                    opening * 0.985,
                    closing,
                    previous[code],
                    10000000.0,
                    200000000.0,
                )
            )
        storage.save_daily_bars_by_date(rows, day)
        storage.save_adj_factors_by_date([AdjFactor(c, day, 1.0) for c in codes], day)
    now = datetime.combine(session, datetime.min.time().replace(hour=19), SHANGHAI)
    sectors = root / "synthetic_sectors.json"
    sectors.write_text(
        json.dumps(
            {
                "observed_at": now.isoformat(),
                "memberships": {c: f"合成行业{i % 3}" for i, c in enumerate(codes)},
            }
        ),
        encoding="utf-8",
    )
    source = Evidence(
        "synthetic",
        "重大订单公告（合成）",
        "合成正式订单线索，规模和盈利影响待确认",
        "https://example.org/synthetic-order",
        now.replace(hour=18).isoformat(),
        now.isoformat(),
        "company_event_date_only",
        (codes[0],),
        event_dates=(session.isoformat(),),
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    class ScriptedClient:
        model = "synthetic_scripted_response"

        def __init__(self, *args):
            self.calls = []

        def ask(self, prompt, schema, search=False):
            stage = len(self.calls)
            self.calls.append(
                {"synthetic": True, "stage": stage, "input_chars": len(prompt), "search": search}
            )
            if stage == 0:
                response = {"hypotheses": []}
            elif stage == 1:
                packet = json.loads(prompt[prompt.index('{"candidates":') :])
                response = {
                    "hypotheses": [],
                    "opportunities": [
                        {"instrument_id": c["instrument_id"], "analysis": synthetic_analysis()}
                        for c in packet["candidates"]
                    ],
                }
            else:
                packet = json.loads(prompt[prompt.index('{"timing":') :])
                response = {
                    "market_view": "合成市场背景，仅检验流程",
                    "comparisons": synthetic_comparisons(packet["candidates"]),
                }
            validate(response, schema)
            return response, {"synthetic": True, "response": response}

    def synthetic_writer(output, report, raw):
        report.update(status="demo", synthetic=True, ai_provider="mock")
        report["limitations"].insert(
            0, "合成行情、脚本模型响应和模拟时钟，不是真实预测或模型验证。"
        )
        return write_report(output, report, raw)

    config = {
        **DEFAULT_CONFIG,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "opportunity_selection": True,
        "max_output_tokens": 32768,
        "tushare_news_sources": [],
        "tushare_industry": False,
        "tushare_disclosures": False,
    }
    with (
        patch("quantlab.scout.pipeline.datetime", Clock),
        patch("quantlab.scout.pipeline.DeepSeekResearch", ScriptedClient),
        patch(
            "quantlab.scout.pipeline.collect_sources",
            return_value=([source], [Coverage("synthetic", "synthetic", 1)]),
        ),
        patch("quantlab.scout.report.write_report", side_effect=synthetic_writer),
        patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("Network forbidden in synthetic walkthrough"),
        ),
    ):
        run, report = run_scout(canonical, root / "runs", config, online=True, sectors_path=sectors)
    if report["opportunity"]["validation"]["status"] != "complete":
        raise ValueError(f"Synthetic integration failed: {report['opportunity']['validation']}")
    pending = observe_ranking(
        run,
        canonical,
        root / "synthetic_ranking_tracking",
        allow_synthetic=True,
        observed_at=datetime.combine(future[0], datetime.min.time().replace(hour=12), SHANGHAI),
    )
    mature = observe_ranking(
        run,
        canonical,
        root / "synthetic_ranking_tracking",
        allow_synthetic=True,
        observed_at=datetime.combine(future[-1], datetime.min.time().replace(hour=19), SHANGHAI),
    )
    return {
        "synthetic": True,
        "run": str(run),
        "pending_observation": str(pending),
        "mature_observation": str(mature),
        "calls": report["ai_calls"],
    }
