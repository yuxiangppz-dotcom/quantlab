"""Show the complete future replay schedule without offering a training action."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.extended_plan_review import read_verification
from quantlab.research.extended_plan_run import OUTPUT, read_report
from quantlab.research.round2_dataset import sealed_read
from quantlab.ui.workbench import public_error


def render_extended_plan():
    st.subheader("全年训练频率对照：事先固定的规划")
    try:
        report = read_report(PROJECT_ROOT)
        if report is None:
            st.info("尚无可核验的全年规划报告。")
            return
        out = PROJECT_ROOT / OUTPUT
        weekly = sealed_read(out / "schedule.json")
        verification = read_verification(PROJECT_ROOT, report, weekly)
        a, b, c = st.columns(3)
        a.metric("已有模型可复用", "6 个")
        b.metric("后续新增训练上限", f"{report['future_max_new_fit_attempts']} 次")
        c.metric("本次规划实际训练", "0 次")
        st.write(
            f"固定2025年9月1日至2026年8月31日，共{weekly['signal_market_days']}个市场交易日、"
            f"{report['weekly_rows']}个有交易的周次。比较每周更新与每月首次周更时更新，"
            "后者在当月剩余周次复用同一个模型。"
        )
        st.warning(
            "下方展示执行前固定的规划，当前执行进度见上方。六个旧模型已消耗的名额保持封闭；"
            "后续全部新尝试共用一个累计预算，失败也计数，每段最多六次。"
            "更长区间仍是已观察历史，不构成新前瞻，也不保证提高收益。"
        )
        table = pd.read_csv(out / "weeks.csv").rename(
            columns={
                "week": "序号",
                "week_id": "周标识",
                "month": "归属月份",
                "monthly_anchor": "低频复用周",
                "train_start": "训练起点",
                "train_end": "训练截止",
                "prediction_start": "预测起点",
                "prediction_end": "预测终点",
                "train_rows": "可训练样本",
                "prediction_rows": "预测成员",
                "evaluation_rows": "可评价标签",
                "partial_calendar_week": "区间截断周",
            }
        )
        display = table[
            [
                "周标识",
                "低频复用周",
                "训练截止",
                "预测起点",
                "预测终点",
                "可训练样本",
                "预测成员",
                "可评价标签",
                "区间截断周",
            ]
        ]
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption(
            "包含假期周与最后仅一个信号交易日的周次，预测成员不会因未来标签缺失而删除。完整训练起点及标签缺口见下载。"
        )
        anchors = pd.DataFrame(
            [
                {
                    "月份": row["month"],
                    "复用候选周": row["week_id"],
                    "训练截止": row["train_end"],
                    "开始使用": row["prediction_start"],
                }
                for row in weekly["weeks"]
                if row["week_id"] == row["monthly_anchor"]
            ]
        )
        with st.expander("查看12个月的低频模型切换"):
            st.dataframe(anchors, hide_index=True, width="stretch")
        st.write(
            "组合评价已固定最多20只、80%目标仓位、5/10/20交易日调仓间隔，以及假设资金5万/20万/100万元。"
        )
        st.caption(
            "现有引擎与后续整股、逐单最低费用情景分开核验；未知费用、分红权益和可成交性保持未知。规划不计算净收益或验证20%回撤目标。"
        )
        for label, value, name, mime in (
            (
                "下载全年对照规划报告",
                json.dumps(report, ensure_ascii=False, indent=2),
                "规划报告.json",
                "application/json",
            ),
            (
                "下载全年完整周次",
                table.to_csv(index=False).encode("utf-8-sig"),
                "完整周次.csv",
                "text/csv",
            ),
            (
                "下载全年模型预算与映射",
                json.dumps(weekly, ensure_ascii=False, indent=2),
                "模型预算.json",
                "application/json",
            ),
            (
                "下载每月模型切换表",
                anchors.to_csv(index=False).encode("utf-8-sig"),
                "每月切换.csv",
                "text/csv",
            ),
        ):
            st.download_button(label, value, f"QuantLab_全年{name}", mime, on_click="ignore")
        if verification is not None:
            st.success(
                "规划阶段已独立复核全部周次样本、日历和六个旧模型复用条件；该规划阶段新增拟合与新增预测均为零。"
            )
            st.download_button(
                "下载全年规划独立复核",
                json.dumps(verification, ensure_ascii=False, indent=2),
                "QuantLab_全年规划独立复核.json",
                "application/json",
                on_click="ignore",
            )
        for label, name in (
            ("下载最小组合成本评价规格", "extended_economic_evaluation_spec_zh.md"),
            ("下载全年规划中文解读", "extended_frequency_plan_findings_zh.md"),
        ):
            path = PROJECT_ROOT / "docs" / name
            if path.exists():
                st.download_button(
                    label,
                    path.read_text(encoding="utf-8"),
                    name.replace(".md", ".txt"),
                    "text/plain",
                    on_click="ignore",
                )
    except Exception as exc:
        st.error(f"全年规划证据无法校验：{public_error(exc)}")
