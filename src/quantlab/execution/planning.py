"""Account-aware order planning from a share instruction to an order plan.

A :class:`RebalanceInstruction.targets` tuple is the COMPLETE desired
position set: every instrument held by the account but absent from the
instruction has a target of zero (an explicit exit, not an omission). Order
quantities are ``target_shares - current_shares`` per instrument.

Planning prices are NOT handoff planning prices: an order limit must come
from separate, raw-unadjusted order-price evidence carrying its own
``available_at`` and source fingerprint. Adjusted, future, missing, or
PIT-unresolved prices stay unknown/rejected. A delta that cannot satisfy a
legal order unit blocks the leg with a structured reason — the final target
is never silently rounded or rewritten. Buys are funded only from currently
available (unreserved) cash at the worst case (limit notional plus fee
cap); expected sell proceeds never fund a buy. No broker submission
happens here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from quantlab.execution.models import (
    AccountSnapshot,
    ExecutionValidationError,
    FeeCapQuote,
    OrderPriceEvidence,
    PositionLot,
    PriceBasis,
    RebalanceInstruction,
    Side,
    exchange_date,
    fingerprint_fee_cap_quote,
    fingerprint_order_price_evidence,
    require_aware,
    require_identifier,
    require_int,
)
from quantlab.execution.models import (
    fingerprint_order_intent as fingerprint_order_intent,
)
from quantlab.execution.rules import PITIdentityBook, PITRuleBook, TradingCalendar

PLANNER_VERSION = "account_aware_order_plan_v0_2_2"


@dataclass(frozen=True)
class AvailabilityReservation:
    """Canonical per-order reservation record for the availability state."""

    order_id: str
    instrument_id: str
    limit_price_fen: int
    fee_cap_fen: int
    fee_used_fen: int
    reserved_cash_fen: int
    reserved_shares: int

    def __post_init__(self) -> None:
        require_identifier(self.order_id, "order_id")
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.limit_price_fen, "limit_price_fen", minimum=0)
        require_int(self.fee_cap_fen, "fee_cap_fen", minimum=0)
        require_int(self.fee_used_fen, "fee_used_fen", minimum=0)
        require_int(self.reserved_cash_fen, "reserved_cash_fen", minimum=0)
        require_int(self.reserved_shares, "reserved_shares", minimum=0)
        if self.fee_used_fen > self.fee_cap_fen:
            raise ExecutionValidationError(
                "fee_used_fen cannot exceed fee_cap_fen"
            )


@dataclass(frozen=True)
class AvailabilityState:
    """Canonical resource state for TOCTOU binding.

    This is the ONLY canonical carrier of "what can still be spent or
    sold". Its fingerprint is derived from its own payload bytes - never
    accepted from a caller - so any fabricated view is rejected by the
    ledger's recomputation at accept time. Pure lifecycle transitions
    (INTENDED -> VALIDATED) do not touch this state; reservations, fills,
    cancels, expiries, and settled-cash/lot changes do.
    """

    account_id: str
    as_of: datetime
    trade_date: date
    settled_cash_fen: int
    lots: tuple[PositionLot, ...]
    reservations: tuple[AvailabilityReservation, ...]
    available_cash_fen: int
    available_sellable_shares: Mapping[str, int]

    def __post_init__(self) -> None:
        require_identifier(self.account_id, "account_id")
        require_aware(self.as_of, "as_of")
        require_int(self.settled_cash_fen, "settled_cash_fen")
        if self.available_cash_fen < 0:
            raise ExecutionValidationError(
                "available_cash_fen must be non-negative"
            )
        if any(value < 0 for value in self.available_sellable_shares.values()):
            raise ExecutionValidationError(
                "available_sellable_shares must be non-negative"
            )

    def _payload(self) -> dict:
        return {
            "account_id": self.account_id,
            "as_of": self.as_of.isoformat(),
            "trade_date": self.trade_date.isoformat(),
            "settled_cash_fen": self.settled_cash_fen,
            "lots": _fingerprint_lots_payload(self.lots),
            "reservations": [
                {
                    "order_id": item.order_id,
                    "instrument_id": item.instrument_id,
                    "limit_price_fen": item.limit_price_fen,
                    "fee_cap_fen": item.fee_cap_fen,
                    "fee_used_fen": item.fee_used_fen,
                    "reserved_cash_fen": item.reserved_cash_fen,
                    "reserved_shares": item.reserved_shares,
                }
                for item in sorted(
                    self.reservations, key=lambda item: item.order_id
                )
            ],
            "available_cash_fen": self.available_cash_fen,
            "available_sellable_shares": {
                key: self.available_sellable_shares[key]
                for key in sorted(self.available_sellable_shares)
            },
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self._payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExecutionStateView:
    """Reservation-aware account view used for planning and lineage.

    The view wraps the canonical :class:`AvailabilityState`; its
    fingerprint is derived from that state's own bytes, never declared by
    the caller. The ledger re-derives and compares the canonical value
    when accepting intents, assessments, and submissions.
    """

    account: AccountSnapshot
    state: AvailabilityState

    def __post_init__(self) -> None:
        if self.account.account_id != self.state.account_id:
            raise ExecutionValidationError(
                "execution-state view does not match the account snapshot"
            )
        if self.account.cash_fen != self.state.settled_cash_fen:
            raise ExecutionValidationError(
                "execution-state view settled cash differs from the account"
            )

    @property
    def available_cash_fen(self) -> int:
        return self.state.available_cash_fen

    @property
    def available_sellable_shares(self) -> Mapping[str, int]:
        return self.state.available_sellable_shares

    @property
    def fingerprint(self) -> str:
        return self.state.fingerprint

    @property
    def availability_fingerprint(self) -> str:
        return self.state.fingerprint


def _fingerprint_lots_payload(lots: tuple[PositionLot, ...]) -> list[dict]:
    return [
        {
            "lot_id": lot.lot_id,
            "instrument_id": lot.instrument_id,
            "quantity": lot.quantity,
            "acquired_trade_date": lot.acquired_trade_date.isoformat(),
            "sellable_from": lot.sellable_from.isoformat(),
        }
        for lot in sorted(
            lots,
            key=lambda lot: (
                lot.instrument_id,
                lot.sellable_from,
                lot.acquired_trade_date,
                lot.lot_id,
            ),
        )
    ]


class OrderPlanStatus(StrEnum):
    SUBMIT_READY = "submit_ready"
    BLOCKED = "blocked"


class OrderPlanLegStatus(StrEnum):
    ORDERABLE = "orderable"
    NOT_TRADED = "not_traded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class OrderPlanLeg:
    """One instrument's planned transition with its full audit trail."""

    leg_id: str
    instruction_id: str
    instrument_id: str
    side: Side | None
    current_shares: int
    sellable_shares: int
    target_shares: int
    delta_shares: int
    status: OrderPlanLegStatus
    reason_code: str
    message: str
    lot_rule_id: str | None = None
    identity_source_record_id: str | None = None
    limit_price: Decimal | None = None
    limit_price_basis: PriceBasis | None = None
    limit_price_source_id: str | None = None
    limit_price_source_fingerprint: str | None = None
    limit_price_evidence_fingerprint: str | None = None
    limit_price_available_at: datetime | None = None
    worst_case_fee_fen: int | None = None
    fee_quote_fingerprint: str | None = None


