"""Verified v3 research progress in the existing personal workbench."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.funding_attribution import read_progress
from quantlab.ui.workbench import public_error

NAMES = {
    "F1": "5日大单净流入强度",
    "F2": "20日大单净流入强度",
    "F3": "20日流入持续性",
    "F4": "资金流与价格背离",
}
COMPONENTS = {
    "F4": "资金流与价格背离",
    "reversal_component": "仅用过去20日价格反转",
    "F2": "仅用20日资金流",
    "F4_minus_reversal": "背离减去纯反转的关联差",
    "F2_within_momentum": "相近过去涨跌幅内的资金流关联",
}


def render_program():
    st.subheader("策略研究进度")
    st.write("已启动你批准的五类策略计划。本页展示首批已复核记录，后续按有限批次继续推进。")
    try:
        reviewed = read_progress(PROJECT_ROOT)
    except Exception as exc:
        st.error(f"研究记录无法校验：{public_error(exc)}")
        return
    if reviewed is None:
        st.info("首批研究尚未完成独立复核，暂不展示结果。")
        return
    report = reviewed["pilot"]
    cols = st.columns(4)
    cols[0].metric("资金流指标", f"{len(report['funding_features_used'])} / 8")
    cols[1].metric("组合回测路径", f"{report['economic_paths_used']} / 80")
    cols[2].metric("模型拟合", f"{report['model_fits_used']} / 6")
    cols[3].metric("首批股票样本", str(report["cohort_count"]))
    st.caption("以上是首批已完成记录的用量；资金流指标诊断不等于已验证策略。")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "方向": "ETF趋势与轮动",
                    "当前进展": "已取得含退市产品的基金清单；待核实价格、分配与历史跟踪指数",
                },
                {
                    "方向": "估值／盈利改善",
                    "当前进展": "先审计历史估值与披露版本；质量腿保留前瞻路线",
                },
                {"方向": "行业趋势选股", "当前进展": "先审计历史行业成员，资料不足按计划降级"},
                {
                    "方向": "条件反转",
                    "当前进展": "已做资金流诊断，继续检查价格反转与资金流的各自贡献",
                },
                {"方向": "结构化事件", "当前进展": "待封存首批事件规则与输入，暂未开展经济回测"},
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.warning("尚无策略通过晋升。本页数值是历史样本中的排序关联，不是收益率，也不是交易建议。")
    st.write(
        "正的 RankIC 表示指标排名与随后5个交易日的价格涨跌排名倾向同向；"
        "它不能说明扣除手续费后能赚多少。样本固定为2019年底已有股票中的256只，未代表全市场。"
    )
    st.caption(
        "2020—2024均属已观察历史。资金流历史发布与修订版本尚未证实；本批只作探索，不能据此晋升。"
    )
    summary = pd.DataFrame(report["summaries"])
    annual = summary[summary.period.isin([str(y) for y in range(2020, 2025)])]
    pivot = annual.pivot(index="feature", columns="period", values="mean_rank_ic").reindex(NAMES)
    pivot.index = pivot.index.map(NAMES)
    pivot.index.name = "指标"
    st.markdown("**四项资金流指标的逐年关联**")
    st.dataframe(pivot.round(4), width="stretch")
    st.write(
        "单纯大单净流入强度和流入持续性没有呈现稳定的正向关联。"
        "背离指标包含价格反转成分，需要先拆解贡献。"
    )
    attribution = reviewed["attribution"]
    if attribution is None:
        st.info("成分对照尚未完成独立复核；暂不判断资金流是否增加了信息。")
    else:
        table = pd.DataFrame(attribution["summaries"])
        period = st.selectbox("成分对照区间", ["2020-2024", "2020-2022", "2023-2024"])
        selected = table[table.period == period].copy()
        selected["series"] = selected.series.map(COMPONENTS)
        selected["mean"] = selected["mean"].map(lambda x: f"{x:.4f}" if pd.notna(x) else "未知")
        selected["moving_block_95_interval"] = selected["moving_block_95_interval"].map(
            lambda x: f"[{x[0]:.4f}, {x[1]:.4f}]" if isinstance(x, list) else "未知"
        )
        selected = selected.rename(
            columns={
                "series": "信息来源",
                "mean": "平均排序关联／关联差",
                "valid_sessions": "有效交易日",
                "moving_block_95_interval": "分块重采样95%区间",
            }
        )
        st.markdown("**资金流是否增加了信息**")
        st.dataframe(
            selected[["信息来源", "平均排序关联／关联差", "有效交易日", "分块重采样95%区间"]],
            hide_index=True,
            width="stretch",
        )
        whole = table[table.period == "2020-2024"].set_index("series")
        delta = whole.loc["F4_minus_reversal", "mean"]
        interval = whole.loc["F4_minus_reversal", "moving_block_95_interval"]
        if pd.notna(delta) and delta <= 0:
            st.write("在相同样本上，背离指标没有超过纯价格反转成分；当前不能把其关联归功于资金流。")
        elif isinstance(interval, list) and interval[0] > 0:
            st.write(
                "背离指标在本批样本中的关联高于纯反转；仍需时点、样本与扣成本验证，不能直接启用。"
            )
        else:
            st.write("背离指标与纯反转的差异仍不确定，资金流增量尚未证实。")
        st.caption(
            "条件关联按过去涨跌幅分成5组，不是因果证明。重采样考虑短期相关性，不能消除历史反复研究的偏差。"
        )
        st.download_button(
            "下载成分对照报告",
            json.dumps(attribution, ensure_ascii=False, indent=2),
            "funding_attribution_report.json",
            "application/json",
            on_click="ignore",
        )
    st.download_button(
        "下载资金流诊断报告",
        json.dumps(report, ensure_ascii=False, indent=2),
        "funding_pilot_report.json",
        "application/json",
        on_click="ignore",
    )
    st.download_button(
        "下载逐年指标表",
        summary.to_csv(index=False).encode("utf-8-sig"),
        "funding_pilot_summary.csv",
        "text/csv",
        on_click="ignore",
    )
    recorded_at = pd.Timestamp(report["at"]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    st.caption(
        f"记录时间：{recorded_at}（北京时间）。缺失数据保持未知；未模拟订单、成交或账户收益。"
    )
