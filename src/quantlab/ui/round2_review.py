"""Chinese read-only view of the pinned second-round diagnostic artifacts."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.round2_diagnostics import ROUND_DIRECTORY, load_report
from quantlab.ui.workbench import public_error

SIGNALS = {
    "momentum_20d": "20 日动量",
    "reversal_20d": "20 日反转",
    "wick_balance": "上下影线差",
    "macd_hist_normalized": "MACD 柱值",
    "transparent_combo_v1": "现有四因子组合",
    "lightgbm_baseline": "基础特征模型",
    "lightgbm_augmented": "加入影线、MACD、指数的模型",
}
PERIODS = {
    "discovery": "训练期 2020–2022",
    "validation": "验证期 2023–2024",
    "test_observed": "已观察回顾期 2025–2026-09-10",
}


def comparison_rows(report, horizon):
    rows = []
    for candidate in report["comparisons"]:
        if candidate["horizon"] != horizon:
            continue
        row = {
            "信号": SIGNALS[candidate["group"]],
            "状态": "完成" if candidate["status"] == "complete" else "失败（已保留）",
        }
        for period, title in (
            ("validation", "验证期"),
            ("test_observed", "回顾期"),
            ("discovery", "训练期"),
        ):
            summary = candidate["periods"].get(period, {}).get("summary", {})
            row[f"{title} 平均 IC"] = summary.get("mean_rank_ic")
        rows.append(row)
    return rows


def render_round2_review():
    st.subheader("第二轮：因子与模型研究结果")
    out = PROJECT_ROOT / ROUND_DIRECTORY
    path = out / "results/report.json"
    if not path.exists():
        st.info("第二轮尚无完整诊断报告。完成后会在这里显示全部 21 项比较，包括失败项。")
        return
    try:
        report = load_report(out)
        st.caption(
            "数据截至 2026-09-10 · 7 组信号 × 3 个预测区间 · "
            f"实际模型训练尝试 {report['fit_attempts']} 次"
        )
        st.warning(
            "这里衡量股票排序与未来收益的相关性，不是可成交净收益。"
            "尚未验证 20% 最大回撤目标，也不会自动替换日报策略。"
        )
        st.write(
            "平均 IC 大于 0 表示高分股票的未来收益排序通常更靠前；它不是收益率，"
            "也不能单独证明能赚钱。训练期成绩受拟合影响，优先对照验证期和年度稳定性。"
        )
        horizon = st.selectbox("查看预测区间（交易日）", [5, 10, 20], key="round2_horizon")
        st.caption("预测区间不等于实际持仓天数；各组使用相同的完整特征及有效标签样本。")
        st.caption("验证期：2023–2024；回顾期：2025–2026-09-10；训练期：2020–2022。")
        st.dataframe(
            pd.DataFrame(comparison_rows(report, horizon)), hide_index=True, width="stretch"
        )
        group = st.selectbox(
            "查看年度稳定性", list(SIGNALS), format_func=SIGNALS.get, key="round2_group"
        )
        candidate = next(
            item
            for item in report["comparisons"]
            if item["group"] == group and item["horizon"] == horizon
        )
        annual = [
            {
                "阶段": PERIODS[period],
                "年度": item["year"],
                "平均 IC": item["mean_rank_ic"],
                "有效日": item["valid_days"],
                "IC 为正的日比例": item["positive_ratio"],
            }
            for period in PERIODS
            for item in candidate["periods"].get(period, {}).get("annual", [])
        ]
        if annual:
            st.dataframe(pd.DataFrame(annual), hide_index=True, width="stretch")
        else:
            st.error("该候选未完成，失败记录保留在完整报告中。")
        with st.expander("样本覆盖与研究限制"):
            coverage = report["feature_coverage"]
            st.write(
                f"历史范围内股票观察 {coverage['feature_rows']:,} 行；"
                f"特征齐全 {coverage['common_feature_rows']:,} 行，之后还需剔除跨期或缺失标签。"
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "阶段": PERIODS[period],
                            "原始信号行": item["signal_rows"],
                            "诊断样本行": item["diagnostic_rows"],
                            "跨期标签排除": item["excluded_crossing_label_end"],
                            "未知标签终点": item["excluded_unknown_label_end"],
                            "缺失标签排除": item["excluded_missing_or_nonfinite_label"],
                            "特征不全排除": item["excluded_incomplete_features"],
                        }
                        for period, item in report["coverage"][str(horizon)].items()
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            st.write(
                "科创 50 发布前不生成相关信号；异常或缺失市值保持缺失。"
                "未来标签缺失的证券不能进入相关性统计，因此样本覆盖限制必须一起阅读。"
                "历史数据为事后获取，历史修订及完整可交易性仍未核验。"
            )
            st.write("2025 年以后的历史此前已被观察过；重叠预测区间不采用独立样本显著性检验。")
            st.caption(f"报告指纹：{report['fingerprint']} · 代码版本：{report['code_head'][:12]}")
            st.caption("本页校验报告与数据清单身份；全部模型和中间文件另由完整校验核对。")
        st.download_button(
            "下载第二轮完整诊断报告",
            path.read_bytes(),
            "QuantLab_第二轮诊断.json",
            "application/json",
        )
        findings = PROJECT_ROOT / "docs/research_round2_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载第二轮中文解读",
                findings.read_bytes(),
                "QuantLab_第二轮中文解读.md",
                "text/markdown",
            )
        st.info(
            "日常使用仍从“开始使用”更新数据、生成日报、登记当日前瞻；研究结果不自动生成买卖指令。"
        )
    except Exception as exc:
        st.error(f"第二轮研究结果无法校验：{public_error(exc)}")
