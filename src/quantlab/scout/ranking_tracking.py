"""Independent all-deep-candidate observations. Old TopN tracking is untouched."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from statistics import mean, median

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, fingerprint, finite
from quantlab.scout.opportunities import TYPE_LABELS, VERSION, load_event_history
from quantlab.scout.tracking import HORIZONS, _eligible_target, _write_snapshot

DEFINITION = "all_deep_rank_d_open_h_close_adjusted_v1"


def observe_ranking(
    run_dir: Path,
    canonical_dir: Path,
    output_root: Path,
    *,
    allow_synthetic: bool = False,
    observed_at: datetime | None = None,
) -> Path:
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    digest = fingerprint(report)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if digest != manifest["report_sha256"]:
        raise ValueError("Original report integrity check failed")
    synthetic = report.get("status") == "demo" or report.get("synthetic", False)
    if synthetic and not allow_synthetic:
        raise ValueError("Synthetic ranking cannot enter real observations")
    if observed_at is not None and not allow_synthetic:
        raise ValueError("Clock override is only for explicitly synthetic examples")
    if output_root.resolve().is_relative_to(canonical_dir.resolve()):
        raise ValueError("Ranking output cannot be inside canonical data")
    now = observed_at or datetime.now(SHANGHAI)
    storage = ParquetStorage(canonical_dir)
    days = sorted(
        {r.trade_date for r in storage.load_trading_calendar() if r.exchange == "SSE" and r.is_open}
    )
    freeze = report.get("opportunity_freeze")
    result = {
        "run_id": report["run_id"],
        "report_sha256": digest,
        "observed_at": now.isoformat(),
        "definition_version": DEFINITION,
        "synthetic": synthetic,
        "rows": [],
        "primary_horizon_sessions": 5,
        "primary_eligibility": "legacy_structure_no_ranking",
        "limitations": (
            "Adjusted prices and daily lows are observations, not fills, "
            "stop execution, net returns, full drawdown paths or risk-adjusted alpha."
        ),
    }
    if not freeze:
        return _write_snapshot(result, output_root)
    if freeze.get("version") != VERSION or freeze.get("status") != "complete":
        raise ValueError("Ranking freeze is incomplete or uses an unknown version")
    if freeze["input_sha256"] != fingerprint(report["selection_input_packet"]):
        raise ValueError("Ranking freeze does not match exact model input")
    target, eligibility = _eligible_target(
        {**report, "status": "complete"} if synthetic else report, days
    )
    result.update(
        primary_eligibility=eligibility,
        d_session=target.isoformat() if target else None,
        frozen=freeze,
        target_day=report["timing"].get("target_session"),
    )
    if eligibility != "eligible":
        return _write_snapshot(result, output_root)
    index = days.index(target)
    asof = date.fromisoformat(freeze["market_asof_session"])
    needed = {asof, *days[index : index + max(HORIZONS)]}
    if index:
        needed.add(days[index - 1])
    bars = {d: {b.instrument_id: b for b in storage.load_daily_bars_by_date(d)} for d in needed}
    factors = {
        d: {f.instrument_id: f.adj_factor for f in storage.load_adj_factors_by_date(d)}
        for d in needed
    }
    limits = {r.instrument_id: r for r in storage.load_daily_price_limits_by_date(target)}

    def price(code: str, day: date | None, field: str) -> tuple[float | None, str]:
        if day is None:
            return None, "calendar_unavailable"
        if datetime.combine(day, time(18), SHANGHAI) > now:
            return None, "not_yet_due"
        if not storage.daily_bars_path(day).exists():
            return None, "partition_missing"
        bar = bars[day].get(code)
        if bar is None:
            return None, "bar_missing_or_halt_unknown"
        factor = factors[day].get(code)
        if not finite(factor) or factor <= 0:
            return None, "factor_missing_or_invalid"
        value = getattr(bar, field)
        if not finite(value) or value <= 0:
            return None, "price_invalid"
        if not all(finite(x) and x > 0 for x in (bar.open, bar.high, bar.low, bar.close)) or not (
            bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        ):
            return None, "ohlc_invalid"
        return value * factor, "available"

    returns = {}

    def observation(code: str, h: int) -> dict:
        if (code, h) in returns:
            return returns[code, h]
        end = days[index + h - 1] if index + h - 1 < len(days) else None
        opening, open_status = price(code, target, "open")
        closing, end_status = price(code, end, "close")
        last, last_status = price(code, asof, "close")
        status = (
            f"d_open_{open_status}"
            if open_status != "available"
            else f"end_{end_status}"
            if end_status != "available"
            else "observed"
        )
        period_days = days[index : index + h] if end else []
        lows = [price(code, day, "low") for day in period_days]
        low_coverage = sum(state == "available" for _, state in lows)
        adverse = (
            min(0.0, min(value for value, _ in lows) / opening - 1)
            if opening and lows and low_coverage == h
            else None
        )
        bar, limit = bars[target].get(code), limits.get(code)
        returns[code, h] = {
            "instrument_id": code,
            "horizon_sessions": h,
            "end_session": end.isoformat() if end else None,
            "status": status,
            "d_open_status": open_status,
            "end_close_status": end_status,
            "adjusted_price_return": closing / opening - 1 if opening and closing else None,
            "report_reference_session": asof.isoformat(),
            "report_reference_status": last_status,
            "report_reference_close_to_d_open": opening / last - 1 if opening and last else None,
            "report_reference_note": (
                "Last archived as-of close is a proxy, "
                "not a quote at report generation; do not include this gap in H5."
            ),
            "adverse_daily_low_change": adverse,
            "path_valid_sessions": low_coverage,
            "path_expected_sessions": h,
            "d_limit_state": "known" if limit else "unknown",
            "d_open_at_upper_limit": abs(bar.open - limit.up_limit) < 0.005
            if bar and limit and opening
            else None,
            "d_one_price_session": abs(bar.high - bar.low) < 0.005 if bar and opening else None,
            "actual_execution": "unknown_with_only_daily_OHLC",
            "suspension_status": "unknown_from_missing_bar_alone",
        }
        return returns[code, h]

    for row in freeze["rows"]:
        for horizon in HORIZONS:
            own = observation(row["instrument_id"], horizon)
            reference_ids = row["reference_ids"]
            peer_rows = [observation(c, horizon) for c in reference_ids]
            valid = [p["adjusted_price_return"] for p in peer_rows if p["status"] == "observed"]
            complete = row["reference_status"] == "frozen" and len(valid) == len(reference_ids)
            peer_return = mean(valid) if valid and complete else None
            result["rows"].append(
                {
                    **own,
                    **row,
                    "reference_original_count": len(reference_ids),
                    "reference_valid_count": len(valid),
                    "reference_missing_reasons": dict(
                        Counter(p["status"] for p in peer_rows if p["status"] != "observed")
                    ),
                    "reference_coverage": len(valid) / len(reference_ids)
                    if reference_ids
                    else None,
                    "peer_equal_weight_price_return": peer_return,
                    "peer_status": "complete_frozen_reference"
                    if complete
                    else "insufficient_frozen_members"
                    if row["reference_status"] != "frozen"
                    else "incomplete_reference_prices",
                    "relative_to_peer_price_change": own["adjusted_price_return"] - peer_return
                    if own["status"] == "observed" and peer_return is not None
                    else None,
                }
            )
    later_events = load_event_history(run_dir.parent.parent / "event_index", now)
    result["event_followups"] = []
    for row in freeze["rows"]:
        parents = set(row["event_ids"])
        related = []
        for event in sorted(later_events, key=lambda e: e["retrieved_at"]):
            if (
                event["instrument_id"] == row["instrument_id"]
                and event["previous_record_id"] in parents
            ):
                parents.add(event["record_id"])
                if event["record_id"] not in row["event_ids"]:
                    related.append(
                        {
                            "event_id": event["record_id"],
                            "source_ids": event["source_ids"],
                            "published_at": event["published_at"],
                            "retrieved_at": event["retrieved_at"],
                            "change": event["change"],
                            "content_excerpt": event["content_excerpt"],
                        }
                    )
        result["event_followups"].append(
            {
                "instrument_id": row["instrument_id"],
                "status": "related_update_requires_interpretation"
                if related
                else "future_evidence_unknown"
                if row["event_ids"]
                else "not_event_hypothesis",
                "related_updates": related,
            }
        )
    actual = sorted(
        (r for r in result["rows"] if r["horizon_sessions"] == 5 and r["status"] == "observed"),
        key=lambda r: (-r["adjusted_price_return"], r["instrument_id"]),
    )
    for row in actual:
        # Equal returns share average rank; code ordering does not break outcome ties.
        positions = [
            i + 1
            for i, r in enumerate(actual)
            if r["adjusted_price_return"] == row["adjusted_price_return"]
        ]
        row["h5_actual_rank"] = mean(positions)
    stages = freeze["stages"]
    cheap, deep = set(stages["cheap_candidates"]), set(stages["deep_candidates"])
    eligible_h5 = [observation(c, 5) for c in freeze["eligible_ids"]]
    winners = [
        r for r in eligible_h5 if r["status"] == "observed" and r["adjusted_price_return"] > 0
    ]
    result["diagnostics"] = {
        "scope": "frozen_eligible_pool_only_not_full_market_recall",
        "eligible_original": len(eligible_h5),
        "eligible_h5_valid": sum(r["status"] == "observed" for r in eligible_h5),
        "positive_outside_cheap": [
            r["instrument_id"] for r in winners if r["instrument_id"] not in cheap
        ],
        "positive_cheap_not_deep": [
            r["instrument_id"] for r in winners if r["instrument_id"] in cheap - deep
        ],
        "positive_deep_bottom": [
            r["instrument_id"]
            for r in actual
            if r["adjusted_price_return"] > 0 and r["rank_band"] == "bottom"
        ],
        "positive_preopen_larger_than_h5": [
            r["instrument_id"]
            for r in actual
            if (r["report_reference_close_to_d_open"] or 0) > max(r["adjusted_price_return"], 0)
        ],
        "event_hypothesis_realization": "unknown_requires_specific_future_evidence_not_price_alone",
    }
    result["eligible_h5"] = eligible_h5
    return _write_snapshot(result, output_root)


def _group(rows: list[dict]) -> dict:
    valid = [r for r in rows if r["status"] == "observed"]
    values = [r["adjusted_price_return"] for r in valid]
    peers = [
        r["relative_to_peer_price_change"]
        for r in valid
        if r["relative_to_peer_price_change"] is not None
    ]
    lows = [
        r["adverse_daily_low_change"] for r in valid if r["adverse_daily_low_change"] is not None
    ]
    return {
        "stock_ids": [r["instrument_id"] for r in rows],
        "original_count": len(rows),
        "valid_count": len(valid),
        "positive_count": sum(v > 0 for v in values),
        "positive_fraction": mean(v > 0 for v in values) if values else None,
        "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "peer_valid_count": len(peers),
        "relative_to_peer_mean": mean(peers) if peers and len(peers) == len(rows) else None,
        "adverse_valid_count": len(lows),
        "adverse_mean": mean(lows) if lows and len(lows) == len(rows) else None,
        "worst_observed_daily_low": min(lows) if lows else None,
        "missing_reasons": dict(Counter(r["status"] for r in rows if r["status"] != "observed")),
    }


def _h5_from_frozen(freeze: dict, observation: dict | None, target: str) -> list[dict]:
    """Frozen identities are the denominator, even for empty/partial observations."""
    frozen = {row["instrument_id"]: row for row in freeze["rows"]}
    if len(frozen) != len(freeze["rows"]):
        raise ValueError("Ranking freeze contains duplicate securities")
    if observation is None:
        return [{**row, "status": "observation_not_run"} for row in frozen.values()]
    if "frozen" in observation and observation["frozen"] != freeze:
        raise ValueError("Ranking snapshot frozen identity differs from report")
    if observation.get("d_session") not in {None, target}:
        raise ValueError("Ranking snapshot target differs from report")
    eligibility = observation.get("primary_eligibility", "eligible")
    seen, h5 = set(), {}
    for mark in observation["rows"]:
        code, horizon = mark["instrument_id"], mark["horizon_sessions"]
        if code not in frozen or type(horizon) is not int or horizon not in HORIZONS:
            raise ValueError("Ranking snapshot contains an outside security or horizon")
        key = (code, horizon)
        if key in seen:
            raise ValueError("Ranking snapshot contains a duplicate security/horizon")
        seen.add(key)
        if any(k not in mark or mark[k] != value for k, value in frozen[code].items()):
            raise ValueError("Ranking snapshot row differs from frozen identity")
        if eligibility != "eligible":
            raise ValueError("Ineligible ranking snapshot cannot contain observed rows")
        if mark["status"] == "observed" and not finite(mark.get("adjusted_price_return")):
            raise ValueError("Observed ranking price return is missing or invalid")
        if horizon == 5:
            h5[code] = mark
    missing_reason = (
        eligibility
        if eligibility != "eligible"
        else "empty_observation_snapshot"
        if not observation["rows"]
        else "h5_observation_missing"
    )
    return [
        {**h5[code], **row} if code in h5 else {**row, "status": missing_reason}
        for code, row in frozen.items()
    ]


def summarize_ranking(report_root: Path, observation_root: Path, output_path: Path) -> Path:
    reports, excluded = {}, []
    for path in sorted(report_root.glob("*/report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("status") == "demo" or report.get("synthetic"):
            excluded.append({"run_id": report["run_id"], "reason": "synthetic"})
            continue
        manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
        if fingerprint(report) != manifest["report_sha256"]:
            raise ValueError("Ranking summary report integrity failure")
        target = report.get("timing", {}).get("target_session")
        if not target or not report.get("opportunity_freeze"):
            excluded.append({"run_id": report["run_id"], "reason": "legacy_structure_no_ranking"})
            continue
        _, eligibility = _eligible_target(report, [date.fromisoformat(target)])
        if eligibility != "eligible":
            excluded.append({"run_id": report["run_id"], "reason": eligibility})
            continue
        if target not in reports or (report["finished_at"], report["run_id"]) > (
            reports[target]["finished_at"],
            reports[target]["run_id"],
        ):
            reports[target] = report
    snapshots = {}
    for path in sorted(observation_root.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("definition_version") != DEFINITION or item.get("synthetic"):
            continue
        key = item["run_id"]
        if key not in snapshots or item["observed_at"] > snapshots[key]["observed_at"]:
            snapshots[key] = item
    date_groups, stock_counts, versions = [], Counter(), Counter()
    names = (
        "focus",
        "watch",
        "selected",
        "unselected",
        "top",
        "middle",
        "bottom",
        "insufficient",
        *TYPE_LABELS,
    )
    for target, report in sorted(reports.items()):
        freeze = report["opportunity_freeze"]
        observation = snapshots.get(report["run_id"])
        if observation and observation["report_sha256"] != fingerprint(report):
            raise ValueError("Ranking observation belongs to another frozen report")
        rows = _h5_from_frozen(freeze, observation, target)
        stock_counts.update(r["instrument_id"] for r in rows)
        version_identity = {
            k: freeze["metadata"].get(k)
            for k in ("provider", "model", "prompt_version", "config_sha256")
        }
        version_id = fingerprint(version_identity)
        versions.update([version_id])
        for name in names:
            group = [
                r
                for r in rows
                if (
                    r["final_status"] in {"focus", "watch"}
                    if name == "selected"
                    else r["final_status"] == name
                    if name in {"focus", "watch", "unselected"}
                    else r.get("primary_type") == name
                    if name in TYPE_LABELS
                    else r["rank_band"] == name
                )
            ]
            date_groups.append(
                {
                    "target_day": target,
                    "run_id": report["run_id"],
                    "version_id": version_id,
                    "group": name,
                    **_group(group),
                }
            )
    summary = {}
    for name in names:
        dates = [r for r in date_groups if r["group"] == name]
        complete = [
            r for r in dates if r["original_count"] and r["valid_count"] == r["original_count"]
        ]
        means = [r["mean"] for r in complete]
        stock_ids = [code for r in dates for code in r["stock_ids"]]
        summary[name] = {
            "original_stock_days": sum(r["original_count"] for r in dates),
            "valid_stock_days": sum(r["valid_count"] for r in dates),
            "independent_target_days": len(complete),
            "unique_stock_count": len(set(stock_ids)),
            "repeat_stock_days": len(stock_ids) - len(set(stock_ids)),
            "mean": mean(means) if means else None,
            "median": median(means) if means else None,
            "positive_fraction": mean(r["positive_fraction"] for r in complete)
            if complete
            else None,
            "relative_to_peer_mean": mean(r["relative_to_peer_mean"] for r in complete)
            if complete and all(r["relative_to_peer_mean"] is not None for r in complete)
            else None,
            "adverse_mean": mean(r["adverse_mean"] for r in complete)
            if complete and all(r["adverse_mean"] is not None for r in complete)
            else None,
            "missing_reasons": dict(
                Counter(
                    {
                        reason: sum(r["missing_reasons"].get(reason, 0) for r in dates)
                        for reason in {k for r in dates for k in r["missing_reasons"]}
                    }
                )
            ),
        }
    result = {
        "definition_version": DEFINITION,
        "primary_horizon_sessions": 5,
        "effective_reports": [
            {"target_day": d, "run_id": r["run_id"]} for d, r in sorted(reports.items())
        ],
        "groups": summary,
        "per_target_day": date_groups,
        "unique_stock_count": len(stock_counts),
        "repeat_stock_days": sum(max(0, n - 1) for n in stock_counts.values()),
        "maximum_stock_repetition": max(stock_counts.values(), default=0),
        "version_counts": dict(versions),
        "excluded_reports": excluded,
        "independence_note": (
            "Target-day counts are distinct dates, not independent return samples; "
            "overlapping H5 windows, repeated stocks and shared themes remain dependent."
        ),
        "aggregation": (
            "H5 only. Complete group means first by target date; cross-date mean "
            "and median of those date means; positive fraction averaged by date. "
            "Partial groups are shown but excluded from aggregate conclusions."
        ),
        "checkpoint": (
            "20 mature target dates is a first diagnostic checkpoint, not evidence of efficacy."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    path = output_path.with_name(output_path.stem + "-" + fingerprint(result)[:12] + ".json")
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    elif json.loads(path.read_text(encoding="utf-8")) != result:
        raise ValueError("Ranking summary snapshot collision")
    markdown = path.with_suffix(".md")
    if not markdown.exists():

        def pct(value):
            return f"{value:.2%}" if value is not None else "待观察/覆盖不足"

        lines = [
            "# Scout 全部深查股 H5 前瞻诊断",
            "",
            "先按目标日形成完整组均值，再按日期汇总；部分覆盖只在逐日表展示。",
            "中位数为目标日组均值的中位数。同行指标为同口径价格差，不是风险调整alpha。",
            "",
            "| 分组 | 原股票日 | 有效股票日 | 完整目标日 | 正收益比例 | 均值 | "
            "中位 | 同行差 | 不利低点均值 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, values in summary.items():
            lines.append(
                f"| {name} | {values['original_stock_days']} | {values['valid_stock_days']} | "
                f"{values['independent_target_days']} | {pct(values['positive_fraction'])} | "
                f"{pct(values['mean'])} | {pct(values['median'])} | "
                f"{pct(values['relative_to_peer_mean'])} | {pct(values['adverse_mean'])} |"
            )
        lines.extend(
            [
                "",
                f"唯一股票 {len(stock_counts)}；重复股票日 {result['repeat_stock_days']}；"
                f"模型/提示/配置版本数 {len(versions)}。",
                "不同版本混合时不作单一版本效果结论；逐日版本和缺失原因见同名JSON。",
                "20个成熟目标日是初步诊断点，当前不是有效性证明。",
            ]
        )
        with markdown.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return path
