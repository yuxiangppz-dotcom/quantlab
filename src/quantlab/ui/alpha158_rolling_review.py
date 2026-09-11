"""Read-only Chinese view of the fixed six-fit rolling experiment."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.alpha158_rolling import progress_report
from quantlab.research.alpha158_rolling_protocol import OUTPUT
from quantlab.ui.workbench import public_error


def render_alpha158_rolling():
    st.subheader("Alpha158 滚动模型：六次固定比较")
    st.write(
        "使用全部 158 项原生因子，分别用 2020–2022、2020–2023、2020–2024 训练，"
        "评价下一年；2026 年至 9 月 10 日复用最后一折模型，不额外训练。"
        "这些历史已经被观察，结果属于回顾诊断。"
    )
    out = PROJECT_ROOT / OUTPUT
    try:
        report = progress_report(out)
        if report is None:
            st.info("固定方案已准备，尚未开始真实训练。最多六次，失败和中断也计入。")
            return
        a, b = st.columns(2)
        a.metric("已消耗训练次数 / 上限", f"{report['cumulative_fit_attempts']} / 6")
        b.metric("完成训练及诊断", report["completed_fits"])
        status_names = {
            "completed": "完成",
            "failed": "失败（不重试）",
            "interrupted": "中断（已计数）",
            "started_or_interrupted": "已启动，等待完成记录",
            "pending": "尚未启动",
        }
        attempts = {r["slot"]: r for r in report["attempts"]}
        rows, metrics = [], []
        for fold in (1, 2, 3):
            for kind in ("ridge", "lightgbm"):
                key = f"fold{fold}_{kind}"
                record = attempts.get(key, {"status": "pending"})
                name = "岭回归" if kind == "ridge" else "LightGBM"
                rows.append(
                    {
                        "窗口": fold,
                        "模型": name,
                        "训练截至": f"{2021 + fold}-12-31",
                        "评价年份": str(2022 + fold),
                        "状态": status_names.get(record["status"], record["status"]),
                    }
                )
                if record["status"] == "failed":
                    st.error(f"窗口 {fold} · {name}：{record.get('error', '详见训练记录')}")
                summary = record.get("summary", {})
                for period, values in summary.get("summaries", {}).items():
                    metrics.append(
                        {
                            "窗口": fold,
                            "模型": name,
                            "区间": {
                                "train": "训练期（样本内）",
                                "evaluation": str(2022 + fold),
                                "observed_2026": "2026 已观察",
                            }[period],
                            "预测记录": values["score_rows"],
                            "标签有效记录": values["evaluation_rows"],
                            "日均 RankIC": values["mean_rank_ic"],
                            "分数标准差": values["mean_score_std_population"],
                            "相邻日排名相关": values["mean_rank_stability"],
                            "前20名成员变化": values["mean_top20_membership_change"],
                        }
                    )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if metrics:
            st.dataframe(pd.DataFrame(metrics), hide_index=True, width="stretch")
        st.caption(
            "RankIC 是每日预测排序与未来 5 个交易日收益排序的相关性，不是收益率。"
            "标签跨区间边界或缺失时不进入评价，但不因此删除预测。前20名成员变化是"
            "名单稳定性的代理指标，不是实际换手或成交。重叠标签不能当成独立样本。"
        )
        if report["status"] == "complete":
            st.caption(
                f"累计新增文件约 {report['generated_bytes'] / 1024**3:.2f} GiB；"
                f"训练子进程内存峰值 {report['child_peak_rss_bytes'] / 1024**3:.2f} GiB。"
                "上限：8 GiB 文件、8 GiB 进程内存、两条计算线程。"
            )
            st.download_button(
                "下载滚动模型完整记录",
                (out / "report.json").read_bytes(),
                "QuantLab_Alpha158滚动模型.json",
                "application/json",
            )
        findings = PROJECT_ROOT / "docs/alpha158_rolling_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载滚动模型中文解读",
                findings.read_bytes(),
                "QuantLab_Alpha158滚动解读.md",
                "text/markdown",
            )
        st.info(
            "本轮不评价可成交净收益，也未验证最大回撤 20% 的目标。下一步结合跨年稳定性、"
            "成本和交易可行性证据安排固定研究；完整费用、历史规则及公司行为记账仍需补齐。"
        )
    except Exception as exc:
        st.error(f"滚动模型记录无法校验：{public_error(exc)}")
