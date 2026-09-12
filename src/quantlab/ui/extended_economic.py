"""Chinese read-only economic proxy: comparable prefixes and all stopped paths."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.extended_economic_contract import (
    CAPITALS,
    CONTROL,
    FRICTIONS,
    HORIZONS,
    OUT,
    POLICIES,
    STRATEGIES,
)
from quantlab.research.extended_economic_review import (
    comparable_prefix,
    load_case,
    read_review,
    summary_table,
)
from quantlab.ui.workbench import public_error

NAMES = {
    CONTROL: "预测成员等权对照（80%仓位）",
    "ridge_weekly": "岭回归 · 每周重训",
    "ridge_monthly": "岭回归 · 月内复用",
    "lightgbm_weekly": "LightGBM · 每周重训",
    "lightgbm_monthly": "LightGBM · 月内复用",
}
LABELS = {
    "policy": "策略",
    "horizon": "调仓间隔（交易日）",
    "friction_bps": "额外单边摩擦（基点）",
    "engine_status": "路径状态",
    "valid_through": "有效截至",
    "valid_sessions": "有效交易日",
    "full_period_completed": "完整观察期",
    "terminal_open_positions": "期末持仓数",
    "blocking_instrument": "停止相关股票",
    "blocking_session": "停止日",
    "last_observed_mark_date": "最后有价标记日",
    "common_end": "共同有效截至",
    "sessions": "共同交易日",
    "return_after_declared_friction": "声明摩擦后区间收益",
    "drawdown": "共同区间回撤",
    "declared_friction_charged": "额外摩擦（初始资金比例）",
    "difference_from_cohort_control": "相对同成员对照收益差",
    "return_after_declared_friction_on_valid_prefix": "各自有效区间收益（声明摩擦后）",
    "drawdown_on_valid_prefix": "各自有效区间回撤",
    "final_cash": "末期现金（初始资金比例）",
}


def display(frame):
    return frame.replace(
        {
            **NAMES,
            "completed": "完整观察期已计算",
            "blocked_by_unsupported_event": "生命周期证据不足，已停止",
        }
    ).rename(columns=LABELS)


def csv_download(label, frame, filename):
    st.download_button(
        label,
        frame.to_csv(index=False).encode("utf-8-sig"),
        filename,
        "text/csv",
        on_click="ignore",
    )


def render_economic():
    st.subheader("组合与成本研究")
    st.caption("保存分数 → 固定目标 → 下一交易日复权收盘价价值转移。全部选择事前固定，本页只读。")
    try:
        reviewed = read_review(PROJECT_ROOT)
        if reviewed is None:
            st.info("经济情景尚未完成独立复核，当前不展示收益结论。")
            return
        report, plan, proof = reviewed
        all_rows = summary_table(report)
        whole = all_rows[all_rows.full_period_completed]
        stopped = all_rows[~all_rows.full_period_completed]
        a, b, c = st.columns(3)
        a.metric("独立复核情景", f"{len(all_rows)} / 60")
        b.metric("覆盖完整观察期", str(len(whole)))
        c.metric("因生命周期问题停止", str(len(stopped)))
        st.warning(
            "当前基线没有满足20%回撤目标的证据：16条完整路径的研究回撤约30.3%—42.1%；"
            "另外44条提前停止，后续价值未知。不能据此直接启用交易。"
        )
        st.write(
            "36条路径遇到000851.SZ持仓退市，8条遇到600355.SH。缺少可核验的退出或结算依据时，"
            "系统保留持仓并停止该路径。现有终止上市风险事实只覆盖其已核验记录；其他公告覆盖仍有缺口。"
        )
        st.caption(
            "这是理想化复权价格代理，额外摩擦不等于完整交易费用。尚未纳入逐单最低佣金、"
            "卖方印花税、整数股、成交能力及权益/股息税现金账；历史全市场覆盖也未证实。"
        )
        left, right = st.columns(2)
        horizon = left.selectbox("计划调仓间隔（市场交易日）", HORIZONS)
        friction = right.selectbox(
            "额外单边摩擦（基点）",
            FRICTIONS,
            help="1基点=0.01%。0是研究比较项，不表示实际交易没有费用。",
        )
        frames = {}
        for policy in POLICIES:
            identity = f"{policy}_h{horizon}_bps{friction}"
            # Read only small daily series for the comparison, after full receipt validation.
            frames[policy] = pd.read_parquet(
                PROJECT_ROOT / OUT / "paths" / identity / "daily.parquet", use_threads=False
            )
        common, curves = comparable_prefix(frames)
        st.markdown("**先在完全相同的有效区间比较**")
        st.caption(
            f"2025-09-01 至 {common.common_end.iloc[0]}，共{common.sessions.iloc[0]}个交易日。"
            "截止日由五条路径共同有效范围自动确定，没有挑选表现好的月份；不能据此代表全年。"
        )
        st.line_chart(curves.rename(columns=NAMES), x_label="共同交易日", y_label="初始价值=1")
        st.dataframe(display(common), hide_index=True, width="stretch")
        csv_download("下载同区间五组比较", display(common), "QuantLab_同区间五组比较.csv")

        st.markdown("**检查一条路径的完整记录**")
        policy = st.selectbox("查看策略", (*STRATEGIES, CONTROL), format_func=NAMES.__getitem__)
        capital = st.selectbox(
            "假设资金（元，仅线性缩放）",
            CAPITALS,
            help="这些资金规模没有重新计算整股或最低佣金，不是额外独立实验，也不是你的实际账户。",
        )
        identity = f"{policy}_h{horizon}_bps{friction}"
        summary, daily, positions, transfers, audit = load_case(PROJECT_ROOT, report, identity)
        path_status = (
            "完整观察期已计算" if summary["full_period_completed"] else "已停止，后续价值未知"
        )
        st.write(f"有效截至 {summary['valid_through']}；{path_status}。")
        if summary["first_blocking_event"]:
            event = summary["first_blocking_event"]
            st.warning(
                f"{event['instrument_id']} 于 {event['blocking_session']} 触发生命周期停止；"
                f"最后标记日期 {event['last_mark_date']}。最后标记日不等于已核验的最后交易日。"
            )
        x, y, z = st.columns(3)
        x.metric(
            "各自有效区间收益（声明摩擦后）",
            f"{summary['return_after_declared_friction_on_valid_prefix']:.2%}",
        )
        y.metric("各自有效区间最大回撤", f"{summary['drawdown_on_valid_prefix']:.2%}")
        z.metric("末期未平仓股票", summary["terminal_open_positions"])
        st.caption(
            "下面是该路径自身的有效区间，可能比上方共同区间更长。不同截止日期的收益与回撤不能直接比较。"
        )
        plot = daily.set_index("trade_date")[["value_after_declared_friction", "cash"]] * capital
        st.line_chart(
            plot.rename(
                columns={"value_after_declared_friction": "声明摩擦后假设价值", "cash": "假设现金"}
            ),
            y_label="假设金额（元）",
        )
        interval = "已满" if summary["terminal_planned_holding_interval_complete"] else "尚未满"
        st.write(
            f"期末假设现金 {summary['final_cash'] * capital:,.2f} 元；累计额外摩擦 "
            f"{summary['declared_friction_charged'] * capital:,.2f} 元。"
            f"最后调仓后观察了 {summary['observed_sessions_since_last_rebalance']} 个交易日，"
            f"本次计划持有区间{interval}；"
            "没有末日强制平仓。"
        )
        rebalances = pd.DataFrame(audit["rebalances"])
        st.write(
            f"调仓记录中的累计单边换手率 {rebalances.turnover.sum():.2%}；"
            f"持仓缺价冻结计数 {int(rebalances.frozen_count.sum())}，"
            f"新目标缺价计数 {int(rebalances.unavailable_target_count.sum())}。"
            "这些计数可跨调仓重复。"
        )
        final = positions[positions.trade_date.eq(positions.trade_date.max())].copy()
        final["value"] *= capital
        st.dataframe(
            final.head(50).rename(
                columns={
                    "instrument_id": "股票代码",
                    "value": "假设持仓金额",
                    "weight": "持仓权重",
                    "last_price": "最后复权标记",
                    "last_mark_date": "标记日期",
                    "missing_price": "当日缺价",
                    "trade_date": "有效日期",
                }
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(f"末期持仓共{len(final)}条，页面最多展示50条；完整逐日记录可下载。")
        csv_download("下载所选情景逐日价值", daily, "QuantLab_情景逐日价值_标准化.csv")
        csv_download("下载所选情景逐日持仓", positions, "QuantLab_情景逐日持仓_标准化.csv")
        csv_download("下载所选情景价值转移", transfers, "QuantLab_情景价值转移_非真实成交.csv")
        csv_download("下载所选情景调仓核算", rebalances, "QuantLab_情景调仓核算_旧引擎字段.csv")
        st.caption(
            "下载中的标准化金额以初始价值1计；调仓核算文件保留旧引擎gross/net字段，"
            "其中net仅指扣除声明额外摩擦，均不代表完整用户净账户。"
        )
        with st.expander("全部60条状态与完整结果"):
            st.dataframe(
                display(
                    all_rows[
                        [
                            "policy",
                            "horizon",
                            "friction_bps",
                            "engine_status",
                            "valid_through",
                            "full_period_completed",
                            "blocking_instrument",
                        ]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            csv_download("下载全部60条完整结果", display(all_rows), "QuantLab_全部60条研究情景.csv")
        with st.expander("复核证据与研究口径"):
            st.write(f"执行源码：{plan['code_head']}")
            st.write(f"独立复核：{proof['fingerprint']}")
            st.write(
                "已重建444,072条目标、1,278,503条原始标记及2,848,540条逐日持仓；复核新增回测0、训练0。"
            )
            for label, obj in (("独立复核", proof), ("情景证据清单", report)):
                st.download_button(
                    f"下载{label}",
                    json.dumps(obj, ensure_ascii=False, indent=2),
                    f"QuantLab_经济情景{label}.json",
                    "application/json",
                    on_click="ignore",
                )
            findings = PROJECT_ROOT / "docs/extended_economic_findings_zh.md"
            if findings.exists():
                st.download_button(
                    "下载中文研究说明",
                    findings.read_bytes(),
                    "QuantLab_经济情景研究说明.md",
                    "text/markdown",
                    on_click="ignore",
                )
    except Exception as exc:
        st.error(f"经济情景证据无法校验：{public_error(exc)}")
