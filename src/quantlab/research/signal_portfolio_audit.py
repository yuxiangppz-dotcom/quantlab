"""Price/context coverage for research intentions, never an executable simulation."""

from __future__ import annotations

import io
from collections import Counter
from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.data.storage import ParquetStorage
from quantlab.execution.rules import default_a_share_rule_book
from quantlab.portfolio.constructor import (
    FixedCountPortfolioConfig,
    construct_fixed_count_portfolio,
)
from quantlab.research.costs import estimate_research_order_components, load_research_cost_profile
from quantlab.research.round2_dataset import KEYS

BLOCKERS = [
    {
        "code": "historical_availability",
        "status": "unknown",
        "detail": "数据事后获取；历史修订、原始发布时间与全量代码沿革未完整认证。",
    },
    {
        "code": "effective_board_identity",
        "status": "unknown",
        "detail": "证券基础信息中的当前板块不能替代每个历史日期的板块身份。",
    },
    {
        "code": "suspension_and_st_coverage",
        "status": "unknown",
        "detail": "ST、停复牌原始记录已核对；没有记录不能认定非 ST 或全天可交易。",
    },
    {
        "code": "price_limits_and_fills",
        "status": "unknown",
        "detail": "日线和涨跌停价格不能证明排队成交、盘中价格笼子或例外规则满足。",
    },
    {
        "code": "board_lot_rule_coverage",
        "status": "partial",
        "detail": "现有规则解析器逐日清点覆盖；缺失规则不借用其他日期的规则。",
    },
    {
        "code": "complete_cost",
        "status": "unknown",
        "detail": "佣金与印花税为已声明情景；其他费用覆盖、滑点和分红税仍未知。",
    },
    {
        "code": "corporate_action_accounting",
        "status": "not_modeled",
        "detail": "复权价格不能替代送转、配股、分红、合并转换的持仓和现金事件。",
    },
    {
        "code": "cash_lots_and_unknown_exits",
        "status": "not_evaluated",
        "detail": "本次仅生成独立目标，不滚动现金或登记成交；无法退出必须保留未平仓或未知。",
    },
]


def research_authority():
    return {
        "execution_eligible": False,
        "performance_eligible": False,
        "complete_trading_cost_fen": None,
        "drawdown_target_passed": None,
    }


def score_targets(cross, column, contract):
    """Accept only score keys; neither price coverage nor labels change ranking."""
    inputs = cross[[*KEYS, column]].rename(columns={column: "alpha_score"}).copy()
    inputs["trade_date"] = pd.to_datetime(inputs.trade_date).dt.date
    config = contract["target_contract"]
    return construct_fixed_count_portfolio(
        inputs,
        inputs.trade_date.iloc[0],
        FixedCountPortfolioConfig(
            config["max_names"], config["score_direction"], config["gross_exposure"]
        ),
    )


