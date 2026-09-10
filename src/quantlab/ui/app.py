"""Streamlit application for QuantLab Daily."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import streamlit as st

from quantlab.daily.configuration import create_daily_user_config
from quantlab.daily.experiments import (
    load_baseline_view,
    load_latest_cadence_audit,
    load_latest_factor_view,
    load_latest_portfolio_audit,
)
from quantlab.daily.service import (
    PROJECT_ROOT,
    SHANGHAI,
    generate_daily_snapshot,
    inspect_data_status,
    load_latest_snapshot,
)
from quantlab.data.enrichment import dividend_context_warnings, inspect_enrichment_status
from quantlab.data.storage import ParquetStorage
from quantlab.personal import (
    build_plan_fill_comparison,
    build_reference_plan,
    build_tracking_valuation,
    create_demo_account,
    import_account_csv,
    import_manual_fills,
    list_accounts,
    load_effective_account,
    load_latest_plan,
    load_tracking_summary,
    preview_manual_fills,
)
from quantlab.research.forward_shadow import latest_forward_shadow

st.set_page_config(page_title="QuantLab Daily", page_icon="📈", layout="wide")


@st.cache_data(show_spinner=False)
def _baseline_view() -> dict | None:
    return load_baseline_view()


@st.cache_data(show_spinner=False)
def _factor_view() -> dict | None:
    return load_latest_factor_view()


@st.cache_data(show_spinner=False)
def _portfolio_audit() -> dict | None:
    return load_latest_portfolio_audit()


@st.cache_data(show_spinner=False)
def _cadence_audit() -> dict | None:
    return load_latest_cadence_audit()


@st.cache_data(show_spinner=False)
def _shadow_view() -> list[dict]:
    return latest_forward_shadow()


def _latest() -> tuple[object | None, pd.DataFrame | None, pd.DataFrame | None]:
    snapshot = load_latest_snapshot()
    if snapshot is None:
        return None, None, None
    return snapshot, pd.read_csv(snapshot.ranking_path), pd.read_csv(snapshot.target_path)


def _format_pct(value: float) -> str:
    return f"{value:.2%}"


st.title("QuantLab Daily v1.1")
st.caption("本地日频研究与辅助决策工具；研究目标不是券商订单，未知证据不会显示为安全。")

page = st.sidebar.radio(
    "页面",
    ("数据状态与日报", "股票排名与因子", "回测与基准", "账户与参考计划"),
)

if page == "数据状态与日报":
    status = inspect_data_status()
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    effective = date.fromisoformat(status["effective_as_of"]) if status["effective_as_of"] else None
    enrichment = inspect_enrichment_status(storage, effective)
    cols = st.columns(4)
    cols[0].metric("共同数据截止日", status["effective_as_of"] or "不可用")
    cols[1].metric("日历截止日", status["latest_calendar_date"] or "不可用")
    cols[2].metric("状态", status["status"])
    cols[3].metric(
        "缺口交易日",
        status["stale_open_sessions"] if status["stale_open_sessions"] is not None else "未知",
    )
    if status["issues"]:
        st.warning("；".join(status["issues"]))
    st.dataframe(
        pd.DataFrame.from_dict(status["datasets"], orient="index").reset_index(names="dataset"),
        width="stretch",
        hide_index=True,
    )
    st.subheader("增强数据可用性")
    st.dataframe(
        pd.DataFrame.from_dict(enrichment, orient="index").reset_index(names="endpoint"),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "fina_indicator_vip 仅从本地首次观察日向前使用；报告期不是公开日，"
        "当前快照不用于回填历史 PIT 因子。dividend 仅作人工核对提示。"
    )
    if st.button("生成/刷新日频报告", type="primary"):
        with st.spinner("正在读取必要 lookback 并生成报告…"):
            created = generate_daily_snapshot(date.fromisoformat(status["requested_as_of"]))
        st.success("已复用相同输入的缓存" if created.reused else "日报已生成")
        st.rerun()
    with st.expander("新建版本化日频配置"):
        strategy = st.selectbox(
            "策略",
            ("baseline_reversal", "transparent_combo_candidate"),
            format_func=lambda value: {
                "baseline_reversal": "研究示例 baseline（已观察）",
                "transparent_combo_candidate": "透明多因子 candidate（未晋级）",
            }[value],
        )
        target_count = st.number_input("目标股票数", min_value=1, max_value=100, value=20)
        cap_pct = st.number_input(
            "单票目标上限（%）", min_value=0.1, max_value=20.0, value=5.0, step=0.1
        )
        allowed_boards = st.multiselect(
            "允许板块", ("主板", "创业板", "科创板"), default=("主板", "创业板", "科创板")
        )
        st.caption("每次保存生成不可变配置版本；不覆盖 frozen baseline，也不冒充历史可比结果。")
        if st.button("保存新配置并生成日报"):
            try:
                config_path = create_daily_user_config(
                    strategy=strategy,
                    target_count=int(target_count),
                    max_weight_per_name=float(cap_pct) / 100,
                    allowed_boards=list(allowed_boards),
                )
                created = generate_daily_snapshot(
                    date.fromisoformat(status["requested_as_of"]), config_path=config_path
                )
                st.success(f"配置 {created.report['model']['config_id']} 已生成")
                st.rerun()
            except Exception as exc:
                st.error(f"配置或日报生成失败：{exc}")
    snapshot, _, target = _latest()
    if snapshot is None:
        st.info("尚无日报缓存。点击上方按钮后生成；页面刷新本身不会同步或重算。")
    else:
        report = snapshot.report
        st.subheader(f"缓存日报：{report['effective_as_of']}")
        if report["data_status"]["status"] != "complete":
            st.warning(f"这是 {report['effective_as_of']} 的离线结果，不是今天的收盘结果。")
        st.write(report["ranking"]["score_interpretation"])
        st.write(f"策略状态：`{report['model']['model_status']}`")
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
        contribution_columns = snapshot.report["ranking"].get("factor_contribution_columns", [])
        available_contributions = [column for column in contribution_columns if column in ranking]
        if available_contributions:
            st.subheader("透明组合因子贡献")
            st.caption("各行贡献之和等于 transparent_combo_v1；这是分数组成，不是收益归因。")
            st.dataframe(
                ranking.loc[ranking["selected"], ["instrument_id", *available_contributions]],
                width="stretch",
                hide_index=True,
            )
        if {"return_20d", "transparent_combo_v1"}.issubset(ranking.columns):
            baseline_ids = set(
                ranking.sort_values(["return_20d", "instrument_id"], kind="mergesort").head(20)[
                    "instrument_id"
                ]
            )
            candidate_ids = set(
                ranking.sort_values(
                    ["transparent_combo_v1", "instrument_id"],
                    ascending=[False, True],
                    kind="mergesort",
                ).head(20)["instrument_id"]
            )
            st.metric("Candidate 与 baseline Top20 重合", f"{len(baseline_ids & candidate_ids)}/20")
        research = _factor_view()
        if research is not None:
            st.subheader("有限因子研究批次")
            st.caption(
                f"run {research['run_id']} · signal end {research['signal_end']} · "
                "RankIC 诊断；成本与 Control 的回溯审计在下方单独展示，仍未晋级"
            )
            rows = []
            for item in research["registry"]:
                rows.append(
                    {
                        "factor": item["factor_id"],
                        "status": item["status"],
                        "discovery_ic": item["discovery"]["mean_rank_ic"],
                        "validation_ic": item["validation"]["mean_rank_ic"],
                        "observed_ic": item["test_observed"]["mean_rank_ic"],
                    }
                )
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.info(
                f"Qlib: {research['qlib']['status']}；LightGBM: {research['lightgbm']['status']}"
            )
        cadence = _cadence_audit()
        if cadence is not None:
            st.subheader("Daily 与 Weekly 信号诊断")
            st.caption(
                f"Discovery only · {cadence['period'][0]} 至 {cadence['period'][1]} · "
                "没有用 Validation/Test 自由调频"
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {"factor": factor, **values}
                        for factor, values in cadence["comparison"].items()
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        shadows = _shadow_view()
        if shadows:
            st.subheader("最新 Forward Shadow")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "model": item["model"]["model_id"],
                            "version": item["model"]["version"],
                            "status": item["model"]["status"],
                            "trade_date": item["trade_date"],
                            "target_count": item["target_count"],
                            "label_status": item["label"]["status"],
                            "前瞻时间资格": item["temporal_admission"]["status"],
                            "fingerprint": item["prediction_fingerprint"][:12],
                        }
                        for item in shadows
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
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
        cols[4].metric("相对 Control", _format_pct(view["active"]["cumulative_active_return"]))
        st.line_chart(view["curve"].set_index("trade_date")[["strategy", "control"]])
        st.error("诚实结论：该研究示例策略相对同宇宙等权 Control 没有正 active return。")
        st.warning(
            "Settlement recovery=1 与 recovery=0 都是假设情景，不是退市实际变现；"
            "终止上市公告覆盖有限，unknown 不是 safe。"
        )
        with st.expander("指标与披露"):
            serializable = {key: value for key, value in view.items() if key != "curve"}
            st.code(json.dumps(serializable, ensure_ascii=False, indent=2), language="json")
        audit = _portfolio_audit()
        if audit is not None:
            st.subheader("有限候选 Portfolio Translation Audit")
            st.caption(
                f"{audit['period'][0]} 至 {audit['period'][1]} · {audit['history_status']} · "
                f"Control CAGR {_format_pct(audit['control_cagr'])}"
            )
            st.dataframe(pd.DataFrame(audit["rows"]), width="stretch", hide_index=True)
            st.warning("这些区间已经被观察；表中相对表现不是 fresh OOS，也没有触发策略晋级。")

else:
    snapshot, _, target = _latest()
    st.subheader("账户持仓与参考调仓计划")
    st.warning("本页只生成参考计划，不会向券商下单。T+1 状态、次日价格和完整费用仍需人工复核。")
    with st.expander("账户 CSV 格式与导入", expanded=not list_accounts()):
        template = (
            "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
            "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
            "my_account,manual_tracking,2026-09-10T09:00:00+08:00,200000.00,"
            "000001.SZ,1000,1000,10.50,none_declared\n"
        )
        st.download_button(
            "下载账户模板 CSV", template.encode(), "quantlab_account_template.csv", "text/csv"
        )
        uploaded = st.file_uploader("选择完整账户快照 CSV", type="csv")
        if uploaded is not None:
            raw = uploaded.getvalue()
            try:
                preview = pd.read_csv(pd.io.common.BytesIO(raw))
                st.dataframe(preview, width="stretch", hide_index=True)
                if st.button("确认导入账户快照"):
                    path = import_account_csv(raw)
                    st.success(f"已原子写入 {path}")
                    st.rerun()
            except Exception as exc:
                st.error(f"CSV 无法导入：{exc}")
        demo_cash = st.number_input(
            "演示账户初始现金（CNY）", min_value=0.0, value=200000.0, step=10000.0
        )
        if st.button("创建/重置演示账户"):
            path = create_demo_account(cash_cny=f"{demo_cash:.2f}")
            st.success(f"演示账户已写入 {path}")
            st.rerun()

    accounts = list_accounts()
    if not accounts:
        st.info("尚无账户。请显式创建演示账户或导入完整账户快照。")
    else:
        account_id = st.selectbox("账户", accounts)
        account = load_effective_account(account_id)
        tracking = load_tracking_summary(account_id)
        positions = pd.DataFrame(account["positions"])
        cols = st.columns(4)
        cols[0].metric("账户模式", account["account_mode"])
        cols[1].metric("现金", f"¥{Decimal(account['cash_fen']) / 100:,.2f}")
        cols[2].metric("持仓数", len(account["positions"]))
        cols[3].metric("账户时间", account["as_of"])
        if account["account_mode"] == "demo_simulation":
            st.info("这是演示账户，不是用户实际资产。")
        corporate_actions = dividend_context_warnings(
            ParquetStorage(PROJECT_ROOT / "data" / "canonical"),
            {item["instrument_id"] for item in account["positions"]},
            as_of=datetime.now(SHANGHAI).date(),
        )
        if corporate_actions:
            st.error("持仓附近存在分红/送转日期上下文，真实现金和股数需要人工核对。")
            st.dataframe(pd.DataFrame(corporate_actions), width="stretch", hide_index=True)
        if tracking["event_count"]:
            st.success(
                f"已通过同一执行账本回放 {tracking['event_count']} 笔人工成交；"
                f"累计实际费用 ¥{Decimal(tracking['total_fee_fen']) / 100:,.2f}。"
            )
            valuation = build_tracking_valuation(account_id)
            if valuation["status"] == "complete_reference_mark_to_market":
                cols = st.columns(2)
                cols[0].metric(
                    "参考盯市净值",
                    f"¥{Decimal(valuation['current_nav_fen']) / 100:,.2f}",
                )
                cols[1].metric("参考盯市变化", f"{valuation['reference_return']:.2%}")
                st.caption(
                    "从首次 journal 的最近完整 raw close 参考锚点计算；"
                    "不是经公司行动和外部现金流验证的业绩声明。"
                )
            else:
                st.warning(f"组合表现暂不可计算：{valuation['status']}")
        if not positions.empty:
            positions["reference_cost_cny"] = positions["reference_cost_fen"].map(
                lambda value: None if pd.isna(value) else float(value) / 100
            )
            st.dataframe(positions, width="stretch", hide_index=True)
        if account["account_mode"] != "manual_tracking":
            st.info("演示账户不会接收人工真实成交；请导入 manual_tracking 账户。")
        else:
            with st.expander("人工成交：预览后导入"):
                fill_template = (
                    "account_id,broker_trade_id,trade_date,reported_at,instrument_id,side,"
                    "quantity,price_cny,gross_notional_cny,fee_cny\n"
                    f"{account_id},broker_trade_001,2026-09-10,"
                    "2026-09-10T15:10:00+08:00,000001.SZ,BUY,100,10.00,1000.00,5.00\n"
                )
                st.download_button(
                    "下载人工成交模板 CSV",
                    fill_template.encode(),
                    "quantlab_manual_fills_template.csv",
                    "text/csv",
                )
                fill_upload = st.file_uploader(
                    "选择券商人工成交 CSV", type="csv", key="manual_fill_upload"
                )
                if fill_upload is not None:
                    fill_raw = fill_upload.getvalue()
                    fill_sha = hashlib.sha256(fill_raw).hexdigest()
                    if st.button("预览并校验成交"):
                        try:
                            preview = preview_manual_fills(account_id, fill_raw)
                            st.session_state["fill_preview"] = preview
                            st.session_state["fill_preview_sha"] = fill_sha
                        except Exception as exc:
                            st.error(f"成交预览失败：{exc}")
                    preview = st.session_state.get("fill_preview")
                    if preview and st.session_state.get("fill_preview_sha") == fill_sha:
                        st.write(
                            f"新增 {preview['accepted_count']} 笔，重复 "
                            f"{preview['duplicate_count']} 笔；预览后现金 "
                            f"¥{Decimal(preview['cash_fen']) / 100:,.2f}。"
                        )
                        preview_rows = [
                            {
                                key: item[key]
                                for key in (
                                    "fill_id",
                                    "trade_date",
                                    "instrument_id",
                                    "side",
                                    "quantity",
                                    "price",
                                    "gross_notional_fen",
                                    "fee_fen",
                                )
                            }
                            for item in preview["events"]
                        ]
                        st.dataframe(pd.DataFrame(preview_rows), width="stretch", hide_index=True)
                        if st.button("确认导入已预览成交"):
                            try:
                                path, result = import_manual_fills(account_id, fill_raw)
                                st.success(f"已导入 {result['accepted_count']} 笔；journal {path}")
                                st.session_state.pop("fill_preview", None)
                                st.session_state.pop("fill_preview_sha", None)
                                st.rerun()
                            except Exception as exc:
                                st.error(f"成交导入失败：{exc}")
        if tracking["fills"]:
            with st.expander("已入账人工成交"):
                st.dataframe(pd.DataFrame(tracking["fills"]), width="stretch", hide_index=True)
        if st.button("生成下一交易日参考计划", type="primary"):
            try:
                json_path, csv_path, payload = build_reference_plan(account_id)
                st.success(f"参考计划已生成：{payload['plan_id'][:12]}")
            except Exception as exc:
                st.error(f"计划未生成：{exc}")
        saved_plan = load_latest_plan(
            account_id, account_fingerprint=account["account_fingerprint"]
        )
        if saved_plan is not None:
            path, payload = saved_plan
        else:
            path = None
            payload = None
        if path is not None and payload is not None:
            st.error(
                f"状态：{payload['status']}；execution_confirmed=false；"
                f"信号日 {payload['signal_date']}，拟用于 "
                f"{payload['intended_next_session']} 复核。"
            )
            if payload.get("candidate_warning"):
                st.error(payload["candidate_warning"])
            plan = pd.DataFrame(payload["rows"])
            st.dataframe(plan, width="stretch", hide_index=True)
            csv_path = path.with_suffix(".csv")
            st.download_button("下载参考计划 CSV", csv_path.read_bytes(), csv_path.name, "text/csv")
            with st.expander("已知限制"):
                for limitation in payload["known_limitations"]:
                    st.write(f"- {limitation}")
        comparison = build_plan_fill_comparison(account_id)
        if comparison is not None:
            st.subheader("参考计划与实际成交差异")
            st.caption(
                f"计划 {comparison['plan_id'][:12]} · 交易日 {comparison['intended_trade_date']}"
            )
            st.dataframe(pd.DataFrame(comparison["rows"]), width="stretch", hide_index=True)
    if snapshot is None:
        st.info("尚无日频报告；参考计划需要先生成日报。")
