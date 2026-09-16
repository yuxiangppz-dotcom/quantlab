"""Common-period benchmarks, actual holdings, costs and dependent-sample IC checks."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from quantlab.research.ml.artifacts import complete, verify_completed, write_frame
from quantlab.research.ml.io import research_output, sha256, write_json


def block_mean_interval(values, block_length, *, seed=20260916, repetitions=500):
    values = np.asarray(values, dtype=float)
    if len(values) < 3 * block_length or np.isfinite(values).sum() < 3 * block_length:
        return {"status": "insufficient_history", "block_sessions": block_length}
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(repetitions):
        starts = rng.integers(0, len(values), size=int(np.ceil(len(values) / block_length)))
        indices = np.concatenate([(s + np.arange(block_length)) % len(values) for s in starts])
        sample = values[indices[: len(values)]]
        if np.isfinite(sample).any():
            means.append(float(np.nanmean(sample)))
    lo, hi = np.quantile(means, [0.025, 0.975])
    return {
        "status": "computed",
        "mean": float(np.nanmean(values)),
        "lower_95": float(lo),
        "upper_95": float(hi),
        "block_sessions": block_length,
        "method": "circular_block_bootstrap",
        "repetitions": repetitions,
        "multiple_testing_adjusted": False,
    }


def comparison_metrics(returns, benchmark):
    r, b = np.asarray(returns, float), np.asarray(benchmark, float)
    if len(r) != len(b) or len(r) == 0 or not (np.isfinite(r).all() and np.isfinite(b).all()):
        raise ValueError("finite aligned daily returns required")
    if (r <= -1).any() or (b <= -1).any():
        raise ValueError("nonpositive wealth cannot be compounded")
    nav = np.r_[1, np.cumprod(1 + r)]
    bnav = np.r_[1, np.cumprod(1 + b)]
    excess = r - b
    std = excess.std(ddof=1) if len(r) > 1 else 0
    beta = np.cov(r, b, ddof=1)[0, 1] / b.var(ddof=1) if len(b) > 1 and b.var(ddof=1) > 0 else None
    return {
        "sessions": len(r),
        "net_return": float(nav[-1] - 1),
        "benchmark_return": float(bnav[-1] - 1),
        "relative_wealth_return": float(nav[-1] / bnav[-1] - 1),
        "tracking_error_annualized": float(std * np.sqrt(252)) if len(r) > 1 else None,
        "information_ratio": float(excess.mean() / std * np.sqrt(252)) if std > 0 else None,
        "beta": float(beta) if beta is not None else None,
        "max_drawdown": float((nav / np.maximum.accumulate(nav) - 1).min()),
    }


def portfolio_style(positions, ledger, exposures, *, decision_hour=16):
    """Report missing exposure coverage; never treat an unknown exposure as zero."""
    required = {"session", "instrument_id", "available_at"}
    if required - set(exposures):
        raise ValueError("exposures require session, instrument_id, available_at")
    styles = [k for k in exposures if k not in required]
    x = exposures.copy()
    x["session"] = pd.to_datetime(x.session).dt.strftime("%Y-%m-%d")
    if x.duplicated(["session", "instrument_id"]).any():
        raise ValueError("duplicate exposure row")
    known = pd.to_datetime(x.available_at, utc=True, errors="raise")
    cutoff = pd.to_datetime(x.session).dt.tz_localize("Asia/Shanghai") + pd.Timedelta(
        hours=decision_hour
    )
    if known.isna().any() or (known > cutoff).any():
        raise ValueError("style exposure unavailable by session cutoff")
    if positions.empty:
        return pd.DataFrame()
    p = positions.copy()
    p["value"] = p.quantity * p.raw_mark_fen
    p = p.groupby(["session", "instrument_id"], as_index=False).value.sum()
    p = p.merge(x, how="left", on=["session", "instrument_id"], validate="one_to_one")
    p = p.merge(ledger[["session", "equity_fen"]], on="session", validate="many_to_one")
    p["weight"] = p.value / p.equity_fen
    rows = []
    for day, group in p.groupby("session"):
        row = {"session": day}
        for style in styles:
            values = pd.to_numeric(group[style], errors="raise")
            valid = np.isfinite(values)
            row[f"{style}_covered_nav_weight"] = float(group.loc[valid, "weight"].sum())
            row[f"{style}_weighted_exposure"] = (
                float((group.loc[valid, "weight"] * values[valid]).sum()) if valid.any() else None
            )
        rows.append(row)
    return pd.DataFrame(rows)


def build_report(replay, benchmark_path, output, *, training=None, exposures_path=None):
    research_output(output)
    verify_completed(replay)
    benchmark_hash = sha256(benchmark_path)
    exposures_hash = sha256(exposures_path) if exposures_path else None
    benchmark = pd.read_parquet(benchmark_path)
    if set(benchmark.columns) != {"session", "benchmark_return"}:
        raise ValueError("benchmark requires exactly session and benchmark_return columns")
    benchmark["session"] = pd.to_datetime(benchmark.session).dt.strftime("%Y-%m-%d")
    if benchmark.session.duplicated().any():
        raise ValueError("duplicate benchmark session")
    summary = json.loads((replay / "summary.json").read_text())
    intent = replay / "intent.json"
    decision_hour = (
        json.loads(intent.read_text()).get("inputs", {}).get("config", {}).get("decision_hour", 16)
        if intent.exists()
        else 16
    )
    ledgers = {}
    for row in summary:
        name = f"{row['model']}-{row['capital_fen']}fen"
        frame = pd.read_parquet(replay / name / "ledger.parquet")
        if not frame.empty:
            ledgers[name] = frame
    if not ledgers:
        raise ValueError("no completed sessions to compare")
    common = set.intersection(*(set(f.session) for f in ledgers.values()))
    if not common:
        raise ValueError("scenarios have no common completed interval")
    # No dropping missing benchmark days: that would erase strategy losses.
    missing = common - set(benchmark.session)
    if missing:
        raise ValueError(f"benchmark missing common sessions:{sorted(missing)[:3]}")
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    exposures = pd.read_parquet(exposures_path) if exposures_path else None
    for name, frame in ledgers.items():
        chosen = frame.loc[frame.session.isin(common)].sort_values("session")
        aligned = chosen.merge(benchmark, on="session", how="left", validate="one_to_one")
        metrics = comparison_metrics(aligned.daily_return, aligned.benchmark_return)
        previous = aligned.equity_fen / (1 + aligned.daily_return)
        costs = (aligned.fees_fen + aligned.slippage_fen) / previous
        row = {
            "scenario": name,
            **metrics,
            "first_session": aligned.session.iloc[0],
            "last_session": aligned.session.iloc[-1],
            "excluded_sessions": len(frame) - len(chosen),
            "mean_one_way_turnover": float(aligned.one_way_turnover.mean()),
            "fees_fen": int(aligned.fees_fen.sum()),
            "slippage_fen": int(aligned.slippage_fen.sum()),
            "same_fills_cost_addback_return": float(np.prod(1 + aligned.daily_return + costs) - 1),
            "cost_addback_is_not_a_frictionless_strategy": True,
            "risk_breach_days": int(aligned.risk_breaches.gt(0).sum()),
        }
        rows.append(row)
        write_frame(output / f"{name}-daily.parquet", aligned)
        if exposures is not None:
            positions = pd.read_parquet(replay / name / "positions.parquet")
            write_frame(
                output / f"{name}-style.parquet",
                portfolio_style(positions, frame, exposures, decision_hour=decision_hour),
            )
    ic = {}
    if training:
        verify_completed(training)
        signal = pd.read_parquet(training / "signal_daily.parquet")
        config = json.loads((training / "intent.json").read_text())["config"]
        for model, group in signal.groupby("model"):
            ic[model] = block_mean_interval(
                group.sort_values("trade_date").rank_ic, config["horizon_sessions"] + 1
            )
    result = {
        "schema": "quantlab_ml_report_v1",
        "scenarios": rows,
        "signal_uncertainty": ic,
        "scenario_statuses": summary,
        "benchmark_sha256": benchmark_hash,
        "replay_completion_sha256": sha256(replay / "completed.json"),
        "exposures_sha256": exposures_hash,
        "training_completion_sha256": sha256(training / "completed.json") if training else None,
        "performance_eligible": False,
    }
    write_json(output / "report.json", result)
    lines = [
        "# QuantLab 同期研究报告",
        "",
        "以下为已声明输入下的情景结果，不代表历史数据已认证或未来收益。",
        "",
        "| 情景 | 净收益 | 基准收益 | 相对净值收益 | 平均单边换手 | 风险偏离天数 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['net_return']:.2%} | {r['benchmark_return']:.2%} | "
            f"{r['relative_wealth_return']:.2%} | {r['mean_one_way_turnover']:.2%} | "
            f"{r['risk_breach_days']} |"
        )
    lines.extend(
        [
            "",
            "成本加回仅解释同一成交路径的成本，不是重新运行的零成本策略。",
            "不同情景只比较共同完成区间；停止原因及区间外天数见 report.json。",
            "容量需结合资金规模、参与率、拒单及部分成交共同判断。",
        ]
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sha256(benchmark_path) != benchmark_hash or (
        exposures_path and sha256(exposures_path) != exposures_hash
    ):
        raise ValueError("report input changed during computation")
    verify_completed(replay)
    if training:
        verify_completed(training)
    complete(output)
    return result