def raw_context(ids, daily, limits, st, suspensions):
    """Explicit missingness plus positive raw observations, no tradability default."""
    result = pd.DataFrame(index=pd.Index(ids, name="instrument_id"))
    if daily is None:
        result["raw_close"] = np.nan
        result["valid_ohlc"] = False
    else:
        prices = daily.set_index("instrument_id").reindex(result.index)
        ohlc = prices[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        valid = np.isfinite(ohlc).all(axis=1) & ohlc.gt(0).all(axis=1)
        valid &= ohlc.high.ge(ohlc[["open", "low", "close"]].max(axis=1))
        valid &= ohlc.low.le(ohlc[["open", "high", "close"]].min(axis=1))
        result["raw_close"] = ohlc.close
        result["valid_ohlc"] = valid
    if limits is None:
        result["valid_limit_pair"] = False
    else:
        pair = limits.set_index("instrument_id").reindex(result.index)[["up_limit", "down_limit"]]
        pair = pair.apply(pd.to_numeric, errors="coerce")
        result["valid_limit_pair"] = (
            np.isfinite(pair).all(axis=1)
            & pair.gt(0).all(axis=1)
            & pair.up_limit.ge(pair.down_limit)
        )
    result["st_raw_record_count"] = (
        st.instrument_id.value_counts().reindex(result.index, fill_value=0)
        if st is not None
        else pd.Series(pd.NA, index=result.index, dtype="Int64")
    )
    for event in ("S", "R"):
        result[f"suspension_{event}_record_count"] = (
            suspensions.loc[suspensions.suspend_type.eq(event), "instrument_id"]
            .value_counts()
            .reindex(result.index, fill_value=0)
            if suspensions is not None
            else pd.Series(pd.NA, index=result.index, dtype="Int64")
        )
    result["market_accessibility"] = "unknown"
    result["execution_eligible"] = False
    return result


def rule_coverage(sessions):
    book = default_a_share_rule_book()
    rows = []
    for exchange, board in (
        ("SSE", "MAIN"),
        ("SSE", "STAR"),
        ("SZSE", "MAIN"),
        ("SZSE", "CHINEXT"),
    ):
        missing = [day for day in sessions if book.resolve(exchange, board, day) is None]
        rows.append(
            {
                "exchange": exchange,
                "declared_board": board,
                "sessions": len(sessions),
                "covered_sessions": len(sessions) - len(missing),
                "missing_sessions": len(missing),
                "first_missing": str(min(missing)) if missing else None,
                "last_missing": str(max(missing)) if missing else None,
                "historical_security_board_certified": False,
            }
        )
    return rows


def audit_portfolio_inputs(root, out, scores, contract, binding, *, progress=print):
    storage = ParquetStorage(root / "data/canonical")
    calendar = pd.read_parquet(io.BytesIO(binding.read(storage.calendar_path)))
    calendar["trade_date"] = pd.to_datetime(calendar.trade_date).dt.date
    cal = calendar.loc[calendar.exchange.isin(["SSE", "SZSE"])]
    if cal.duplicated(["exchange", "trade_date"]).any():
        raise DataValidationError("ambiguous portfolio audit calendar")
    flags = cal.pivot(index="trade_date", columns="exchange", values="is_open")
    relevant = flags.loc[date.fromisoformat(contract["signal_start"]) :]
    if (
        relevant.isna().any().any()
        or not relevant.SSE.eq(relevant.SZSE).all()
        or not relevant.isin([True, False, 0, 1]).all().all()
    ):
        raise DataValidationError("incomplete or conflicting audit calendar")
    sessions = list(flags.index[flags.SSE.eq(True) & flags.SZSE.eq(True)])
    positions = {day: i for i, day in enumerate(sessions)}
    cutoff = date.fromisoformat(contract["signal_end"])
    paths = {
        "daily": storage.daily_bars_path,
        "limits": storage.daily_price_limit_path,
        "st": storage.stock_st_v1_path,
        "suspensions": storage.suspensions_v1_path,
    }
    source_inventory = {}

    @lru_cache(maxsize=128)
    def table(kind, day):
        if day is None or day > cutoff:
            return None
        path = paths[kind](day)
        label = path.relative_to(root).as_posix()
        if not path.exists():
            source_inventory[label] = {"dataset": kind, "status": "missing"}
            return None
        data = pd.read_parquet(io.BytesIO(binding.read(path)), use_threads=False)
        if not set(KEYS).issubset(data.columns) or data[KEYS].isna().any().any():
            raise DataValidationError(f"invalid context keys: {label}")
        if not pd.to_datetime(data.trade_date).eq(pd.Timestamp(day)).all():
            raise DataValidationError(f"wrong context date: {label}")
        keys = KEYS if kind in ("daily", "limits") else [*KEYS, "source_record_id"]
        if data.duplicated(keys).any():
            raise DataValidationError(f"duplicate context rows: {label}")
        if kind == "suspensions" and not data.suspend_type.isin(["S", "R"]).all():
            raise DataValidationError(f"unknown S/R record type: {label}")
        source_inventory[label] = {"dataset": kind, "status": "present", "rows": len(data)}
        return data

    def shifted(day, count):
        if day not in positions:
            raise DataValidationError("score date absent from audit calendar")
        pos = positions[day] + count
        return sessions[pos] if pos < len(sessions) else None

    audit_dir = out / "context"
    audit_dir.mkdir()
    totals, target_rows, day_rows, monthly = Counter(), [], [], []
    current_month = None
    entry_sessions = set()
    for number, (stamp, cross) in enumerate(scores.groupby("trade_date", sort=True), 1):
        day, month = stamp.date(), stamp.strftime("%Y-%m")
        if current_month is not None and current_month != month:
            pd.concat(monthly, ignore_index=True).to_parquet(
                audit_dir / f"{current_month}.parquet", index=False
            )
            monthly = []
        current_month = month
        ids = cross.instrument_id.astype(str).tolist()
        evidence = cross[KEYS].copy().reset_index(drop=True)
        evidence["instrument_id"] = ids
        date_row = {"trade_date": str(day), "score_rows": len(cross)}
        for prefix, shift in [("entry", 1), *[(f"exit_{h}d", h + 1) for h in (5, 10, 20)]]:
            target_day = shifted(day, shift)
            if prefix == "entry" and target_day is not None:
                entry_sessions.add(target_day)
            context = raw_context(ids, *(table(kind, target_day) for kind in paths))
            evidence[f"{prefix}_date"] = pd.Timestamp(target_day) if target_day else pd.NaT
            evidence[f"{prefix}_beyond_data_cutoff"] = target_day is None or target_day > cutoff
            for column in (
                "raw_close",
                "valid_ohlc",
                "valid_limit_pair",
                "st_raw_record_count",
                "suspension_S_record_count",
                "suspension_R_record_count",
            ):
                evidence[f"{prefix}_{column}"] = context[column].to_numpy()
            for field in ("valid_ohlc", "valid_limit_pair"):
                count = int((~context[field]).sum())
                totals[f"{prefix}_{field}_missing_or_invalid"] += count
                date_row[f"{prefix}_{field}_missing_or_invalid"] = count
        evidence["execution_eligible"] = False
        evidence["market_accessibility"] = "unknown"
        monthly.append(evidence)
        lookup = evidence.set_index("instrument_id")
        for group in contract["groups"]:
            for horizon in contract["horizons"]:
                column = f"{group}_{horizon}d"
                target = score_targets(cross, column, contract)
                for position in target.positions:
                    item = lookup.loc[position.instrument_id]
                    target_rows.append(
                        {
                            "trade_date": stamp,
                            "group": group,
                            "horizon": horizon,
                            "instrument_id": position.instrument_id,
                            "target_weight": position.target_weight,
                            "cash_weight": target.cash_weight,
                            "entry_raw_price_present": bool(item.entry_valid_ohlc),
                            "exit_raw_price_present": bool(item[f"exit_{horizon}d_valid_ohlc"]),
                            "execution_eligible": False,
                        }
                    )
        day_rows.append(date_row)
        if number % 100 == 0:
            progress(f"portfolio input audit: {number} signal sessions", flush=True)
    if monthly:
        pd.concat(monthly, ignore_index=True).to_parquet(
            audit_dir / f"{current_month}.parquet", index=False
        )
    targets = pd.DataFrame(target_rows)
    targets.to_parquet(out / "hypothetical_targets.parquet", index=False)
    pd.DataFrame(day_rows).to_parquet(out / "daily_coverage.parquet", index=False)
    summaries = []
    for (group, horizon), data in targets.groupby(["group", "horizon"]):
        summaries.append(
            {
                "group": group,
                "horizon": int(horizon),
                "target_rows": len(data),
                "missing_entry_raw_price": int((~data.entry_raw_price_present).sum()),
                "missing_exit_raw_price": int((~data.exit_raw_price_present).sum()),
                "execution_eligible_rows": 0,
            }
        )
    profile = load_research_cost_profile(root / contract["cost_profile"])
    fee_examples = []
    # These are isolated notional illustrations, not rounded share orders or fills.
    for capital in contract["hypothetical_capital_cny"]:
        allocation_fen = int(
            capital
            * 100
            * contract["target_contract"]["gross_exposure"]
            / contract["target_contract"]["max_names"]
        )
        for asset in ("stock", "etf"):
            for side in ("buy", "sell"):
                for fee_day in (date(2023, 8, 25), date(2023, 8, 28)):
                    fee_examples.append(
                        {
                            "hypothetical_capital_cny": capital,
                            "hypothetical_allocation_fen": allocation_fen,
                            "share_quantity": None,
                            **estimate_research_order_components(
                                [allocation_fen],
                                asset_type=asset,
                                side=side,
                                trade_date=fee_day,
                                profile_path=root / contract["cost_profile"],
                            ),
                        }
                    )
    # A missing source appearing during the run must not silently change evidence.
    for name, item in source_inventory.items():
        if item["status"] == "missing" and (root / name).exists():
            raise DataValidationError(f"missing source appeared during audit: {name}")
    return {
        "row_coverage": dict(totals),
        "target_summary": summaries,
        "source_inventory": source_inventory,
        "rule_coverage": rule_coverage(sorted(entry_sessions)),
        "fee_profile_fingerprint": profile["profile_fingerprint"],
        "fee_examples": fee_examples,
        "blockers": BLOCKERS,
        **research_authority(),
        "next_step": "补齐历史身份、规则和费用/公司行动证据后，按已固定合约评价组合；不自动重训。",
    }
