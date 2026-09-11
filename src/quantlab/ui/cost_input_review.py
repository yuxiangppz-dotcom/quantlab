"""Read-only Chinese readiness evidence and an exact data acquisition request."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.cost_input_review import read_report
from quantlab.research.cost_input_sources import MANIFEST, OUTPUT
from quantlab.ui.workbench import public_error


def render_cost_input_review():
    st.subheader("扣费评价准备度：费用与分红送转")
    try:
        report = read_report(PROJECT_ROOT)
        if report is None:
            st.info("输入审计尚未完成。")
            return
        request = report["acquisition_request"]
        missing = len(request["codes_without_any_local_event"])
        a, b, c = st.columns(3)
        a.metric("已核对模型分数", f"{report['score_rows']:,} 条")
        b.metric("目标股票范围", f"{request['instrument_count']:,} 只")
        c.metric("暂无本地分红送转记录", f"{missing:,} 只")
        st.warning(
            f"目前无法完成扣费收益评价：{missing:,} 只目标股票暂无本地分红送转记录。"
            "没有记录不能理解为没有分红；复权因子也不能代替实际到账现金、股份和股息税。"
        )
        rows = []
        for item in report["summary"]:
            total, complete = item["target_windows"], item["complete_market_window"]
            rows.append(
                {
                    "模型": {"ridge": "岭回归", "lightgbm": "LightGBM"}[item["model"]],
                    "预期持有交易日": item["horizon"],
                    "目标窗口": total,
                    "报价及复权齐全": complete,
                    "齐全比例": f"{complete / total:.2%}",
                    "退出超出资料上限": item["exit_beyond_cutoff"],
                    "其余不完整窗口": total - complete - item["exit_beyond_cutoff"],
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(
            "固定检查 2023-01-03 至 2026-09-10 的 895 个信号日；每模型每天至多 20 个目标，"
            "80% 总仓位。这些是独立研究意向，窗口会重叠。报价齐全仍不能证明可买卖或成交。"
        )
        with st.expander("查看缺口与费用口径"):
            profile = report["corporate_profile"]
            codes = "、".join(profile["instrument_ids"])
            st.write(
                f"本地共有 {profile['rows']} 条分红送转记录，"
                f"仅涉及 {profile['instruments']} 只股票，"
                f"代码为 {codes}。"
                "实施记录与预案、股东大会通过等记录需要分别核对；"
                "字段缺失不一定是下载错误。"
            )
            st.write(
                "佣金沿用万分之 0.86、股票最低 5 元，印花税另计；"
                "按假设 5 万 / 20 万 / 100 万元，核对不同日期的单笔费用分项。"
                "这不是完整成本，也不是实际成交或券商账单；其他收费、滑点和股息税保持未知。"
            )
            st.write(
                "股息税还需要账户持股批次和自然月/年期限，不能把 20 个交易日直接当作一个月。"
                "分红方案的公告、实施、登记、除权、到账和红股上市日期不能混用。"
            )
            st.write(
                f"模型元数据中另有 {report['metadata_incomplete']:,} 条特征不完整记录，"
                "仍保留在源资料中。"
                "原有六次模型训练未重做；本轮读取八份评价文件，包含两份 2026 年预测。"
            )
        st.info(
            "下一步按固定的目标股票清单补齐公司行为原始资料和取回记录，再核对现金与股份处理。"
            "本轮没有收益回测，最大回撤 20% 的目标仍未验证。"
        )
        for label, path, filename in (
            ("下载扣费输入审计", OUTPUT + "/report.json", "QuantLab_扣费输入审计.json"),
            (
                "下载待补数据清单",
                OUTPUT + "/acquisition_request.json",
                "QuantLab_待补公司行为数据.json",
            ),
            ("下载费用与公司行为来源", MANIFEST, "QuantLab_扣费输入来源.json"),
            ("下载扣费输入中文解读", "docs/cost_input_findings_zh.md", "QuantLab_扣费输入解读.md"),
        ):
            file = PROJECT_ROOT / path
            if file.exists():
                st.download_button(
                    label,
                    file.read_bytes(),
                    filename,
                    "text/markdown" if filename.endswith(".md") else "application/json",
                )
    except Exception as exc:
        st.error(f"扣费输入证据无法校验：{public_error(exc)}")
