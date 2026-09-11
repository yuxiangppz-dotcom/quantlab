"""Chinese view of retrospective score coverage and unresolved portfolio evidence."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.signal_feasibility import OUTPUT_DIRECTORY, load_feasibility_report
from quantlab.ui.workbench import public_error

GROUPS = {"lightgbm_baseline": "基础特征模型", "transparent_combo_v1": "现有四因子组合"}


def render_signal_feasibility_review():
    st.subheader("组合检查：研究分数能否用于组合")
    out = PROJECT_ROOT / OUTPUT_DIRECTORY
    if not (out / "report.json").exists():
        st.info("组合输入检查尚无完整报告，暂不能评价可成交收益。")
        return
    try:
        report = load_feasibility_report(out)
        first, second, third = st.columns(3)
        first.metric("每个候选的股票日期记录", f"{report['score_rows_per_candidate']:,}")
        second.metric("覆盖交易日", f"{report['score_dates']:,}")
        third.metric("本轮新增模型训练", report["new_fit_attempts"])
        st.caption("2023-01-03 至 2026-09-10 · 两组信号 × 5／10／20 日 · 复用封存模型")
        st.write(
            "分数现在只按当时的特征是否齐全生成，不再因为未来收益缺失而排除股票。"
            "这些模型实际在 2026 年 9 月训练，历史分数属于事后模拟，不是当年的预测登记。"
        )
        st.warning("这些目标尚不能视为可成交组合。净收益与 20% 回撤目标尚未评估。")
        st.caption(
            "下表按每天各取最多 20 只、总目标仓位 80% 检查输入；每天是独立意向，"
            "不是实际调仓记录。缺价包含截止日之后的日期及区间内缺失，不能据此认定停牌或退市。"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "信号": GROUPS[item["group"]],
                        "预定持有交易日": item["horizon"],
                        "独立目标条数": item["target_rows"],
                        "入场参考价缺失或异常": item["missing_entry_raw_price"],
                        "退出参考价缺失或异常": item["missing_exit_raw_price"],
                    }
                    for item in report["audit"]["target_summary"]
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        with st.expander("还需要补齐什么", expanded=True):
            for item in report["audit"]["blockers"]:
                st.write(f"• {item['detail']}")
            st.info(report["audit"]["next_step"])
        with st.expander("资金规模与费用情景"):
            st.write(
                "预设 5 万、20 万、100 万元三个假设资金规模，不代表你的账户余额。"
                "按 20 只股票和 80% 仓位计算，下表只是单笔名义金额的费用组成示例，"
                "尚未按实际股数取整，也未证明可以买到。"
            )
            examples = [
                item
                for item in report["audit"]["fee_examples"]
                if item["asset_type"] == "stock" and item["trade_date"] == "2023-08-28"
            ]
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "假设总资金（元）": item["hypothetical_capital_cny"],
                            "单笔名义金额（元）": item["hypothetical_allocation_fen"] / 100,
                            "方向": "买入" if item["side"] == "buy" else "卖出",
                            "佣金（元）": item["commission_fen"] / 100,
                            "印花税（元）": item["stamp_duty_fen"] / 100,
                        }
                        for item in examples
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "佣金暂按万分之 0.86、股票最低 5 元，卖方印花税另计。完整费用仍未知；"
                "另预设单边 5／15／30 个基点的滑点压力情景，尚未运行，也不是测得的实际滑点。"
            )
        with st.expander("历史规则覆盖与记录核验"):
            boards = {"MAIN": "主板", "STAR": "科创板", "CHINEXT": "创业板"}
            exchanges = {"SSE": "上交所", "SZSE": "深交所"}
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "市场": exchanges[item["exchange"]],
                            "声明板块": boards[item["declared_board"]],
                            "规则覆盖交易日": item["covered_sessions"],
                            "规则缺失交易日": item["missing_sessions"],
                            "首个缺失日期": item["first_missing"],
                        }
                        for item in report["audit"]["rule_coverage"]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            st.caption("这是现有规则解析器的覆盖，不代表每只股票的历史板块身份已核实。")
            st.caption(
                f"特征不完整记录：{report['excluded_rows']:,}；身份与原因已保留。"
                f"报告指纹：{report['fingerprint']}。本页核对报告，完整文件另经独立校验。"
            )
        st.download_button(
            "下载组合检查完整报告",
            (out / "report.json").read_bytes(),
            "QuantLab_组合输入检查.json",
            "application/json",
        )
        findings = PROJECT_ROOT / "docs/signal_feasibility_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载组合检查中文解读",
                findings.read_bytes(),
                "QuantLab_组合检查解读.md",
                "text/markdown",
            )
    except Exception as exc:
        st.error(f"组合检查报告无法校验：{public_error(exc)}")
