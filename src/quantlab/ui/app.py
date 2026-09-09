"""Streamlit application for QuantLab Daily."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import streamlit as st

from quantlab.daily.experiments import load_baseline_view
from quantlab.daily.service import (
    generate_daily_snapshot,
    inspect_data_status,
    load_latest_snapshot,
)

st.set_page_config(page_title="QuantLab Daily", page_icon="📈", layout="wide")


@st.cache_data(show_spinner=False)
def _baseline_view() -> dict | None:
    return load_baseline_view()


def _latest() -> tuple[object | None, pd.DataFrame | None, pd.DataFrame | None]:
    snapshot = load_latest_snapshot()
    if snapshot is None:
        return None, None, None
    return snapshot, pd.read_csv(snapshot.ranking_path), pd.read_csv(snapshot.target_path)


def _format_pct(value: float) -> str:
    return f"{value:.2%}"


st.title("QuantLab Daily v1")
st.caption("本地日频研究与辅助决策工具；研究目标不是券商订单，未知证据不会显示为安全。")

page = st.sidebar.radio(
    "页面",
    ("数据状态与日报", "股票排名与因子", "回测与基准", "账户与参考计划"),
)

if page == "数据状态与日报":
    status = inspect_data_status()
    cols = st.columns(4)
    cols[0].metric("共同数据截止日", status["effective_as_of"] or "不可用")
    cols[1].metric("日历截止日", status["latest_calendar_date"] or "不可用")
    cols[2].metric("状态", status["status"])
    cols[3].metric(
        "缺口交易日",
        status["stale_open_sessions"]
        if status["stale_open_sessions"] is not None
        else "未知",
    )
    if status["issues"]:
        st.warning("；".join(status["issues"]))
    st.dataframe(
        pd.DataFrame.from_dict(status["datasets"], orient="index").reset_index(
            names="dataset"
        ),
        width="stretch",
        hide_index=True,
    )
    if st.button("生成/刷新日频报告", type="primary"):
        with st.spinner("正在读取必要 lookback 并生成报告…"):
            created = generate_daily_snapshot(date.fromisoformat(status["requested_as_of"]))
        st.success("已复用相同输入的缓存" if created.reused else "日报已生成")
        st.rerun()
    snapshot, _, target = _latest()
    if snapshot is None:
        st.info("尚无日报缓存。点击上方按钮后生成；页面刷新本身不会同步或重算。")
    else:
        report = snapshot.report
        st.subheader(f"缓存日报：{report['effective_as_of']}")
        if report["data_status"]["status"] != "complete":
            st.warning(
                f"这是 {report['effective_as_of']} 的离线结果，不是今天的收盘结果。"
            )
        st.write(report["ranking"]["score_interpretation"])
        st.dataframe(target, width="stretch", hide_index=True)
        st.download_button(
            "下载目标 CSV",
            snapshot.target_path.read_bytes(),
            file_name=snapshot.target_path.name,
            mime="text/csv",
        )
        st.download_button(
            "下载 HTML 日报",
            snapshot.html_path.read_bytes(),
            file_name=snapshot.html_path.name,
            mime="text/html",
        )

elif page == "股票排名与因子":
    snapshot, ranking, _ = _latest()
    if snapshot is None or ranking is None:
        st.info("请先在“数据状态与日报”页面显式生成报告。")
    else:
        st.caption(
            f"模型 {snapshot.report['model']['strategy_id']} · "
            f"状态 {snapshot.report['model']['model_status']} · "
            f"数据 {snapshot.report['effective_as_of']}"
        )
        selected_only = st.toggle("仅看目标股票")
        query = st.text_input("代码或名称筛选")
        shown = ranking
        if selected_only:
            shown = shown[shown["selected"]]
        if query:
            mask = shown["instrument_id"].str.contains(query, case=False, na=False)
            mask |= shown["name"].astype(str).str.contains(query, case=False, na=False)
            shown = shown[mask]
        st.dataframe(shown, width="stretch", hide_index=True, height=650)
        st.download_button(
            "下载完整排名 CSV",
            snapshot.ranking_path.read_bytes(),
            file_name=snapshot.ranking_path.name,
            mime="text/csv",
        )

elif page == "回测与基准":
    view = _baseline_view()
    if view is None:
        st.error("未找到通过 COMPLETED 标记的正式 baseline artifact。")
    else:
        st.caption(
            f"正式历史 artifact {view['schema']} / {view['run_id']} · "
            "以下指标从 manifest 约束的 daily_records 重新计算"
        )
        s = view["strategy_recovery_1"]
        c = view["control_recovery_1"]
        cols = st.columns(5)
        cols[0].metric("策略总收益", _format_pct(s["total_return_net"]))
        cols[1].metric("策略 CAGR", _format_pct(s["cagr_net"]))
        cols[2].metric("最大回撤", _format_pct(s["max_drawdown_net"]))
        cols[3].metric("Control 总收益", _format_pct(c["total_return_net"]))
        cols[4].metric(
            "相对 Control", _format_pct(view["active"]["cumulative_active_return"])
        )
        st.line_chart(view["curve"].set_index("trade_date")[["strategy", "control"]])
        st.error(
            "诚实结论：该研究示例策略相对同宇宙等权 Control 没有正 active return。"
        )
        st.warning(
            "Settlement recovery=1 与 recovery=0 都是假设情景，不是退市实际变现；"
            "终止上市公告覆盖有限，unknown 不是 safe。"
        )
        with st.expander("指标与披露"):
            serializable = {key: value for key, value in view.items() if key != "curve"}
            st.code(json.dumps(serializable, ensure_ascii=False, indent=2), language="json")

else:
    snapshot, _, target = _latest()
    st.subheader("账户持仓与参考调仓计划")
    st.info(
        "M1 已把研究目标与账户事实隔离。M3 将在本页提供账户 CSV 导入、现金/可卖数量、"
        "BUY/SELL/HOLD/NO_TRADE 参考计划；在此之前不会把目标列表冒充可执行订单。"
    )
    if snapshot is not None and target is not None:
        st.caption(f"当前只读研究目标（{snapshot.report['effective_as_of']}）")
        st.dataframe(target, width="stretch", hide_index=True)
