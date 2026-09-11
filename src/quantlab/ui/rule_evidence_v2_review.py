"""Read-only v2 findings with an explicit route to the unchanged v1 display."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.rule_evidence_v2 import MANIFEST, OUTPUT, read_report
from quantlab.ui.rule_evidence_review import NAMES, render_rule_evidence
from quantlab.ui.workbench import public_error


def render_rule_evidence_versions():
    try:
        report = read_report(PROJECT_ROOT)
        if report is None:
            render_rule_evidence()
            return
        version = st.radio("查看规则证据版本", ["v2（补齐早期依据）", "v1（封存记录）"],
                           horizontal=True)
        if version == "v1（封存记录）":
            render_rule_evidence()
            return
        st.subheader("历史交易规则 v2：早期依据已补齐")
        st.write(
            "重新检查同一份 2023 年至 2026 年 9 月 10 日的日历，补齐年初旧版通知和修订链。"
            "连续竞价限价单的普通规则范围已覆盖；这仍是一份研究证据目录。"
        )
        comparison = report["comparison_to_v1"]
        a, b = st.columns(2)
        a.metric("本轮补齐的板块×交易日", f"{comparison['newly_covered_rows']:,} 项")
        b.metric("上一版数值与来源区间保持一致", f"{comparison['unchanged_rows']:,} 项")
        added = comparison["additions"]
        rows = []
        for r in report["summary"]:
            count = sum(x["exchange"] == r["exchange"] and x["board"] == r["board"] for x in added)
            rows.append({"板块": NAMES[(r["exchange"], r["board"])],
                         "固定交易日": r["sessions"],
                         "v1覆盖": r["catalogue_covered"] - count,
                         "v2覆盖": r["catalogue_covered"], "本轮新增": count,
                         "该范围仍未知": r["still_unknown"]})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(
            "补齐区间为 2023-01-03 至 2023-04-07，每板块 63 个交易日。"
            "复用 24 份原件，新增 8 份官方文件；v1 的原报告和当时的缺口记录继续保留。"
        )
        with st.expander("查看旧版生效时间与保留的限制"):
            st.write(
                "科创板特别规定从 2019-03-01 生效；2020 版上交所规则从 2020-03-13 生效。"
                "创业板特别规定从 2020-08-24 生效，深交所 2021 版从 2021-04-06 生效。"
                "两所 2022 年大宗修订分别从沪市 2022-09-05、深市 2022-08-22 生效；"
                "大宗机制没有被扩展成此目录支持的连续竞价行为。"
            )
            st.write(
                "旧默认深交所版本从发布日期 2021-03-31 起算的问题仍是版本迁移阻塞，"
                "未修改旧规则或参考计划。零股仍只实现普通整手及全仓退出的保守子集。"
            )
        st.info(
            "覆盖完成仅限这里列出的普通规则字段和日期，不能证明某只股票能买卖或成交。"
            "历史个股身份、账户权限、ST/停牌、涨跌幅与价格笼子、完整费用及公司行为"
            "仍需单独核对。本轮未做收益回测，最大回撤 20% 的目标仍未验证。"
        )
        st.caption(
            "下一步使用已保存的 Alpha158 模型分数审计费用和公司行为输入，"
            "为持有 5 / 10 / 20 个交易日、假设 5 万 / 20 万 / 100 万元的组合评价做准备。"
        )
        for label, path, name in (
            ("下载v2逐日核验与版本对比", OUTPUT + "/report.json", "QuantLab_历史规则v2.json"),
            ("下载v2官方来源清单", MANIFEST, "QuantLab_历史规则v2来源.json"),
            ("下载v2中文解读", "docs/historical_rule_v2_findings_zh.md", "QuantLab_规则v2解读.md"),
        ):
            target = PROJECT_ROOT / path
            if target.exists():
                st.download_button(label, target.read_bytes(), name,
                                   "text/markdown" if name.endswith(".md") else "application/json")
    except Exception as exc:
        st.error(f"v2规则证据无法校验：{public_error(exc)}")
