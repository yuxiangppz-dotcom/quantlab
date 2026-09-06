"""Fail-closed conversion from research target weights to share targets.

Reference prices are raw, unadjusted observations used only to plan integer
share quantities. They are never order prices, fills, or proof that a market
was accessible. Any unresolved positive target suppresses the whole
instruction so omission cannot be misread downstream as a target of zero.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from quantlab.execution.models import (
    InstructionSourceMetadata,
    PositionTarget,
    PriceBasis,
    RebalanceInstruction,
    exchange_date,
    require_aware,
    require_decimal,
    require_identifier,
    require_int,
)
from quantlab.execution.rules import PITIdentityBook, PITRuleBook, TradingCalendar
from quantlab.portfolio import TargetPortfolio

PLANNER_VERSION = "target_weight_to_share_target_v0_1"
PLANNING_PRICE_POLICY = "raw_observation_available_by_signal_cutoff"
SHARE_ROUNDING_POLICY = "applicable_buy_lot_floor_from_zero"
CASH_POLICY = "max_target_cash_weight_and_minimum_cash_fen"


class HandoffStatus(StrEnum):
    READY = "ready"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PlanningPrice:
    instrument_id: str
    price: Decimal
    price_date: date
    available_at: datetime
    basis: PriceBasis
    source_id: str

    def __post_init__(self) -> None:
        require_identifier(self.instrument_id, "instrument_id")
        require_decimal(self.price, "planning price", positive=True)
        require_aware(self.available_at, "planning price available_at")
        if not isinstance(self.basis, PriceBasis):
            raise ValueError("planning price basis must be a PriceBasis enum")
        require_identifier(self.source_id, "planning price source_id")
        if exchange_date(self.available_at) < self.price_date:
            raise ValueError("planning price cannot be available before its price date")


@dataclass(frozen=True)
class TargetHandoffConfig:
    portfolio_id: str
    signal_as_of: datetime
    execution_date: date
    planning_nav_fen: int
    minimum_cash_fen: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.portfolio_id, "portfolio_id")
        require_aware(self.signal_as_of, "signal_as_of")
        require_int(self.planning_nav_fen, "planning_nav_fen", minimum=1)
        require_int(self.minimum_cash_fen, "minimum_cash_fen")
        if self.minimum_cash_fen > self.planning_nav_fen:
            raise ValueError("minimum cash exceeds planning NAV")


@dataclass(frozen=True)
class HandoffAuditRow:
    instrument_id: str
    target_weight: str
    status: HandoffStatus
    reason_code: str
    planning_price: str | None
    planning_price_date: date | None
    planning_price_source_id: str | None
    target_budget_fen: int
    unrounded_shares: int | None
    target_shares: int | None
    rounding_cash_fen: int | None
    rule_id: str | None


@dataclass(frozen=True)
class TargetHandoffResult:
    instruction: RebalanceInstruction | None
    audit_rows: tuple[HandoffAuditRow, ...]
    status: HandoffStatus
    reason_codes: tuple[str, ...]
    planned_position_notional_fen: int | None
    planned_cash_fen: int | None


def fingerprint_target_portfolio(target: TargetPortfolio) -> str:
    """Stable fingerprint that does not depend on dict or input row order."""
    payload = {
        "as_of": target.as_of.isoformat(),
        "cash_weight": format(target.cash_weight, ".17g"),
        "positions": [
            {
                "instrument_id": item.instrument_id,
                "target_weight": format(item.target_weight, ".17g"),
            }
            for item in sorted(target.positions, key=lambda item: item.instrument_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _floor_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _floor_to_rule_quantity(raw_shares: int, minimum: int, step: int) -> int:
    if raw_shares < minimum:
        return 0
    return minimum + ((raw_shares - minimum) // step) * step


def build_rebalance_instruction(
    target: TargetPortfolio,
    config: TargetHandoffConfig,
    planning_prices: Mapping[str, PlanningPrice],
    *,
    calendar: TradingCalendar,
    identities: PITIdentityBook,
    rules: PITRuleBook,
) -> TargetHandoffResult:
    """Convert a complete long-only target to integer shares, or publish none."""
    if target.as_of != exchange_date(config.signal_as_of):
        raise ValueError("target as_of must equal the Shanghai-local signal cutoff date")
    if calendar.next_session(target.as_of) != config.execution_date:
        raise ValueError("execution_date must be the next declared trading session")

    fingerprint = fingerprint_target_portfolio(target)
    rows: list[HandoffAuditRow] = []
    targets: list[PositionTarget] = []
    source_ids: set[str] = set()
    reasons: set[str] = set()
    planned_notional_fen = 0

    for item in sorted(target.positions, key=lambda value: value.instrument_id):
        weight = Decimal(format(item.target_weight, ".17g"))
        budget_fen = _floor_decimal(Decimal(config.planning_nav_fen) * weight)
        base = {
            "instrument_id": item.instrument_id,
            "target_weight": str(weight),
            "target_budget_fen": budget_fen,
        }
        if weight < 0:
            reasons.add("short_target_not_supported")
            rows.append(HandoffAuditRow(
                **base, status=HandoffStatus.REJECTED,
                reason_code="short_target_not_supported", planning_price=None,
                planning_price_date=None, planning_price_source_id=None,
                unrounded_shares=None, target_shares=None, rounding_cash_fen=None,
                rule_id=None,
            ))
            continue
        if weight == 0:
            targets.append(PositionTarget(item.instrument_id, 0))
            rows.append(HandoffAuditRow(
                **base, status=HandoffStatus.READY, reason_code="explicit_zero_target",
                planning_price=None, planning_price_date=None,
                planning_price_source_id=None, unrounded_shares=0, target_shares=0,
                rounding_cash_fen=0, rule_id=None,
            ))
            continue

        price = planning_prices.get(item.instrument_id)
        if price is None:
            reasons.add("planning_price_missing")
            rows.append(HandoffAuditRow(
                **base, status=HandoffStatus.UNKNOWN,
                reason_code="planning_price_missing", planning_price=None,
                planning_price_date=None, planning_price_source_id=None,
                unrounded_shares=None, target_shares=None, rounding_cash_fen=None,
                rule_id=None,
            ))
            continue
        if price.instrument_id != item.instrument_id:
            raise ValueError("planning price mapping key/instrument mismatch")
        if price.basis is not PriceBasis.RAW:
            reasons.add("planning_price_not_raw")
            status = (
                HandoffStatus.REJECTED
                if price.basis is PriceBasis.ADJUSTED
                else HandoffStatus.UNKNOWN
            )
            rows.append(HandoffAuditRow(
                **base, status=status, reason_code="planning_price_not_raw",
                planning_price=str(price.price), planning_price_date=price.price_date,
                planning_price_source_id=price.source_id, unrounded_shares=None,
                target_shares=None, rounding_cash_fen=None, rule_id=None,
            ))
            continue
        if price.available_at > config.signal_as_of or price.price_date > target.as_of:
            reasons.add("planning_price_after_signal_cutoff")
            rows.append(HandoffAuditRow(
                **base, status=HandoffStatus.REJECTED,
                reason_code="planning_price_after_signal_cutoff",
                planning_price=str(price.price), planning_price_date=price.price_date,
                planning_price_source_id=price.source_id, unrounded_shares=None,
                target_shares=None, rounding_cash_fen=None, rule_id=None,
            ))
            continue
        price_session_status = calendar.session_status(price.price_date)
        if price_session_status is not True:
            reason = (
                "planning_price_date_not_session"
                if price_session_status is False
                else "planning_price_date_outside_calendar_coverage"
            )
            reasons.add(reason)
            rows.append(HandoffAuditRow(
                **base,
                status=(
                    HandoffStatus.REJECTED
                    if price_session_status is False
                    else HandoffStatus.UNKNOWN
                ),
                reason_code=reason,
                planning_price=str(price.price),
                planning_price_date=price.price_date,
                planning_price_source_id=price.source_id,
                unrounded_shares=None,
                target_shares=None,
                rounding_cash_fen=None,
                rule_id=None,
            ))
            continue

        identity = identities.resolve(item.instrument_id, config.execution_date)
        rule = (
            rules.resolve(identity.exchange, identity.board, config.execution_date)
            if identity is not None
            else None
        )
        if identity is None or rule is None:
            reason = "pit_identity_missing" if identity is None else "pit_rule_missing"
            reasons.add(reason)
            rows.append(HandoffAuditRow(
                **base, status=HandoffStatus.UNKNOWN, reason_code=reason,
                planning_price=str(price.price), planning_price_date=price.price_date,
                planning_price_source_id=price.source_id, unrounded_shares=None,
                target_shares=None, rounding_cash_fen=None,
                rule_id=None,
            ))
            continue

        price_fen = price.price * 100
        if price_fen != price_fen.to_integral_value():
            reasons.add("planning_price_requires_fractional_fen")
            rows.append(HandoffAuditRow(
                **base, status=HandoffStatus.UNKNOWN,
                reason_code="planning_price_requires_fractional_fen",
                planning_price=str(price.price), planning_price_date=price.price_date,
                planning_price_source_id=price.source_id, unrounded_shares=None,
                target_shares=None, rounding_cash_fen=None, rule_id=rule.rule_id,
            ))
            continue

        price_fen_int = int(price_fen)
        unrounded = budget_fen // price_fen_int
        shares = _floor_to_rule_quantity(
            unrounded, rule.buy_min_quantity, rule.buy_quantity_step
        )
        notional_fen = shares * price_fen_int
        rounding_cash = budget_fen - notional_fen
        targets.append(PositionTarget(item.instrument_id, shares))
        source_ids.add(price.source_id)
        planned_notional_fen += notional_fen
        rows.append(HandoffAuditRow(
            **base, status=HandoffStatus.READY,
            reason_code=("rounded_to_zero_below_buy_minimum" if shares == 0 else "lot_rounded"),
            planning_price=str(price.price), planning_price_date=price.price_date,
            planning_price_source_id=price.source_id, unrounded_shares=unrounded,
            target_shares=shares, rounding_cash_fen=rounding_cash,
            rule_id=rule.rule_id,
        ))

    target_cash_fen = _floor_decimal(
        Decimal(config.planning_nav_fen)
        * Decimal(format(target.cash_weight, ".17g"))
    )
    required_cash_fen = max(config.minimum_cash_fen, target_cash_fen)
    planned_cash_fen = config.planning_nav_fen - planned_notional_fen
    if not reasons and planned_cash_fen < required_cash_fen:
        reasons.add("minimum_cash_not_met_after_rounding")

    if reasons:
        status = (
            HandoffStatus.REJECTED
            if any(row.status is HandoffStatus.REJECTED for row in rows)
            else HandoffStatus.UNKNOWN
        )
        return TargetHandoffResult(
            instruction=None,
            audit_rows=tuple(rows),
            status=status,
            reason_codes=tuple(sorted(reasons)),
            planned_position_notional_fen=None,
            planned_cash_fen=None,
        )

    planning_payload = [
        {
            "instrument_id": row.instrument_id,
            "target_weight": row.target_weight,
            "planning_price": row.planning_price,
            "planning_price_date": (
                row.planning_price_date.isoformat() if row.planning_price_date else None
            ),
            "planning_price_source_id": row.planning_price_source_id,
            "target_budget_fen": row.target_budget_fen,
            "unrounded_shares": row.unrounded_shares,
            "target_shares": row.target_shares,
            "rounding_cash_fen": row.rounding_cash_fen,
            "rule_id": row.rule_id,
        }
        for row in rows
    ]
    planning_input_fingerprint = hashlib.sha256(
        json.dumps(planning_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    metadata = InstructionSourceMetadata(
        target_as_of=target.as_of,
        target_fingerprint=fingerprint,
        planning_input_fingerprint=planning_input_fingerprint,
        planner_version=PLANNER_VERSION,
        planning_nav_fen=config.planning_nav_fen,
        minimum_cash_fen=config.minimum_cash_fen,
        planning_price_basis=PriceBasis.RAW,
        planning_price_policy=PLANNING_PRICE_POLICY,
        share_rounding_policy=SHARE_ROUNDING_POLICY,
        cash_policy=CASH_POLICY,
        planning_price_source_ids=tuple(sorted(source_ids)),
    )
    id_payload = {
        "portfolio_id": config.portfolio_id,
        "signal_as_of": config.signal_as_of.isoformat(),
        "execution_date": config.execution_date.isoformat(),
        "metadata": {
            "target_fingerprint": fingerprint,
            "planning_nav_fen": config.planning_nav_fen,
            "minimum_cash_fen": config.minimum_cash_fen,
            "price_source_ids": sorted(source_ids),
            "planning_input_fingerprint": planning_input_fingerprint,
        },
        "targets": [
            {"instrument_id": value.instrument_id, "target_shares": value.target_shares}
            for value in targets
        ],
    }
    instruction_id = hashlib.sha256(
        json.dumps(id_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    instruction = RebalanceInstruction(
        instruction_id=instruction_id,
        portfolio_id=config.portfolio_id,
        signal_as_of=config.signal_as_of,
        execution_date=config.execution_date,
        targets=tuple(targets),
        source_fingerprint=fingerprint,
        source_metadata=metadata,
    )
    return TargetHandoffResult(
        instruction=instruction,
        audit_rows=tuple(rows),
        status=HandoffStatus.READY,
        reason_codes=(),
        planned_position_notional_fen=planned_notional_fen,
        planned_cash_fen=planned_cash_fen,
    )
