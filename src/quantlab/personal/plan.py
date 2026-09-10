"""Conservative next-session reference rebalance plans."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

from quantlab.daily.integrity import load_validated_latest_snapshot
from quantlab.daily.service import DEFAULT_PRODUCT_ROOT, PROJECT_ROOT
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import (
    DEFAULT_ACCOUNT_ROOT,
    atomic_json,
    atomic_text,
)


def _fen_from_price(value: object) -> int:
    price = Decimal(str(value))
    fen = (price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if price <= 0 or fen <= 0:
        raise ValueError(f"invalid reference close: {value}")
    return int(fen)


def _quantity_grid(board: str) -> tuple[int, int, str] | None:
    if board == "科创板":
        return 200, 1, "daily_mvp_assumption_star_min200_step1"
    if board in {"主板", "创业板"}:
        return 100, 100, "daily_mvp_assumption_a_share_lot100"
    return None


def _floor_buy(raw: int, minimum: int, step: int) -> int:
    if raw < minimum:
        return 0
    return minimum + ((raw - minimum) // step) * step


def _estimated_commission_fen(notional_fen: int) -> int:
    rate = Decimal("0.000086") if notional_fen <= 50_000_000 else Decimal("0.00008")
    variable = (Decimal(notional_fen) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(500, int(variable))


def load_latest_plan(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    account_fingerprint: str | None = None,
) -> tuple[Path, dict] | None:
    """Read the newest complete plan for one account without recomputing it."""
    plan_root = account_root / account_id / "plans"
    candidates = list(plan_root.glob("*/*/plan.json")) if plan_root.exists() else []
    candidates += list(plan_root.glob("*/plan.json")) if plan_root.exists() else []
    candidates = sorted(candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True)
    if not candidates:
        return None
    for path in candidates:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("account_id") != account_id:
            raise ValueError("stored plan account binding does not match its path")
        if account_fingerprint and payload.get("account_fingerprint") != account_fingerprint:
            continue
        csv_path = path.with_suffix(".csv")
        expected_csv = payload.get("csv_sha256")
        if expected_csv is not None:
            if not csv_path.exists():
                raise ValueError("stored plan CSV is missing")
            actual_csv = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            if actual_csv != expected_csv:
                raise ValueError("stored plan CSV fingerprint mismatch")
        return path, payload
    return None


def build_reference_plan(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    storage: ParquetStorage | None = None,
) -> tuple[Path, Path, dict]:
    """Build an auditable plan that is explicitly not submission-ready."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    from quantlab.personal.tracking import load_effective_account

    account = load_effective_account(account_id, account_root=account_root, storage=storage)
    snapshot = load_validated_latest_snapshot(product_root)
    if snapshot is None:
        raise FileNotFoundError("no daily snapshot; run `quantlab daily` first")
    signal_date = date.fromisoformat(snapshot.report["effective_as_of"])
    next_session_value = snapshot.report.get("next_known_open_session")
    if not next_session_value:
        raise ValueError("daily snapshot has no verified next open session")
    intended_session = date.fromisoformat(next_session_value)
    latest_fill = account.get("latest_fill_trade_date")
    if latest_fill is not None and date.fromisoformat(latest_fill) >= intended_session:
        raise ValueError(
            "daily snapshot is stale relative to imported fills; update and regenerate daily first"
        )
    account_as_of = account["as_of"]
    ranking = pd.read_csv(snapshot.ranking_path)
    target_rows = ranking[ranking["selected"]]
    if target_rows["instrument_id"].duplicated().any():
        raise ValueError("daily target contains duplicate instruments")
    target_rows = target_rows.copy()
    target_rows["target_weight"] = pd.to_numeric(target_rows["target_weight"], errors="coerce")
    if (
        target_rows["target_weight"].isna().any()
        or (target_rows["target_weight"] <= 0).any()
        or (target_rows["target_weight"] > 1).any()
        or target_rows["target_weight"].sum() > 1.000000001
    ):
        raise ValueError("daily target weights are outside a long-only fully-funded portfolio")
    if "rank" not in target_rows:
        raise ValueError("daily target is missing alpha rank for deterministic cash allocation")
    target_rows["rank"] = pd.to_numeric(target_rows["rank"], errors="coerce")
    if (
        target_rows["rank"].isna().any()
        or (target_rows["rank"] <= 0).any()
        or (target_rows["rank"] % 1 != 0).any()
    ):
        raise ValueError("daily target alpha ranks must be positive integers")
    target_weights = dict(
        zip(target_rows["instrument_id"], target_rows["target_weight"], strict=True)
    )
    target_ranks = {
        instrument: int(rank)
        for instrument, rank in zip(target_rows["instrument_id"], target_rows["rank"], strict=True)
    }
    risk_context = {
        instrument: str(context)
        for instrument, context in zip(
            ranking["instrument_id"], ranking["risk_context"], strict=True
        )
    }
    bars = {item.instrument_id: item for item in storage.load_daily_bars_by_date(signal_date)}
    securities = {item.instrument_id: item for item in storage.load_securities()}
    current = {item["instrument_id"]: item for item in account["positions"]}
    missing_prices = sorted(set(current) - set(bars))
    if missing_prices:
        raise ValueError(
            "cannot value complete account on reference date; missing prices: "
            + ", ".join(missing_prices[:10])
        )

    nav_fen = account["cash_fen"] + sum(
        item["quantity"] * _fen_from_price(bars[instrument].close)
        for instrument, item in current.items()
    )
    available_cash = account["cash_fen"]
    rows = []
    # Cash is deliberately consumed by economic priority, not security code.
    # Existing non-target holdings sort after ranked targets and sale proceeds
    # never increase ``available_cash`` below.
    instruments = sorted(
        set(current) | set(target_weights),
        key=lambda instrument: (target_ranks.get(instrument, float("inf")), instrument),
    )
    for instrument in instruments:
        holding = current.get(
            instrument,
            {"quantity": 0, "sellable_quantity": 0, "reference_cost_fen": None},
        )
        security = securities.get(instrument)
        bar = bars.get(instrument)
        grid = _quantity_grid(security.board) if security else None
        weight = Decimal(str(target_weights.get(instrument, 0.0)))
        reference_price_fen = _fen_from_price(bar.close) if bar else None
        target_shares = 0
        rule_id = None
        if weight > 0 and reference_price_fen is not None and grid is not None:
            minimum, step, rule_id = grid
            budget = int((Decimal(nav_fen) * weight).quantize(Decimal("1"), rounding=ROUND_FLOOR))
            target_shares = _floor_buy(budget // reference_price_fen, minimum, step)
        delta = target_shares - holding["quantity"]
        action = "HOLD"
        reason = "AT_TARGET"
        planned = 0
        fee_fen = 0
        pending = [
            "T_PLUS_1_MARKET_STATUS",
            "NEXT_SESSION_PRICE_LIMIT_NOT_YET_OBSERVED",
            "EXECUTABLE_LIMIT_PRICE",
            "VERIFIED_ALL_IN_FEE",
        ]
        context = risk_context.get(instrument, "OUTSIDE_RANKING_CONTEXT_UNKNOWN")
        if security is None or grid is None:
            action, reason = "NO_TRADE", "PIT_OR_QUANTITY_RULE_UNKNOWN"
        elif bar is None:
            action, reason = "NO_TRADE", "REFERENCE_PRICE_MISSING"
        elif account["open_orders_declaration"] != "none_declared":
            action, reason = "NO_TRADE", "OPEN_ORDERS_NOT_RECONCILED"
        elif weight > 0 and holding["quantity"] == 0 and target_shares == 0:
            action, reason = "NO_TRADE", "TARGET_BUDGET_BELOW_MINIMUM_QUANTITY"
        elif delta > 0 and "ST_CONTEXT_REPORTED" in context:
            action, reason = "NO_TRADE", "ST_CONTEXT_REQUIRES_USER_REVIEW"
        elif delta > 0 and "SUSPENSION_CONTEXT_REPORTED:S" in context:
            action, reason = "NO_TRADE", "SUSPENSION_CONTEXT_REQUIRES_REVIEW"
        elif delta > 0:
            notional = delta * reference_price_fen
            fee_fen = _estimated_commission_fen(notional)
            if notional + fee_fen <= available_cash:
                action, reason, planned = "BUY", "REFERENCE_TARGET_INCREASE", delta
                available_cash -= notional + fee_fen
            else:
                action, reason = "NO_TRADE", "INSUFFICIENT_CURRENT_CASH_NO_SELL_FUNDING"
                fee_fen = 0
        elif delta < 0:
            desired = -delta
            sellable = min(desired, holding["sellable_quantity"])
            minimum, step, rule_id = grid
            if sellable == holding["quantity"] and desired == holding["quantity"]:
                planned = sellable
            elif sellable >= minimum:
                planned = minimum + ((sellable - minimum) // step) * step
            if planned > 0:
                action, reason = "SELL", "REFERENCE_TARGET_DECREASE"
                fee_fen = _estimated_commission_fen(planned * reference_price_fen)
            else:
                action, reason = "HOLD", "T1_OR_QUANTITY_GRID_PREVENTS_SELL"
        rows.append(
            {
                "instrument_id": instrument,
                "name": security.name if security else None,
                "target_alpha_rank": target_ranks.get(instrument),
                "action": action,
                "reason": reason,
                "current_shares": holding["quantity"],
                "sellable_shares": holding["sellable_quantity"],
                "target_shares": target_shares,
                "planned_shares": planned,
                "reference_price_date": signal_date.isoformat(),
                "reference_price_cny": (
                    str(Decimal(reference_price_fen) / 100)
                    if reference_price_fen is not None
                    else None
                ),
                "estimated_notional_cny": (
                    str(Decimal(planned * reference_price_fen) / 100)
                    if reference_price_fen is not None
                    else None
                ),
                "estimated_partial_fee_cny": str(Decimal(fee_fen) / 100),
                "fee_status": "user_reported_commission_only_not_verified_all_in",
                "quantity_rule_status": "engineering_assumption_requires_next_session_review",
                "quantity_rule_id": rule_id,
                "risk_context": context,
                "pending_checks": ";".join(pending),
            }
        )
    frame = pd.DataFrame(rows)
    economic = {
        "schema": "quantlab_reference_plan_v1",
        "account_id": account_id,
        "account_mode": account["account_mode"],
        "account_fingerprint": account["account_fingerprint"],
        "account_as_of": account_as_of,
        "signal_date": signal_date.isoformat(),
        "intended_next_session": intended_session.isoformat(),
        "daily_content_fingerprint": snapshot.report["content_fingerprint"],
        "planning_nav_fen": nav_fen,
        "remaining_current_cash_after_reference_buys_fen": available_cash,
        "status": "reference_only_pending_review",
        "strategy_status": snapshot.report.get("model", {}).get("model_status", "unknown"),
        "candidate_warning": (
            "RESEARCH_CANDIDATE_NOT_PROMOTED"
            if "candidate" in snapshot.report.get("model", {}).get("model_status", "").lower()
            else None
        ),
        "execution_confirmed": False,
        "broker_submission": False,
        "sell_proceeds_fund_buys": False,
        "cash_allocation_policy": "target_alpha_rank_ascending_then_instrument_id",
        "price_basis": "raw_T_close_reference_not_order_limit",
        "fee_evidence": (
            "user_reported commission only: <=500k 0.86/10000; >500k 0.80/10000; min CNY5"
        ),
        "known_limitations": [
            "statutory and exchange fees are not included because all-in commission "
            "scope is unverified",
            "next-session market status and executable limit price are unknown",
            "quantity rules are current engineering assumptions, not a validated 2026 rule book",
        ],
        "rows": rows,
    }
    plan_id = hashlib.sha256(
        json.dumps(economic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, lineterminator="\n")
    csv_content = buffer.getvalue()
    payload = {
        **economic,
        "plan_id": plan_id,
        "csv_sha256": hashlib.sha256(csv_content.encode()).hexdigest(),
    }
    out_dir = account_root / account_id / "plans" / signal_date.isoformat() / plan_id
    json_path = out_dir / "plan.json"
    csv_path = out_dir / "plan.csv"
    atomic_text(csv_path, csv_content)
    atomic_json(json_path, payload)
    return json_path, csv_path, payload
