"""Render the verified two-variant model replay and matched SSE price benchmark."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from quantlab.data.index_context import load_index_context
from quantlab.research.model_replay_intake import nav_metrics


def report(base, exst, root, output):
    output.mkdir(parents=True, exist_ok=False)
    names = {"original": "原始模型组合", "ex_st": "剔除ST后选前20"}
    results, frames, curves, costs = {}, {}, {}, {}
    for key, directory in (("original", base), ("ex_st", exst)):
        result = json.loads((directory / "result.json").read_text())
        proof = json.loads((directory / "independent_verification.json").read_text())
        if result["status"] != "completed_scenario" or proof["all_ok"] is not True:
            raise ValueError("only complete independently reconciled runs can be reported")
        frame = pd.read_csv(directory / "ledger.csv", parse_dates=["date"]).set_index("date")
        results[key], frames[key] = result, frame
        curves[key] = frame.equity_fen / 20000000
        attempts = pd.read_csv(directory / "attempts.csv")
        costs[key] = {
            "commission_cny": frame.commission_fen.sum() / 100,
            "stamp_cny": frame.stamp_fen.sum() / 100,
            "gross_turnover_cny": frame.turnover_fen.sum() / 100,
            "executed_attempts": int((attempts.quantity > 0).sum()),
            "blocked_attempts": int((attempts.quantity == 0).sum()),
            "block_reasons": attempts.loc[attempts.quantity == 0].reason.value_counts().to_dict(),
            "mean_gross_exposure": float(frame.gross_exposure.mean()),
        }
        last = json.loads(
            (directory / "days" / (str(frame.index[-1].date()) + ".json")).read_text()
        )
        costs[key]["cash_dividends_received_cny"] = (
            sum(c["gross_fen"] for c in last["claims"] if c["paid"]) / 100
        )
        costs[key]["dividend_tax_withheld_cny"] = (
            sum(c["withheld_fen"] for c in last["claims"]) / 100
        )
        fractions = []
        for dayfile in sorted((directory / "days").glob("*.json")):
            day = json.loads(dayfile.read_text())
            fractions.extend(
                {"date": day["record"]["date"], **event}
                for event in day["corporate"]
                if event["kind"] == "fractional_shares_not_counted"
            )
        for item in fractions:
            stamp = pd.Timestamp(item["date"])
            path = (root / "data/canonical/daily" / f"year={stamp.year}"
                    / f"month={stamp.month:02d}" / f"{stamp.date()}.parquet")
            bars = pd.read_parquet(path)
            matched = bars.loc[bars.instrument_id == item["event"].split(":")[0], "close"]
            if len(matched) != 1:
                raise ValueError("fractional-share valuation needs one raw close")
            item["listing_day_close_cny"] = float(matched.iloc[0])
            item["excluded_listing_day_value_cny"] = (
                float(item["quantity"]) * item["listing_day_close_cny"]
            )
        costs[key]["excluded_fractional_shares"] = fractions
        costs[key]["excluded_fractional_listing_value_cny"] = sum(
            x["excluded_listing_day_value_cny"] for x in fractions
        )
    if not curves["original"].index.equals(curves["ex_st"].index):
        raise ValueError("variant calendars differ")
    snapshot = (
        root
        / "data/canonical/research_index_context_v1"
        / "162764d675283a4eb8d42e1f621ea262fde9054b37a14bb6f4383a135bc623f2.json"
    )
    index = load_index_context(
        snapshot,
        expected_fingerprint="5fb766a57484af176254a8352d6ea54f14bcffb009a0854a379d5ff460497829",
    )
    series = next(x for x in index["series"] if x["metadata"]["ts_code"] == "000001.SH")
    bench = pd.DataFrame(series["rows"])
    bench["trade_date"] = pd.to_datetime(bench.trade_date)
    bench = bench.set_index("trade_date").close.reindex(curves["original"].index)
    if bench.isna().any():
        raise ValueError("benchmark calendar coverage gap")
    curves["sse"] = bench / bench.iloc[0]
    names["sse"] = "上证指数（价格指数）"
    all_metrics = {key: nav_metrics(curve) for key, curve in curves.items()}
    annual = []
    for year in sorted(set(bench.index.year)):
        dates = bench.index[bench.index.year == year]
        begin = bench.index[max(0, bench.index.get_loc(dates[0]) - 1)]
        annual.append(
            {
                "year": year,
                **{
                    key: float(curve.loc[dates[-1]] / curve.loc[begin] - 1)
                    for key, curve in curves.items()
                },
            }
        )
    pd.DataFrame(curves).to_csv(output / "nav_comparison.csv")
    payload = {
        "metrics": all_metrics,
        "annual_returns": annual,
        "costs": costs,
        "benchmark_fingerprint": index["fingerprint"],
        "source_runs": {"original": str(base), "ex_st": str(exst)},
        "report_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "claim": "retrospective_simulated_portfolio_not_actual_broker_performance",
    }
    (output / "comparison.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    colors = {"original": "#b04444", "ex_st": "#2667a8", "sse": "#64834b"}
    labels = {"original": "Original Top-20", "ex_st": "Exclude ST, Top-20", "sse": "SSE Composite"}
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, layout="constrained")
    for key, curve in curves.items():
        axes[0].plot(curve.index, curve, color=colors[key], label=labels[key], linewidth=1.5)
        axes[1].plot(
            curve.index, (curve / curve.cummax() - 1) * 100, color=colors[key], linewidth=1.3
        )
    axes[0].set(
        title="Alpha158 rolling LightGBM | CNY 200,000 | next-session execution",
        ylabel="NAV (start = 1)",
    )
    axes[1].set(ylabel="Drawdown (%)")
    axes[0].legend(loc="upper left")
    for ax in axes:
        ax.grid(alpha=0.18)
        ax.spines[["right", "top"]].set_visible(False)
    fig.savefig(output / "nav_drawdown.png", dpi=160)
    plt.close(fig)
    rows = []
    for key, m in all_metrics.items():
        rows.append(
            f"| {names[key]} | {m['total_return']:.2%} | {m['cagr_252']:.2%} | "
            f"{m['max_drawdown']:.2%} | {m['sharpe_rf0_252']:.3f} | "
            f"{200000 * (1 + m['total_return']):,.2f} |"
        )
    year_rows = [
        f"| {r['year']} | {r['original']:.2%} | {r['ex_st']:.2%} | {r['sse']:.2%} |" for r in annual
    ]
    cost_rows = [
        f"| {names[k]} | {c['commission_cny']:,.2f} | {c['stamp_cny']:,.2f} | "
        f"{c['executed_attempts']} | {c['mean_gross_exposure']:.1%} |"
        for k, c in costs.items()
    ]
    text = (
        """# Alpha158 滚动 LightGBM：含费回放结果

