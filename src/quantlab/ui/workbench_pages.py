"""Guided local product pages built on the existing domain services."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

from quantlab.daily.service import SHANGHAI
from quantlab.personal import (
    import_manual_cash_flows,
    inspect_performance_inputs,
    list_accounts,
    load_effective_account,
    load_latest_valuation_checkpoint,
    load_tracking_summary,
    materialize_valuation_checkpoint,
    preview_manual_cash_flows,
    replay_account_at,
)
from quantlab.personal.tracking import CASH_FLOW_COLUMNS
from quantlab.research.forward_shadow import evaluate_matured_forward_shadows
from quantlab.ui.workbench import (
    DATA_STATUS_LABELS,
    csv_template,
    preview_matches,
    public_error,
    read_workbench_status,
    register_current_shadow,
)


def render_start():
    state = read_workbench_status()
    st.subheader("从这里开始")
    st.write("先确认数据，再查看组合；需要记录自己的持仓时，再导入账户与券商成交。")
    for area, error in state["errors"].items():
        label = {"data": "数据", "report": "日报", "shadow": "前瞻记录", "accounts": "账户"}[area]
        st.error(f"{label}无法读取或校验：{error}")
    cols = st.columns(4)
    data = state["data"] or {}
    cols[0].metric("行情截止", data.get("effective_as_of") or "未知")
    cols[1].metric("日报日期", (state["report"] or {}).get("effective_as_of") or "尚未生成")
    shadow = state["shadow"]
    cols[2].metric(
        "有效前瞻记录",
        sum(row["prediction_count"] - row["excluded_prediction_count"] for row in shadow)
        if shadow is not None
        else "未知",
    )
    cols[3].metric(
        "已保存账户", len(state["accounts"]) if state["accounts"] is not None else "未知"
    )
    if data:
        st.info(DATA_STATUS_LABELS.get(data["status"], data["status"]))
    st.markdown(
        "1. **数据状态与日报**：检查截止日期；需要时确认更新，再生成日报。\n"
        "2. **股票排名与因子**：查看目标组合、分数和风险提示；可下载结果。\n"
        "3. **前瞻观察**：当天 16:00 至午夜前登记预测，之后等待标签成熟。\n"
        "4. **账户与参考计划**：先用演示账户熟悉界面；自己的账户需填完整快照。\n"
        "5. **资金流水与估值**：记录实际出入金、保存估值，并核对现金和持仓。"
    )
    st.warning(
        "策略仍处于研究观察阶段。参考计划需要人工复核；本工具没有券商下单入口。"
        "账户估值尚未覆盖完整公司行动，不能据此认定策略收益。"
    )
    with st.expander("遇到问题时怎么处理", expanded=False):
        st.write("数据日期落后：去数据页面更新；交易日历未知：先更新日历，不猜测休市日。")
        st.write("不能登记前瞻：核对信号日期和生成时间，过期记录保留但不补记。")
        st.write("账户导入报错：使用下载的空白模板，填写真实记录并保留全部表头。")
        st.write("参考计划被阻止：逐项核对账户时间、原始价格、可卖数量及规则证据。")
    guide = Path(__file__).resolve().parents[3] / "docs" / "quickstart_zh.md"
    if guide.exists():
        st.download_button(
            "下载中文使用说明", guide.read_bytes(), "QuantLab使用说明.md", "text/markdown"
        )
    if st.button("重新检查本地状态"):
        st.cache_data.clear()
        st.rerun()


def render_data_update():
    with st.expander("更新本地市场数据", expanded=False):
        st.write(
            "补齐所选日期之前最近最多 5 个交易日的缺失行情、指数和交易状态，"
            "刷新股票基础信息，并查询未来 35 天交易日历。已有日线分区保留。"
        )
        through = st.date_input(
            "更新截至", value=datetime.now(SHANGHAI).date(), max_value=datetime.now(SHANGHAI).date()
        )
        confirmed = st.checkbox("确认调用已配置的数据源，并写入本地正式数据目录")
        if st.button("开始更新数据", disabled=not confirmed):
            try:
                from quantlab.daily.service import PROJECT_ROOT
                from quantlab.daily.update import run_incremental_update
                from quantlab.data import ParquetStorage, TushareProvider

                with st.spinner("正在按限定范围补齐数据…"):
                    result = run_incremental_update(
                        TushareProvider(),
                        ParquetStorage(PROJECT_ROOT / "data" / "canonical"),
                        through,
                        include_context=True,
                        include_enrichment=True,
                    )
                st.session_state["last_data_update"] = result.to_dict()
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"更新未完成：{public_error(exc)}。可在问题解决后重试，已有分区会保留。")
        result = st.session_state.get("last_data_update")
        if result:
            if result["stop_reason"]:
                st.warning(f"更新在 {result['stopped_at']} 停止：{result['stop_reason']}")
            else:
                st.success(f"本次补齐 {len(result['core_synced_sessions'])} 个交易日行情。")
            if result["older_missing_partition_sessions"]:
                st.warning(
                    f"更早历史仍有 {result['older_missing_partition_sessions']} 个分区缺口，"
                    "未扩大下载范围；需要另行修复。"
                )
            with st.expander("查看更新范围与结果"):
                st.json(result)


def render_shadow():
    st.subheader("前瞻观察")
    st.write("先保存当时的预测，等未来交易日到来后再评估。旧版、超时和缺失标签分别显示。")
    state = read_workbench_status()
    for area in ("report", "shadow"):
        if area in state["errors"]:
            st.error(f"记录无法校验：{state['errors'][area]}")
            return
    admission = state["shadow_admission"]
    st.info(admission["reason"])
    left, right = st.columns(2)
    if left.button("登记当天前瞻预测", disabled=not admission["eligible"], type="primary"):
        try:
            results = register_current_shadow()
            st.session_state["shadow_notice"] = (
                f"处理 {len(results)} 个模型，"
                f"其中 {sum(result.reused for result in results)} 个复用首次记录。"
            )
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            st.error(f"登记未完成：{public_error(exc)}")
    if right.button("检查已成熟标签"):
        try:
            results = evaluate_matured_forward_shadows()
            st.session_state["shadow_notice"] = f"完成检查，本次得到 {len(results)} 份评估记录。"
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            st.error(f"评估未完成：{public_error(exc)}")
    if notice := st.session_state.get("shadow_notice"):
        st.success(notice)
    rows = [
        {
            "模型": row["model_id"],
            "版本": row["model_version"],
            "有效登记": row["prediction_count"] - row["excluded_prediction_count"],
            "已排除": row["excluded_prediction_count"],
            "等待成熟": row["pending_prediction_count"],
            "评估完整": row["complete_evaluation_count"],
            "标签缺失": row["incomplete_evaluation_count"],
        }
        for row in state["shadow"] or []
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.info("尚无预测记录。当天完整日报生成后，可在允许的时段登记。")
    st.caption("20 交易日标签会重叠，记录数不是独立样本数，也不能拼接为真实账户收益曲线。")


def render_cash_and_valuation():
    st.subheader("资金流水与估值")
    accounts = list_accounts()
    if not accounts:
        st.info("请先到账户页面创建演示账户，或导入自己的完整账户快照。")
        return
    account_id = st.selectbox("账户", accounts, key="cash_account")
    try:
        account = load_effective_account(account_id)
        tracking = load_tracking_summary(account_id)
    except Exception as exc:
        st.error(f"账户无法回放：{public_error(exc)}")
        return
    st.metric("当前账面现金", f"¥{Decimal(account['cash_fen']) / 100:,.2f}")
    st.caption(
        f"经济事实截止：{tracking['latest_economic_fact_at']}；录入截止：{tracking['latest_reported_at']}"
    )
    if not tracking["intraday_timing_eligible"]:
        st.warning("存在旧版时间未验证记录，不能用于精确的日内收益归因。")
    if account["account_mode"] == "manual_tracking":
        st.download_button(
            "下载出入金空白模板",
            csv_template(CASH_FLOW_COLUMNS),
            "quantlab_cash_flows.csv",
            "text/csv",
        )
        st.write(
            "DEPOSIT 表示入金，WITHDRAWAL 表示出金；金额均填正数。发生时间和录入时间需含时区。"
        )
        uploaded = st.file_uploader("选择真实出入金记录 CSV", type="csv", key="cash_flow_upload")
        if uploaded is not None:
            raw = uploaded.getvalue()
            digest = hashlib.sha256(raw).hexdigest()
            if st.button("预览并校验出入金"):
                st.session_state.pop("cash_preview", None)
                try:
                    st.session_state["cash_preview"] = preview_manual_cash_flows(account_id, raw)
                    st.session_state["cash_preview_sha"] = digest
                    st.session_state["cash_preview_account"] = account["account_fingerprint"]
                except Exception as exc:
                    st.error(f"预览失败：{public_error(exc)}")
            preview = st.session_state.get("cash_preview")
            if preview_matches(
                preview,
                digest,
                account,
                st.session_state.get("cash_preview_sha"),
                st.session_state.get("cash_preview_account"),
            ):
                st.write(
                    f"新增 {preview['accepted_count']} 笔；重复 {preview['duplicate_count']} 笔。"
                )
                st.dataframe(pd.DataFrame(preview["cash_flows"]), hide_index=True, width="stretch")
                if st.button("确认导入已预览出入金"):
                    try:
                        import_manual_cash_flows(account_id, raw)
                        st.session_state.pop("cash_preview", None)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"导入失败：{public_error(exc)}")
    else:
        st.info("演示账户不接收真实出入金。")
    if tracking["cash_flows"]:
        with st.expander("已入账出入金"):
            st.dataframe(pd.DataFrame(tracking["cash_flows"]), hide_index=True, width="stretch")
    st.divider()
    st.write("估值使用已校验日报的当日原始收盘价。它是账户估值记录，尚不是完整收益报表。")
    if st.button("保存当前账户估值"):
        try:
            _, _, reused = materialize_valuation_checkpoint(account_id)
            st.success("已复用相同估值记录。" if reused else "估值已保存。")
        except Exception as exc:
            st.error(f"无法保存估值：{public_error(exc)}")
    try:
        saved = load_latest_valuation_checkpoint(account_id)
        if saved:
            path, value = saved
            if value["account_fingerprint"] != account["account_fingerprint"]:
                st.warning("这份估值对应此前的账户状态；请保存新的估值后再核对。")
            st.metric("已保存估值", f"¥{Decimal(value['nav_fen']) / 100:,.2f}")
            st.caption(f"价格日期：{value['price_date']}；请人工核对分红、送转及券商持仓。")
            st.download_button(
                "下载估值记录", path.read_bytes(), "quantlab_valuation.json", "application/json"
            )
    except Exception as exc:
        st.error(f"已保存估值无法校验：{public_error(exc)}")
    render_historical_account(account_id, account)
    render_performance_inputs(account_id, account)


def render_performance_inputs(account_id, account):
    with st.expander("为什么目前还不能计算可信收益"):
        st.write("检查成交、出入金、估值和对账证据。单项通过不代表收益记录已经完整。")
        query_key = (account_id, account["account_fingerprint"])
        if st.button("检查收益计算条件"):
            st.session_state.pop("performance_inputs_result", None)
            try:
                st.session_state["performance_inputs_result"] = inspect_performance_inputs(
                    account_id
                )
                st.session_state["performance_inputs_query"] = query_key
            except Exception as exc:
                st.error(f"收益证据无法校验：{public_error(exc)}")
        result = st.session_state.get("performance_inputs_result")
        if result and st.session_state.get("performance_inputs_query") == query_key:
            st.warning("目前不能生成可信的账户收益率。请按下面的具体缺口补齐证据。")
            st.caption(f"上次检查时间：{result['checked_at']}；记录变化后请重新检查。")
            names = {"pass": "本项通过", "unknown": "证据缺失", "blocked": "尚未满足"}
            st.dataframe(
                pd.DataFrame(
                    [
                        {"状态": names[row["status"]], "说明": row["detail"]}
                        for row in result["checks"]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            import json

            st.download_button(
                "下载收益条件检查",
                json.dumps(result, ensure_ascii=False, indent=2).encode(),
                "quantlab_performance_inputs.json",
                "application/json",
            )


def render_historical_account(account_id, account):
    with st.expander("查看历史时点的现金与持仓"):
        st.write(
            "按目前已录入的真实发生时间回放，包含事后补录的记录；"
            "不代表当时已经获知全部事实，也不是收益报表。"
        )
        default_at = datetime.now(SHANGHAI).replace(microsecond=0)
        day = st.date_input(
            "回放日期",
            value=default_at.date(),
            max_value=datetime.now(SHANGHAI).date(),
            key=f"history_day_{account_id}",
        )
        moment = st.time_input(
            "截止时间（北京时间，包含该时刻）",
            value=default_at.time(),
            key=f"history_time_{account_id}",
        )
        basis = st.text_input(
            "历史期初指纹（留空使用当前起点）", key=f"history_basis_{account_id}"
        ).strip()
        cutoff = datetime.combine(day, moment, tzinfo=SHANGHAI)
        query_key = (account_id, account["account_fingerprint"], cutoff.isoformat(), basis)
        if st.button("回放指定历史时点"):
            st.session_state.pop("history_result", None)
            try:
                st.session_state["history_result"] = replay_account_at(
                    account_id,
                    cutoff,
                    opening_fingerprint=basis or None,
                )
                st.session_state["history_query"] = query_key
            except Exception as exc:
                st.error(f"历史回放无法完成：{public_error(exc)}")
        result = st.session_state.get("history_result")
        if result and st.session_state.get("history_query") == query_key:
            if result["status"] == "blocked":
                reasons = {
                    "cutoff_precedes_opening_basis": "查询时间早于期初快照，请选择更早的已保存起点",
                    "legacy_fill_execution_time_unknown": "旧成交缺少真实发生时刻",
                    "legacy_cash_flow_effective_time_unknown": "旧出入金缺少真实生效时刻",
                    "calendar_unavailable": "没有交易日历",
                    "calendar_does_not_cover_basis_to_cutoff": "交易日历未覆盖回放区间",
                    "calendar_has_unverified_days_inside_replay_interval": (
                        "区间内有未经验证的日历日期"
                    ),
                }
                for reason in result["blocked_reasons"]:
                    st.warning(reasons.get(reason, reason))
            else:
                st.metric("该时点账面现金", f"¥{Decimal(result['cash_fen']) / 100:,.2f}")
                st.caption(
                    f"纳入 {len(result['included_event_ids'])} 笔；"
                    f"其中 {result['late_reported_event_count']} 笔是在截止时间之后补录。"
                )
                st.dataframe(pd.DataFrame(result["positions"]), hide_index=True, width="stretch")
            import json

            st.download_button(
                "下载本次历史回放",
                json.dumps(result, ensure_ascii=False, indent=2).encode(),
                "quantlab_historical_account.json",
                "application/json",
            )