@dataclass(frozen=True)
class OrderPlan:
    """A fully audited, account-aware order plan; never a submission."""

    plan_id: str
    instruction_id: str
    instruction_fingerprint: str
    account_fingerprint: str
    account_id: str
    execution_date: date
    created_at: datetime
    status: OrderPlanStatus
    reason_codes: tuple[str, ...]
    legs: tuple[OrderPlanLeg, ...]
    worst_case_cash_fen_required: int
    availability_fingerprint: str | None = None




def fingerprint_rebalance_instruction(instruction: RebalanceInstruction) -> str:
    """Deterministic fingerprint of the instruction's economic content.

    Binds the signal cutoff, the full source metadata, and every target.
    """
    payload = {
        "instruction_id": instruction.instruction_id,
        "portfolio_id": instruction.portfolio_id,
        "signal_as_of": instruction.signal_as_of.isoformat(),
        "execution_date": instruction.execution_date.isoformat(),
        "source_fingerprint": instruction.source_fingerprint,
        "source_metadata": {
            "target_as_of":
                instruction.source_metadata.target_as_of.isoformat(),
            "target_fingerprint":
                instruction.source_metadata.target_fingerprint,
            "planning_input_fingerprint":
                instruction.source_metadata.planning_input_fingerprint,
            "planner_version": instruction.source_metadata.planner_version,
            "planning_nav_fen": instruction.source_metadata.planning_nav_fen,
            "minimum_cash_fen":
                instruction.source_metadata.minimum_cash_fen,
            "planning_price_basis":
                instruction.source_metadata.planning_price_basis.value,
            "planning_price_policy":
                instruction.source_metadata.planning_price_policy,
            "share_rounding_policy":
                instruction.source_metadata.share_rounding_policy,
            "cash_policy": instruction.source_metadata.cash_policy,
            "planning_price_source_ids": list(
                instruction.source_metadata.planning_price_source_ids
            ),
        },
        "targets": [
            {
                "instrument_id": target.instrument_id,
                "target_shares": target.target_shares,
            }
            for target in sorted(
                instruction.targets, key=lambda item: item.instrument_id
            )
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def fingerprint_account_state(account: AccountSnapshot) -> str:
    """Deterministic fingerprint of the account's settled state."""
    payload = {
        "account_id": account.account_id,
        "cash_fen": account.cash_fen,
        "lots": [
            {
                "lot_id": lot.lot_id,
                "instrument_id": lot.instrument_id,
                "quantity": lot.quantity,
                "acquired_trade_date": lot.acquired_trade_date.isoformat(),
                "sellable_from": lot.sellable_from.isoformat(),
            }
            for lot in sorted(
                account.lots,
                key=lambda lot: (
                    lot.instrument_id,
                    lot.sellable_from,
                    lot.acquired_trade_date,
                    lot.lot_id,
                ),
            )
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_order_plan(
    instruction: RebalanceInstruction,
    account: AccountSnapshot,
    *,
    created_at: datetime,
    order_prices: Mapping[str, OrderPriceEvidence],
    fee_caps: Mapping[str, FeeCapQuote] | None = None,
    calendar: TradingCalendar,
    identities: PITIdentityBook,
    rules: PITRuleBook,
    execution_state: ExecutionStateView | None = None,
) -> OrderPlan:
    """Plan one instruction against one account snapshot, fully audited.

    ``created_at`` is the aware planning instant: order-price evidence must
    be available no later than it, and the plan's DAY orders bind the
    instruction's ``execution_date`` as their intended trade date.

    ``execution_state`` is the reservation-aware view: buys are funded only
    from ``available_cash_fen`` (settled minus active reservations) and sell
    legs are limited by ``available_sellable_shares``. Passing the settled
    snapshot alone (``execution_state=None``) is the legacy low-level path
    and never appears in production readiness evidence.
    """
    require_aware(created_at, "created_at")
    if account.account_id.strip() == "":
        raise ExecutionValidationError("account_id must be non-empty")
    if exchange_date(created_at) > instruction.execution_date:
        raise ExecutionValidationError(
            "plan created after the intended execution date"
        )
    if execution_state is not None and execution_state.account != account:
        raise ExecutionValidationError(
            "execution-state view does not match the account snapshot"
        )

    instruction_fingerprint = fingerprint_rebalance_instruction(instruction)
    account_fingerprint = fingerprint_account_state(account)

    current: dict[str, int] = {}
    sellable: dict[str, int] = {}
    for lot in account.lots:
        current[lot.instrument_id] = (
            current.get(lot.instrument_id, 0) + lot.quantity
        )
        if lot.sellable_from <= instruction.execution_date:
            sellable[lot.instrument_id] = (
                sellable.get(lot.instrument_id, 0) + lot.quantity
            )
    if execution_state is not None:
        # T+1 sellable is further reduced by active share reservations
        for instrument_id in set(sellable) | set(
            execution_state.available_sellable_shares
        ):
            sellable[instrument_id] = min(
                sellable.get(instrument_id, 0),
                execution_state.available_sellable_shares.get(instrument_id, 0),
            )

    target_map: dict[str, int] = {
        target.instrument_id: target.target_shares for target in instruction.targets
    }
    # The instruction is the complete desired position set: any held name it
    # omits has an explicit target of zero and must therefore be sold.
    for instrument_id in current:
        target_map.setdefault(instrument_id, 0)

    fee_caps = fee_caps or {}
    legs: list[OrderPlanLeg] = []
    reasons: set[str] = set()
    worst_case_cash = 0

    def _leg(
        instrument_id: str,
        side: Side | None,
        target_shares: int,
        status: OrderPlanLegStatus,
        reason_code: str,
        message: str,
        *,
        lot_rule_id: str | None = None,
        identity_record: str | None = None,
        price: OrderPriceEvidence | None = None,
        fee_cap: FeeCapQuote | None = None,
        delta: int = 0,
    ) -> None:
        leg_payload = {
            "instruction_id": instruction.instruction_id,
            "instruction_fingerprint": instruction_fingerprint,
            "account_fingerprint": account_fingerprint,
            "instrument_id": instrument_id,
            "side": side.value if side else None,
            "current_shares": current.get(instrument_id, 0),
            "sellable_shares": sellable.get(instrument_id, 0),
            "target_shares": target_shares,
            "delta_shares": delta,
            "status": status.value,
            "reason_code": reason_code,
            "lot_rule_id": lot_rule_id,
            "limit_price": (str(price.price) if price else None),
            "limit_price_basis": (price.basis.value if price else None),
            "limit_price_source_id": (price.source_id if price else None),
            "limit_price_source_fingerprint": (
                price.source_fingerprint if price else None
            ),
            "limit_price_evidence_fingerprint": (
                fingerprint_order_price_evidence(price) if price else None
            ),
            "limit_price_available_at": (
                price.available_at.isoformat() if price else None
            ),
            "worst_case_fee_fen": (fee_cap.cap_fen if fee_cap else None),
            "fee_quote_fingerprint": (
                fingerprint_fee_cap_quote(fee_cap) if fee_cap else None
            ),
        }
        leg_id = hashlib.sha256(
            json.dumps(leg_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        legs.append(
            OrderPlanLeg(
                leg_id=leg_id,
                instruction_id=instruction.instruction_id,
                instrument_id=instrument_id,
                side=side,
                current_shares=current.get(instrument_id, 0),
                sellable_shares=sellable.get(instrument_id, 0),
                target_shares=target_shares,
                delta_shares=delta,
                status=status,
                reason_code=reason_code,
                message=message,
                lot_rule_id=lot_rule_id,
                identity_source_record_id=identity_record,
                limit_price=(price.price if price else None),
                limit_price_basis=(price.basis if price else None),
                limit_price_source_id=(price.source_id if price else None),
                limit_price_source_fingerprint=(
                    price.source_fingerprint if price else None
                ),
                limit_price_evidence_fingerprint=(
                    leg_payload["limit_price_evidence_fingerprint"]
                ),
                limit_price_available_at=(price.available_at if price else None),
                worst_case_fee_fen=(fee_cap.cap_fen if fee_cap else None),
                fee_quote_fingerprint=(
                    leg_payload["fee_quote_fingerprint"]
                ),
            )
        )

    for instrument_id in sorted(target_map):
        target_shares = target_map[instrument_id]
        current_shares = current.get(instrument_id, 0)
        delta = target_shares - current_shares
        if delta == 0:
            _leg(
                instrument_id, None, target_shares,
                OrderPlanLegStatus.NOT_TRADED, "already_at_target",
                "current shares equal the target; no order is planned",
                delta=delta,
            )
            continue

        side = Side.BUY if delta > 0 else Side.SELL
        quantity = abs(delta)
        identity = identities.resolve(instrument_id, instruction.execution_date)
        rule = (
            rules.resolve(identity.exchange, identity.board, instruction.execution_date)
            if identity is not None
            else None
        )
        if identity is None or rule is None:
            reason = (
                "pit_identity_missing" if identity is None else "pit_rule_missing"
            )
            reasons.add(reason)
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, reason,
                "PIT identity or rule book does not resolve on the execution date",
                delta=delta,
                identity_record=(identity.source_record_id if identity else None),
            )
            continue

        evidence = order_prices.get(instrument_id)
        if evidence is not None and not isinstance(evidence, OrderPriceEvidence):
            # handoff planning prices and other foreign price objects are
            # never order prices; the TYPE check must come first so a
            # foreign object yields a structured blocked leg instead of an
            # AttributeError on a missing attribute
            reasons.add("order_price_evidence_invalid")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "order_price_evidence_invalid",
                "order limits require raw order-price evidence with its own "
                "available_at and source fingerprint",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                delta=delta,
            )
            continue
        if evidence is not None and evidence.instrument_id != instrument_id:
            # evidence is keyed by instrument; a mismatched payload would
            # price one instrument with another instrument's evidence
            reasons.add("order_price_evidence_instrument_mismatch")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED,
                "order_price_evidence_instrument_mismatch",
                "order-price evidence belongs to a different instrument",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                delta=delta,
            )
            continue
        if evidence is None:
            reasons.add("order_price_evidence_missing")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "order_price_evidence_missing",
                "no raw order-price evidence was supplied for this leg",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                delta=delta,
            )
            continue
        if evidence.basis is not PriceBasis.RAW:
            reasons.add("order_price_basis_not_raw")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "order_price_basis_not_raw",
                "only raw unadjusted prices may become order limits",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue
        if evidence.available_at > created_at:
            reasons.add("order_price_evidence_from_the_future")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "order_price_evidence_from_the_future",
                "order-price evidence became available after the planning instant",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue
        if exchange_date(evidence.available_at) < evidence.price_date:
            reasons.add("order_price_evidence_pre_dated")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "order_price_evidence_pre_dated",
                "order-price evidence claims availability before its price date",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue
        price_session = calendar.session_status(evidence.price_date)
        if price_session is not True or evidence.price_date > instruction.execution_date:
            reasons.add("order_price_date_invalid")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "order_price_date_invalid",
                "order-price evidence date is not a session at or before execution",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue

        if side is Side.BUY:
            if not rule.quantity_is_admissible(
                side=Side.BUY, quantity=quantity, total_position_quantity=current_shares
            ):
                reasons.add("buy_delta_not_lot_conforming")
                _leg(
                    instrument_id, side, target_shares,
                    OrderPlanLegStatus.BLOCKED, "buy_delta_not_lot_conforming",
                    "the buy delta cannot satisfy the board's unit rules; the "
                    "target is never silently rounded",
                    lot_rule_id=rule.rule_id,
                    identity_record=identity.source_record_id,
                    price=evidence,
                    delta=delta,
                )
                continue
            fee_cap = fee_caps.get(instrument_id)
            if fee_cap is None:
                reasons.add("fee_cap_unknown")
                _leg(
                    instrument_id, side, target_shares,
                    OrderPlanLegStatus.BLOCKED, "fee_cap_unknown",
                    "a buy cannot reserve cash without an explicit worst-case "
                    "fee cap; production without a real fee table stays unknown",
                    lot_rule_id=rule.rule_id,
                    identity_record=identity.source_record_id,
                    price=evidence,
                    delta=delta,
                )
                continue
            if (
                fee_cap.instrument_id != instrument_id
                or fee_cap.account_id != account.account_id
                or fee_cap.trade_date != instruction.execution_date
            ):
                reasons.add("fee_quote_mismatch")
                _leg(
                    instrument_id, side, target_shares,
                    OrderPlanLegStatus.BLOCKED, "fee_quote_mismatch",
                    "the fee quote is bound to a different instrument, "
                    "account, or trade date",
                    lot_rule_id=rule.rule_id,
                    identity_record=identity.source_record_id,
                    price=evidence,
                    delta=delta,
                )
                continue
            worst_case_cash += int(evidence.price * quantity * 100) + fee_cap.cap_fen
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.ORDERABLE, "buy_plan_orderable",
                "raw limit price, lot rules, and fee cap resolved",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                fee_cap=fee_cap,
                delta=delta,
            )
            continue

        # SELL: normal lot rules apply, with the full odd-lot exit exception
        # evaluated against the WHOLE current position.
        if not rule.quantity_is_admissible(
            side=Side.SELL,
            quantity=quantity,
            total_position_quantity=current_shares,
        ):
            reasons.add("sell_delta_not_lot_conforming")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "sell_delta_not_lot_conforming",
                "the sell delta is not a legal sell quantity and is not a "
                "full odd-lot exit; the target is never silently rounded",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue
        if quantity > sellable.get(instrument_id, 0):
            reasons.add("insufficient_sellable_shares_t_plus_one")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "insufficient_sellable_shares_t_plus_one",
                "T+1 sellability cannot cover the planned sell quantity",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue
        # a sell reserves shares only, but it still carries the
        # order-lifetime cumulative fee budget: bind the quote when one is
        # available for this instrument (and reject a mismatched one)
        sell_fee_cap = fee_caps.get(instrument_id)
        if sell_fee_cap is not None and (
            sell_fee_cap.instrument_id != instrument_id
            or sell_fee_cap.account_id != account.account_id
            or sell_fee_cap.trade_date != instruction.execution_date
        ):
            reasons.add("fee_quote_mismatch")
            _leg(
                instrument_id, side, target_shares,
                OrderPlanLegStatus.BLOCKED, "fee_quote_mismatch",
                "the fee quote is bound to a different instrument, "
                "account, or trade date",
                lot_rule_id=rule.rule_id,
                identity_record=identity.source_record_id,
                price=evidence,
                delta=delta,
            )
            continue
        _leg(
            instrument_id, side, target_shares,
            OrderPlanLegStatus.ORDERABLE, "sell_plan_orderable",
            "raw limit price, lot rules, and T+1 sellability resolved",
            lot_rule_id=rule.rule_id,
            identity_record=identity.source_record_id,
            price=evidence,
            fee_cap=sell_fee_cap,
            delta=delta,
        )

    if reasons or any(leg.status is OrderPlanLegStatus.BLOCKED for leg in legs):
        status = OrderPlanStatus.BLOCKED
    else:
        status = OrderPlanStatus.SUBMIT_READY
    available_cash_fen = (
        execution_state.available_cash_fen
        if execution_state is not None
        else account.cash_fen
    )
    if (
        status is OrderPlanStatus.SUBMIT_READY
        and worst_case_cash > available_cash_fen
    ):
        # buys are funded only from current available cash; expected sell
        # proceeds never fund a buy
        reasons.add("aggregate_buy_worst_case_exceeds_available_cash")
        status = OrderPlanStatus.BLOCKED

    plan_payload = {
        "planner_version": PLANNER_VERSION,
        "instruction_id": instruction.instruction_id,
        "instruction_fingerprint": instruction_fingerprint,
        "account_fingerprint": account_fingerprint,
        "account_id": account.account_id,
        "execution_date": instruction.execution_date.isoformat(),
        "created_at": created_at.isoformat(),
        "availability_fingerprint": (
            execution_state.fingerprint
            if execution_state is not None
            else None
        ),
        "worst_case_cash_fen_required": worst_case_cash,
        "price_evidence": sorted(
            fingerprint_order_price_evidence(evidence)
            for evidence in order_prices.values()
            if isinstance(evidence, OrderPriceEvidence)
        ),
        "fee_quote_fingerprints": sorted(
            fingerprint_fee_cap_quote(quote)
            for quote in fee_caps.values()
        ),
        "legs": [
            {
                "leg_id": leg.leg_id,
                "instrument_id": leg.instrument_id,
                "side": leg.side.value if leg.side else None,
                "current_shares": leg.current_shares,
                "sellable_shares": leg.sellable_shares,
                "target_shares": leg.target_shares,
                "delta_shares": leg.delta_shares,
                "status": leg.status.value,
                "reason_code": leg.reason_code,
                "lot_rule_id": leg.lot_rule_id,
                "limit_price": (
                    str(leg.limit_price) if leg.limit_price is not None else None
                ),
                "limit_price_source_fingerprint": leg.limit_price_source_fingerprint,
                "limit_price_evidence_fingerprint":
                    leg.limit_price_evidence_fingerprint,
                "worst_case_fee_fen": leg.worst_case_fee_fen,
                "fee_quote_fingerprint": leg.fee_quote_fingerprint,
            }
            for leg in legs
        ],
    }
    plan_id = hashlib.sha256(
        json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OrderPlan(
        plan_id=plan_id,
        instruction_id=instruction.instruction_id,
        instruction_fingerprint=instruction_fingerprint,
        account_fingerprint=account_fingerprint,
        account_id=account.account_id,
        execution_date=instruction.execution_date,
        created_at=created_at,
        status=status,
        reason_codes=tuple(sorted(reasons)),
        legs=tuple(legs),
        worst_case_cash_fen_required=worst_case_cash,
        availability_fingerprint=(
            execution_state.fingerprint if execution_state is not None else None
        ),
    )