区间：2023-01-03 至 2026-09-10，895 个估值日；本金 200,000 元。

以下为历史数据上的模拟组合结果，不是实盘账户收益。模型本身没有独立的收益率；这些数字属于“保存预测＋每日前20名＋次日执行”的组合方案。

| 方案 | 累计收益 | 年化收益（252日） | 最大回撤 | 夏普（无风险利率0） | 期末参考权益（元） |
|---|---:|---:|---:|---:|---:|
"""
        + "\n".join(rows)
        + """

## 分年收益

2023年从1月3日收盘基准起算，2026年截至9月10日，均不冒充完整自然年度。

| 年份 | 原始组合 | 剔除ST | 上证指数 |
|---|---:|---:|---:|
"""
        + "\n".join(year_rows)
        + """

## 费用与执行

| 方案 | 佣金累计（元） | 印花税累计（元） | 模拟成交笔数 | 平均实际股票仓位 |
|---|---:|---:|---:|---:|
"""
        + "\n".join(cost_rows)
        + """

固定股票全佣万0.86、单笔最低5元，印花税按期间另计；5bp单边不利滑点并按分向不利方向取整，滑点已进入成交价，不能再从净值重复扣除。全佣情景不再重复加过户等附加费。这是用户申报的模拟费率，并非历史券商账单。

按信号日净值与原始收盘价生成次日股数；卖出优先，买入按排名，现金不足、整数股数与整手限制会使实际仓位低于100%。执行沿用数量内核的T+1、涨跌停、当日价区间及5%容量约束；无可行的滑点价格就不成交，不强行填单。每日调仓会产生很高的换手和最低佣金负担。

