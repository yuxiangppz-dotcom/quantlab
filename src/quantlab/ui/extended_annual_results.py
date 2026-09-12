"""User-facing, independently verified full-year predictive comparison."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.extended_annual_review import comparison_tables, read_verification
from quantlab.research.extended_completion_protocol import OUTPUT
from quantlab.research.round2_dataset import sealed_read


def render_verified_results(report):
    review = read_verification(PROJECT_ROOT, report)
    if review is None:
        st.info("全量独立复核尚未交付，暂不展示研究结论。")
        return
    out = PROJECT_ROOT / OUTPUT / "combined"
    diagnostics = sealed_read(out / "diagnostics.json")
    daily = pd.read_parquet(out / "daily_policies.parquet", use_threads=False)
    whole, paired = comparison_tables(daily, diagnostics)
    st.success("104个模型引用、全部保存预测和全年诊断已独立复核；复核没有增加训练。")
    st.caption(
        "样本范围是既有股票记录中符合完整特征条件的成员；历史全市场覆盖未获证实，不能称为无生存偏差的全市场结果。"
    )
    findings = PROJECT_ROOT / "docs/extended_annual_findings_zh.md"
    if findings.exists():
        text = findings.read_text(encoding="utf-8")
        marker = "## 本轮结论\n\n"
        if marker in text:
            st.markdown(text.split(marker, 1)[1].split("\n## ", 1)[0].strip())
    st.write("全年预测诊断")
    display = whole.replace(
        {
            "ridge": "岭回归",
            "lightgbm": "LightGBM",
            "weekly": "每周重训",
            "monthly": "每月首次周更后复用",
        }
    ).rename(
        columns={
            "model": "模型",
            "policy": "训练频率",
            "signal_days": "信号交易日",
            "rank_ic_days": "可计算RankIC的天数",
            "prediction_rows": "预测成员",
            "evaluation_rows": "可评价标签",
            "mean_rank_ic": "平均RankIC",
            "mean_rank_stability": "相邻交易日排序稳定性",
            "mean_top20_membership_change": "前20候选变化比例",
        }
    )
    st.caption("每组242个信号交易日，1,215,709个预测成员、1,214,290个可评价标签。")
    st.dataframe(
        display[
            [
                "模型",
                "训练频率",
                "可计算RankIC的天数",
                "平均RankIC",
                "相邻交易日排序稳定性",
                "前20候选变化比例",
            ]
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "RankIC衡量当日预测排序与之后五个交易日收益排序的相关性，不是胜率或组合收益。"
        "前20候选变化比例没有计算实际持仓、成交或费用，不能当作真实换手率。"
    )
    st.write("剔除54个共享模型交易日后的训练频率对照")
    paired_display = paired.replace({"ridge": "岭回归", "lightgbm": "LightGBM"}).rename(
        columns={
            "model": "模型",
            "nonshared_signal_days": "非共享交易日",
            "paired_rank_ic_days": "可配对天数",
            "weekly_mean_rank_ic_on_paired_days": "配对日周更平均RankIC",
            "monthly_mean_rank_ic_on_paired_days": "配对日低频平均RankIC",
            "mean_daily_rank_ic_difference": "按日平均差值",
            "mean_weekly_rank_ic_difference": "按周平均差值",
            "positive_difference_weeks": "周更较高周数",
            "negative_difference_weeks": "周更较低周数",
            "tied_difference_weeks": "相同周数",
            "undefined_difference_weeks": "无法计算周数",
        }
    )
    compact = paired_display[
        ["模型", "可配对天数", "按日平均差值", "按周平均差值", "周更较高周数", "周更较低周数"]
    ].copy()
    compact["相同/未知周数"] = (
        paired_display["相同周数"].astype(str) + "/" + paired_display["无法计算周数"].astype(str)
    )
    st.dataframe(compact, hide_index=True, width="stretch")
    st.caption(
        "差值为周更减去低频。按日平均与按周平均分别对交易日和周次等权，短假期周的权重不同。"
        "各周标签可能重叠，这些计数没有独立样本显著性含义；完整历史仍是已观察区间。"
    )
    months = pd.DataFrame(diagnostics["monthly"])
    months["series"] = (
        months.model.map({"ridge": "岭回归", "lightgbm": "LightGBM"})
        + " / "
        + months.policy.map({"weekly": "周更", "monthly": "低频复用"})
    )
    st.write("全部12个自然月的平均RankIC")
    st.line_chart(
        months.pivot(index="month", columns="series", values="mean_rank_ic"), y_label="平均RankIC"
    )
    st.caption(
        "月份按实际信号日期汇总，包括跨月周；每月首次周更后的共享日期仍保留在这张完整月度图中。缺失值保持空白。"
    )
    for label, data, filename, mime in (
        (
            "下载全年预测摘要",
            display.to_csv(index=False).encode("utf-8-sig"),
            "QuantLab_全年预测摘要.csv",
            "text/csv",
        ),
        (
            "下载全年频率差异摘要",
            paired_display.to_csv(index=False).encode("utf-8-sig"),
            "QuantLab_全年频率差异摘要.csv",
            "text/csv",
        ),
        (
            "下载全年独立复核",
            json.dumps(review, ensure_ascii=False, indent=2),
            "QuantLab_全年独立复核.json",
            "application/json",
        ),
    ):
        st.download_button(label, data, filename, mime, on_click="ignore")
    if findings.exists():
        st.download_button(
            "下载全年中文解读",
            findings.read_text(encoding="utf-8"),
            "QuantLab_全年中文解读.txt",
            "text/plain",
            on_click="ignore",
        )
