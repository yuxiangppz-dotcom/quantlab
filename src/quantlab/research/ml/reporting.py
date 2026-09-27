"""Common-period benchmarks, actual holdings, costs and dependent-sample IC checks."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

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


def build_report(replay, benchmark_path, output, **kwargs):
    """Publish a complete report atomically; a failed diagnostic leaves no final directory."""
    from quantlab.research.alpha158_store import exclusive_job

    output = research_output(Path(output))
    with exclusive_job(output.parent):
        if output.exists():
            raise FileExistsError(output)
        with TemporaryDirectory(dir=output.parent) as temporary:
            stage = Path(temporary) / "report"
            result = _build_report(replay, benchmark_path, stage, **kwargs)
            stage.rename(output)
    return result


def _build_report(
    replay,
    benchmark_path,
    output,
    *,
    training=None,
    exposures_path=None,
    baseline_replay=None,
    factor_replay=None,
    bundle=None,
    study=None,
):
    research_output(output)
    verify_completed(replay)
    benchmark_hash = sha256(benchmark_path)
    exposures_hash = sha256(exposures_path) if exposures_path else None
    metadata_path = benchmark_path.with_name("benchmark_metadata.json")
    benchmark_metadata = (
        json.loads(metadata_path.read_text())
        if metadata_path.exists()
        else {
            "return_basis": "unspecified",
            "comparable_to_net_dividend_portfolio": False,
        }
    )
    metadata_hash = sha256(metadata_path) if metadata_path.exists() else None
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
    summary_by_name = {
        f"{row['model']}-{row['capital_fen']}fen": row for row in summary
    }
    # Each scenario reports its OWN completed path; the benchmark is matched
    # to that window. No dropping missing benchmark days: that would erase
    # strategy losses.
    for name, frame in ledgers.items():
        missing = set(frame.session) - set(benchmark.session)
        if missing:
            raise ValueError(f"benchmark missing {name} sessions:{sorted(missing)[:3]}")
    baseline_comparison = None
    if baseline_replay:
        from quantlab.research.ml.baselines import compare_pool_baseline

        baseline_comparison = compare_pool_baseline(replay, baseline_replay)
    factor_comparison = None
    if factor_replay:
        from quantlab.research.ml.baselines import compare_factor_baseline

        factor_comparison = compare_factor_baseline(replay, factor_replay)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    exposures = pd.read_parquet(exposures_path) if exposures_path else None
    for name, frame in ledgers.items():
        chosen = frame.sort_values("session")
        aligned = chosen.merge(benchmark, on="session", how="left", validate="one_to_one")
        metrics = comparison_metrics(aligned.daily_return, aligned.benchmark_return)
        gross_budget = (
            json.loads(intent.read_text())
            .get("inputs", {}).get("config", {})
            .get("gross_exposure", 1)
            if intent.exists()
            else 1
        )
        cash_reference = comparison_metrics(
            aligned.daily_return, gross_budget * aligned.benchmark_return
        )
        previous = aligned.equity_fen / (1 + aligned.daily_return)
        costs = (aligned.fees_fen + aligned.slippage_fen) / previous
        status = summary_by_name.get(name, {})
        mean_gross = (
            float(aligned.gross_exposure.mean()) if "gross_exposure" in aligned else None
        )
        if mean_gross is None:
            position_class = "unknown"
        elif mean_gross <= 1e-9:
            position_class = "pure_cash"
        elif mean_gross < 0.5 * gross_budget:
            position_class = "low_position"
        else:
            position_class = "invested"
        row = {
            "scenario": name,
            **metrics,
            "sessions": len(chosen),
            "excluded_sessions": 0,
            "stop_reason": status.get("stop_reason"),
            "valid_through": status.get("valid_through"),
            "position_class": position_class,
            "mean_gross_exposure": mean_gross,
            "cash_budget_relative_wealth_return": cash_reference["relative_wealth_return"],
            "cash_budget_reference": {
                "risky_fraction": gross_budget,
                "cash_return": 0,
                "daily_rebalanced_cost_free_diagnostic": True,
            },
            "first_session": aligned.session.iloc[0],
            "last_session": aligned.session.iloc[-1],
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
            style = portfolio_style(positions, frame, exposures, decision_hour=decision_hour)
            coverage_columns = [c for c in style if c.endswith("_covered_nav_weight")]
            gross = (
                frame.set_index("session").gross_exposure
                if "gross_exposure" in frame
                else pd.Series(dtype=float)
            )
            row["style_unknown_days"] = (
                sum(
                    any(float(r[c]) + 1e-6 < gross.get(r["session"], 1) for c in coverage_columns)
                    for r in style.to_dict("records")
                )
                if coverage_columns
                else len(frame)
            )
            write_frame(
                output / f"{name}-style.parquet",
                style,
            )
        yearly = []
        for year, part in aligned.groupby(pd.to_datetime(aligned.session).dt.year):
            yearly.append(
                {"year": int(year), **comparison_metrics(part.daily_return, part.benchmark_return)}
            )
        write_json(output / f"{name}-yearly.json", yearly)
    # Common-prefix diagnostics are SECONDARY: they compare scenarios only
    # over the intersection of their completed paths and never replace a
    # preregistered full-window comparison. Truncated scenarios keep their
    # failed suffix visible in their own rows above.
    complete_scenarios = [
        name
        for name in ledgers
        if summary_by_name.get(name, {}).get("stop_reason") is None
    ]
    common_prefix = None
    if len(complete_scenarios) >= 2:
        common = set.intersection(
            *(set(ledgers[name].session) for name in complete_scenarios)
        )
        if common:
            common_prefix = {
                "sessions": len(common),
                "note": (
                    "intersection of complete scenarios only; a preregistered "
                    "full-window comparison requires every scenario to finish"
                ),
                "scenarios": {},
            }
            for name in complete_scenarios:
                chosen = ledgers[name].loc[ledgers[name].session.isin(common)].sort_values(
                    "session"
                )
                aligned = chosen.merge(
                    benchmark, on="session", how="left", validate="one_to_one"
                )
                common_prefix["scenarios"][name] = {
                    **comparison_metrics(aligned.daily_return, aligned.benchmark_return),
                    "mean_gross_exposure": float(aligned.gross_exposure.mean())
                    if "gross_exposure" in aligned
                    else None,
                }
    ic = {}
    if training:
        verify_completed(training)
        signal = pd.read_parquet(training / "signal_daily.parquet")
        config = json.loads((training / "intent.json").read_text())["config"]
        for model, group in signal.groupby("model"):
            ic[model] = block_mean_interval(
                group.sort_values("trade_date").rank_ic, config["horizon_sessions"] + 1
            )
    selection = None
    if study:
        if training is None:
            raise ValueError("study diagnostics require training")
        from quantlab.research.ml.selection import selection_diagnostics

        selection = selection_diagnostics(study, training, replay)
    decay = None
    bundle_manifest = None
    if bundle:
        if training is None:
            raise ValueError("signal decay requires training")
        from quantlab.research.ml.config import MLConfig
        from quantlab.research.ml.diagnostics import signal_decay
        from quantlab.research.ml.io import verify_bundle
        from quantlab.research.ml.panel import read_range

        bundle_manifest = verify_bundle(bundle)
        run_intent = json.loads((training / "intent.json").read_text())
        if run_intent["inputs"] != bundle_manifest:
            raise ValueError("diagnostic bundle differs from trained inputs")
        diagnostic_config = MLConfig(**run_intent["config"])
        sessions = pd.DatetimeIndex(json.loads((bundle / "calendar.json").read_text()))
        scores = pd.read_parquet(training / "scores.parquet")
        start = pd.Timestamp(scores.trade_date.min())
        # Diagnostic horizons must not consume outcomes beyond the registered evaluation.
        evaluation_end = sessions[sessions <= pd.Timestamp(run_intent["end"])][-1]
        prices = read_range(
            bundle / "prices.parquet",
            start,
            evaluation_end,
            columns=["trade_date", "instrument_id", "adj_close"],
            instruments=scores.instrument_id.unique(),
            max_bytes=diagnostic_config.max_matrix_bytes,
        )
        decay_daily, persistence, decay = signal_decay(
            scores, prices, sessions, diagnostic_config, evaluation_end=evaluation_end
        )
        write_frame(output / "signal-decay.parquet", decay_daily)
        write_frame(output / "signal-persistence.parquet", persistence)
        if verify_bundle(bundle) != bundle_manifest:
            raise ValueError("diagnostic bundle changed during analysis")
    result = {
        "schema": "quantlab_ml_report_v1",
        "same_pool_baseline": baseline_comparison,
        "factor_baseline": factor_comparison,
        "baseline_completion_sha256": sha256(baseline_replay / "completed.json")
        if baseline_replay
        else None,
        "selection_diagnostics": selection,
        "signal_decay": decay,
        "diagnostic_bundle_manifest": bundle_manifest,
        "benchmark_metadata": benchmark_metadata,
        "benchmark_metadata_sha256": metadata_hash,
        "dividend_comparability_verified": False,
        "scenarios": rows,
        "common_prefix_diagnostics": common_prefix,
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
        "每个情景使用自身完整完成路径；基准与该区间匹配。",
        "",
        "| 情景 | 状态 | 交易日数 | 区间 | 仓位 | 净收益 | 基准收益 | "
        "相对净值收益 | 平均单边换手 | 风险偏离 |",
        "|---|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        window = f"{r['first_session']}~{r['last_session']}"
        lines.append(
            f"| {r['scenario']} | {r.get('stop_reason') or 'completed'} | {r.get('sessions')} "
            f"| {window} | {r.get('position_class')} | {r['net_return']:.2%} "
            f"| {r['benchmark_return']:.2%} | {r['relative_wealth_return']:.2%} "
            f"| {r['mean_one_way_turnover']:.2%} | {r['risk_breach_days']} |"
        )
    lines.extend(
        [
            "",
            "成本加回仅解释同一成交路径的成本，不是重新运行的零成本策略。",
            "停止情景保留部分区间结果并单独列出停止原因；共同前缀诊断见",
            "common_prefix_diagnostics（仅完整情景交集，非预登记全区间比较）。",
            "纯现金/低仓位情景不能作为选股 alpha 证据。",
            "容量需结合资金规模、参与率、拒单及部分成交共同判断。",
            "指数收益口径见 benchmark_metadata；价格指数不含股息再投资，",
            "与含股息组合之差不能直接称为 alpha。",
            "同池等权比较见 same_pool_baseline；手数、最小交易额可能使小资金大量闲置。",
            "研究选择统计见 selection_diagnostics；DSR 假设不等于已验证的独立试验数。",
            "多期限 IC：signal-decay.parquet；"
            "分数持续性：signal-persistence.parquet（若提供 bundle）。",
        ]
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sha256(benchmark_path) != benchmark_hash or (
        exposures_path and sha256(exposures_path) != exposures_hash
    ):
        raise ValueError("report input changed during computation")
    if metadata_hash and sha256(metadata_path) != metadata_hash:
        raise ValueError("benchmark metadata changed during report")
    verify_completed(replay)
    if training:
        verify_completed(training)
    if baseline_replay:
        verify_completed(baseline_replay)
    if factor_replay:
        verify_completed(factor_replay)
    complete(output)
    return result
