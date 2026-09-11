"""Chinese, read-only display of the source-bound rule coverage audit."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.rule_evidence import MANIFEST, OUTPUT, read_report
from quantlab.ui.workbench import public_error

NAMES = {("SSE", "MAIN"): "沪市主板", ("SSE", "STAR"): "科创板",
         ("SZSE", "MAIN"): "深市主板", ("SZSE", "CHINEXT"): "创业板"}


def render_rule_evidence():
    st.subheader("历史交易规则：证据覆盖")
    st.write(
        "核对沪深主板、科创板和创业板在 2023 年至 2026 年 9 月 10 日的规则依据。"
        "这里只审计连续竞价限价单的普通数量、报价单位和最早卖出滞后，供后续组合研究使用。"
    )
    try:
        report = read_report(PROJECT_ROOT)
        if report is None:
            st.info("规则目录已固定，尚未完成交易日日历审计。")
            return
        a, b = st.columns(2)
        a.metric("逐板块、逐日检查", f"{len(report['rows']):,} 项")
        covered = sum(r["catalogue_covered"] for r in report["summary"])
        b.metric("已覆盖的板块×交易日", f"{covered:,} 项")
        rows = [{
            "板块": NAMES[(r["exchange"], r["board"])],
            "旧目录覆盖": r["baseline_covered"], "新目录覆盖": r["catalogue_covered"],
            "新增覆盖": r["newly_covered"], "新目录仍未知": r["still_unknown"],
            "重叠日期数值冲突": r["conflict"],
        } for r in report["summary"]]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(
            "每板块固定检查 895 个交易日。新目录覆盖 2023-04-10 至 2026-09-10；"
            "2023 版与 2026 版以 2026-07-06 为边界。9 月 10 日是审计上限，不是法规失效日。"
        )
        st.warning(
            "2023-01-03 至 2023-04-07 的 63 个交易日，各板块仍缺完整旧版证据链。"
            "旧默认目录虽有结果，本次没有将其直接转为新目录的核验结论。"
        )
        with st.expander("查看版本区间与发现的差异"):
            st.dataframe(pd.DataFrame([{
                "板块": NAMES[(r["exchange"], r["board"])],
                "从": r["from"], "至": r["through"], "交易日数": r["sessions"],
                "结果": {"verified_existing": "与旧目录数值一致", "newly_covered": "新增依据",
                         "still_unknown": "新目录未知", "conflict": "数值冲突"}[r["status"]],
            } for r in report["intervals"]]), hide_index=True, width="stretch")
            for finding in report["source_discrepancies"]:
                st.write(finding["detail"])
        st.info(
            "规则有依据仍不等于某只股票可买、可卖或能成交。历史板块身份、账户权限、"
            "停牌、涨跌停、价格笼子、完整费用及公司行为仍需单独核对。"
            "本轮没有收益回测，也未验证最大回撤 20% 的目标；旧默认规则及参考计划行为保持原样。"
        )
        st.caption(
            "这一步为后续固定持有 5 / 10 / 20 个交易日、不同假设资金规模的扣费评价补充依据。"
            "零股卖出只表达普通整手及全仓退出的保守子集；T+1 是最早普通卖出滞后，不保证成交。"
        )
        st.download_button(
            "下载历史规则逐日核验", (PROJECT_ROOT / OUTPUT / "report.json").read_bytes(),
            "QuantLab_历史规则逐日核验.json", "application/json",
        )
        st.download_button(
            "下载官方规则来源清单", (PROJECT_ROOT / MANIFEST).read_bytes(),
            "QuantLab_官方规则来源.json", "application/json",
        )
        findings = PROJECT_ROOT / "docs/historical_rule_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载历史规则中文解读", findings.read_bytes(),
                "QuantLab_历史规则中文解读.md", "text/markdown",
            )
    except Exception as exc:
        st.error(f"历史规则核验记录无法校验：{public_error(exc)}")