## 两组的唯一区别与估值约定

- 原始组保留全部有限预测；
对照组在每个信号日按该日ST名单先过滤再选20名。
模型训练和预测均不重做。
持仓随后变为ST时，仍可能因跌停或停牌卖不出。

- 停牌不成交，持仓按最后可核实原始价估值，明确保留观察日期；实际清算价值可能不同。
- 按用户确认，退市日起持仓零估值，股份和未知回收权利保留，不虚构卖出或回款。
- 分红按实际登记日持仓确认，除息日计应收、派息日入现金；
FIFO卖出计算红利税，未结税计准备。
送转只按核实的普通股东权益增加股份，并遵守上市日。

- 不足1股的送转按账户合计向下取整，明细见 comparison.json；这是一项保守分配假设。
- 使用3个保存的年度滚动模型，2026年沿用fold3；
这次没有实施每周重训，也没有优化风险层或调仓参数。
历史区间已被研究使用，不能称为全新的样本外检验。

- 依赖现有供应商历史快照与特定公告补证；
分红事件历史完整性、停牌估值及日线成交近似均不是实盘保证。
上证指数是价格指数，不含再投资分红，也不是扣除基金费率后的产品收益。


## 核对与复现

两组均须完整运行且通过独立逐日现金、股份、费用和净值核对后，本报告才生成。原始每日账本、订单尝试、公司行为和源文件指纹保留在各run目录。未下载财务数据、未训练模型、未下实盘订单、未开启自动任务。

运行入口：scripts/replay_alpha158_model.py；校验入口：scripts/verify_model_replay.py；报告入口：scripts/report_model_replay.py。首次运行须新输出目录，旧结果不会覆盖。具体目录与数值见comparison.json。
"""
    )
    fraction_lines = []
    for key, cost in costs.items():
        for item in cost["excluded_fractional_shares"]:
            fraction_lines.append(
                f"{names[key]}：{item['date']}，{item['event'].split(':')[0]}，"
                f"舍去 {item['quantity']} 股；当日原始收盘价 "
                f"{item['listing_day_close_cny']:.2f} 元，对应 "
                f"{item['excluded_listing_day_value_cny']:.2f} 元。"
            )
    text += "\n## 零碎股份影响\n\n" + "\n\n".join(fraction_lines)
    text += "\n\n上述为分配当日估值影响，不代表持有至期末的反事实收益。\n"
    (output / "report_zh.md").write_text(text)
    image = base64.b64encode((output / "nav_drawdown.png").read_bytes()).decode()
    rendered, table_open = [], False
    for line in text.splitlines():
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(set(cell) <= {"-", ":"} for cell in cells):
                continue
            tag = "td" if table_open else "th"
            if not table_open:
                rendered.append("<table>")
                table_open = True
            rendered.append(
                "<tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in cells) + "</tr>"
            )
            continue
        if table_open:
            rendered.append("</table>")
            table_open = False
        if line.startswith("# "):
            rendered.append("<h1>" + html.escape(line[2:]) + "</h1>")
        elif line.startswith("## "):
            rendered.append("<h2>" + html.escape(line[3:]) + "</h2>")
        elif line:
            rendered.append("<p>" + html.escape(line) + "</p>")
    if table_open:
        rendered.append("</table>")
    page = (
        "<!doctype html><meta charset='utf-8'><title>Alpha158 回放对比</title>"
        "<style>body{max-width:1120px;margin:40px auto;padding:0 24px;"
        "font:16px/1.7 sans-serif;color:#172b3a}img{width:100%}"
        "table{border-collapse:collapse;width:100%;margin:24px 0}"
        "td,th{padding:10px;border-bottom:1px solid #ddd;text-align:right}"
        "td:first-child,th:first-child{text-align:left}th{background:#edf3f8}</style>"
        "<img alt='净值与回撤对比' src='data:image/png;base64," + image + "'>" + "".join(rendered)
    )
    (output / "report.html").write_text(page)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--ex-st", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report(args.original, args.ex_st, args.source_root, args.output)
