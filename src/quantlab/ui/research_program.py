"""Verified v3 research progress in the existing personal workbench."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.alpha101_pilot import read_progress as read_alpha_progress
from quantlab.research.etf_event_progress import read_etf_progress
from quantlab.research.funding_attribution import read_progress
from quantlab.research.replay_readiness import read_preparation
from quantlab.research.s4_pilot import read_progress as read_s4_progress
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


def render_etf_progress():
    st.markdown("**ETF：分红、拆分与历史资料核对**")
    try:
        report = read_etf_progress(PROJECT_ROOT)
    except Exception as exc:
        st.error(f"ETF核对记录无法校验：{public_error(exc)}")
        return
    if report is None:
        st.info("ETF资料核对尚未完成复核，暂不展示结果。")
        return
    st.write(
        f"已核对5只固定ETF、{report['pdf_count']}份公开原始文件。"
        f"分红数据的{report['duplicate_groups']}组重复记录中，"
        f"{report['exact_duplicate_groups']}组完全相同，"
        f"{report['metadata_conflict_groups']}组辅助资料不同；每份现金及关键日期在各组内一致。"
        "这些重复不能当成多次分红相加，原始记录已保留。"
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "代码": row["code"],
                    "类别": row["name"],
                    "已核实": row["verified_progress"],
                    "收益回测尚缺": "；".join(row["s1_blockers"]),
                }
                for row in report["instruments"]
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "2022—2024无分配已有相应年报支持，但不能自动扩展到更早年份。"
        "拆分造成的份额增加不能当作资金流入。ETF份额公开时间的缺口影响份额因子，"
        "不单独阻塞只用价格的趋势／轮动版本。"
    )
    st.info("本批完成的是资料核对，尚未计算ETF策略收益。未决缺口已列出，接下来按封存规则推进对照。")
    st.download_button(
        "下载ETF资料核对报告",
        json.dumps(report, ensure_ascii=False, indent=2),
        "etf_event_audit_report.json",
        "application/json",
        on_click="ignore",
    )


def render_alpha_progress():
    st.markdown("**新增价量公式：是否比已有价格反转多提供信息**")
    try:
        report = read_alpha_progress(PROJECT_ROOT)
    except Exception as exc:
        st.error(f"价量公式记录无法校验：{public_error(exc)}")
        return
    if report is None:
        st.info("首批Alpha101价量公式尚未完成复核，暂不展示结果。")
        return
    names = {
        "alpha101_12": "Alpha101 #12：量变与短期反转",
        "alpha101_101": "Alpha101 #101：日内方向与振幅",
    }
    table = pd.DataFrame(report["summaries"])
    annual = table[table.period.isin([str(y) for y in range(2020, 2025)])]
    pivot = annual.pivot(index="formula", columns="period", values="mean_rank_ic").reindex(names)
    pivot.index = pivot.index.map(names)
    pivot.index.name = "公式"
    st.caption(
        "本批完成2项／首批上限20项公式。固定256只历史股票、随后5个交易日的排序关联；没有训练模型。"
    )
    st.dataframe(pivot.round(4), width="stretch")
    whole = table[table.period == "2020-2024"].copy()
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "公式": names[row.formula],
                    "公式关联": f"{row.mean_rank_ic:.4f}" if pd.notna(row.mean_rank_ic) else "未知",
                    "同样本价格反转关联": f"{row.mean_reversal_rank_ic:.4f}"
                    if pd.notna(row.mean_reversal_rank_ic)
                    else "未知",
                    "关联差": f"{row.mean_paired_ic_difference:.4f}"
                    if pd.notna(row.mean_paired_ic_difference)
                    else "未知",
                    "差的95%区间": (
                        f"[{row.difference_block_95_interval[0]:.4f}, "
                        f"{row.difference_block_95_interval[1]:.4f}]"
                    )
                    if isinstance(row.difference_block_95_interval, list)
                    else "未知",
                }
                for row in whole.itertuples()
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.write(
        "公式保留原来的正负方向，没有看完结果再反过来选股。这些数值不是收益率，尚不能据此确定股票或启用策略。"
    )
    st.caption(
        "#12遇到记录的复权因子变化时，不跨该日计算原始价差。历史可得信息和实际成交条件仍需另行验入；Alpha191留在后续有限批次。"
    )
    st.download_button(
        "下载价量公式诊断报告",
        json.dumps(report, ensure_ascii=False, indent=2),
        "alpha101_price_volume_report.json",
        "application/json",
        on_click="ignore",
    )


def render_s4_progress():
    st.markdown("**条件反转：下跌后的修复是否需要额外条件**")
    try:
        report = read_s4_progress(PROJECT_ROOT)
    except Exception as exc:
        st.error(f"条件反转记录无法校验：{public_error(exc)}")
        return
    if report is None:
        st.info("S4三种条件反转规则尚未完成复核，暂不展示结果。")
        return
    names = {
        "S4-A": "三日相对下跌",
        "S4-B": "相同信号＋60日正趋势",
        "S4-C": "相同信号＋5日资金流转正",
    }
    table = pd.DataFrame(report["summaries"])
    annual = table[table.period.isin([str(y) for y in range(2020, 2025)])]
    pivot = annual.pivot(index="variant", columns="period", values="mean_rank_ic").reindex(names)
    pivot.index = pivot.index.map(names)
    pivot.index.name = "规则"
    st.caption(
        "固定256只历史股票；比较随后5个交易日的排序关联。三条使用原计划15个规则中的3个身份；未运行组合回测或模型。"
    )
    st.dataframe(pivot.round(4), width="stretch")
    whole = table[table.period == "2020-2024"]

    def number(x):
        return f"{x:.4f}" if pd.notna(x) else "未知"

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "规则": names[row.variant],
                    "每日平均保留股票": f"{row.mean_retained_signal_count:.1f}",
                    "有效关联交易日": f"{row.valid_ic_sessions} / {row.calendar_sessions}",
                    "每日平均条件未知": f"{row.mean_condition_unknown_count:.1f}",
                    "规则关联": number(row.mean_rank_ic),
                    "同样本20日反转关联": number(row.mean_reversal_rank_ic),
                    "关联差": number(row.mean_paired_ic_difference),
                }
                for row in whole.itertuples()
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.write(
        "条件不满足和条件资料未知分别保留。每天不足30个有效对照样本时不计算关联，也不会为得到结果临时放宽条件。"
    )
    st.caption(
        "B/C筛出的股票群体与A不同，关联变化不能直接解释成条件带来的增量收益。资金流的历史发布版本仍未证实；这些指标不含手续费、无法卖出或账户回撤。"
    )
    st.download_button(
        "下载条件反转诊断报告",
        json.dumps(report, ensure_ascii=False, indent=2),
        "s4_signal_report.json",
        "application/json",
        on_click="ignore",
    )


def render_replay_preparation():
    st.markdown("**扣费回测准备：已补齐什么，还差什么**")
    try:
        preparation = read_preparation(PROJECT_ROOT)
    except Exception as exc:
        st.error(f"回放准备度记录无法校验：{public_error(exc)}")
        return
    if preparation is None:
        st.info("分红与历史规则的本批核对记录尚未齐备，暂不展示准备度。")
        return
    terms = preparation["terms"]
    notice = preparation["cash_notice_annotations"]
    shares = preparation["share_notice_annotations"]
    comparison = preparation["rule_comparison"]
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "环节": "分红与送转基本字段",
                    "核对范围": f"2020—2024年，{terms['occurrences']}条实施记录",
                    "已经核对": f"{terms['cash_fields_complete']}条现金字段齐全；"
                    f"{terms['quantity_fields_complete']}条数量字段齐全",
                    "仍需完成": "事件是否完整、谁有权收款收股、税务、实际到账及可售日期",
                },
                {
                    "环节": "现金未知记录的原件",
                    "核对范围": f"固定{preparation['recipient_occurrences']}条记录",
                    "已经核对": f"{notice.get('explicit_zero', 0)}条明确不派现金；"
                    f"{shares.get('restructuring_allocation', 0)}条重整定向分配",
                    "仍需完成": f"{notice.get('unknown', 0)}条现金仍未知；"
                    f"{preparation['ordinary_recipient_evidence_blocked']}条未通过普通分配对象检查",
                },
                {
                    "环节": "历史数量规则",
                    "核对范围": "2022年，4个市场板块",
                    "已经核对": f"{comparison['covered_2022']}条板块／交易日记录覆盖；"
                    f"此前{comparison['unchanged_rows']}条记录保持一致",
                    "仍需完成": "收盘成交情景、逐股状态、公司行为与逐日股数现金连接",
                },
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.write(
        "重整增发可能分给债权人或投资人，不能按公司总股本增加比例给原股东加仓。"
        f"本批{preparation['recipient_occurrences']}条中仅"
        f"{preparation['ordinary_recipient_evidence_complete']}条具备所要求的普通分配对象原始公告；"
        "这仍不等于可以直接入账。"
    )
    st.caption(
        "字段齐全不等于完整现金流；股票供应商原始空值仍保留。历史规则只覆盖连续竞价限价单的基础数量条款，"
        "不能证明收盘集合竞价可成交。这些准备工作尚未产生本轮策略的扣费净收益。"
    )
    st.download_button(
        "下载回放准备度摘要",
        json.dumps(preparation, ensure_ascii=False, indent=2),
        "replay_preparation_summary.json",
        "application/json",
        on_click="ignore",
    )


def render_program():
    st.subheader("策略研究进度")
    st.write("已启动你批准的五类策略计划。本页展示已复核记录，后续按有限批次继续推进。")
    try:
        reviewed = read_progress(PROJECT_ROOT)
    except Exception as exc:
        st.error(f"研究记录无法校验：{public_error(exc)}")
        return
    if reviewed is None:
        st.info("首批研究尚未完成独立复核，暂不展示结果。")
        render_etf_progress()
        render_alpha_progress()
        render_s4_progress()
        render_replay_preparation()
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
                    "当前进展": "已采集固定5只ETF；分红、拆分与历史资料核对见下方",
                },
                {
                    "方向": "估值／盈利改善",
                    "当前进展": "先审计历史估值与披露版本；质量腿保留前瞻路线",
                },
                {"方向": "行业趋势选股", "当前进展": "先审计历史行业成员，资料不足按计划降级"},
                {
                    "方向": "条件反转",
                    "当前进展": "固定三种条件反转规则；实际复核结果与样本覆盖见下方",
                },
                {"方向": "结构化事件", "当前进展": "待封存首批事件规则与输入，暂未开展经济回测"},
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    render_etf_progress()
    render_alpha_progress()
    render_s4_progress()
    render_replay_preparation()
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
