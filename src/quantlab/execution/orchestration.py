"""Pure plan-to-order lineage adapter.

Turns a validated, account-aware :class:`OrderPlan` plus a
reservation-aware :class:`ExecutionStateView` into a deterministic batch of
:class:`OrderIntent` and broker-facing :class:`OrderRequest` objects. Every
artifact binds the full lineage: instruction fingerprint, plan id, leg id,
execution-state fingerprint, raw limit-price source fingerprint, fee-quote
fingerprint, intended trade date, and DAY time in force. Blocked plans,
blocked legs, and not-traded legs never materialize; the adapter is pure
(no I/O, no submission, no ledger access). Internal ledger events produced
later are ``OrderSubmitted`` ledger records, NOT external broker
submissions; formal artifacts must state
``external_broker_submission = false``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from quantlab.execution.models import (
    AssessmentAuthority,
    ExecutionValidationError,
    OrderIntent,
    OrderRequest,
    OrderType,
    RebalanceInstruction,
    TimeInForce,
    fingerprint_order_intent,
    require_aware,
)
from quantlab.execution.planning import (
    ExecutionStateView,
    FeeCapQuote,
    OrderPlan,
    OrderPlanLegStatus,
    fingerprint_fee_cap_quote,
    fingerprint_rebalance_instruction,
)


@dataclass(frozen=True)
class OrderBatch:
    """A deterministic, fully bound plan-to-intent batch.

    ``requests`` is always empty: requests are materialized ONLY from the
    stored assessment authority after the intent has been assessed inside
    the ledger (materialize_bound_request).
    """

    plan: OrderPlan
    intents: tuple[OrderIntent, ...]
    requests: tuple[OrderRequest, ...] = ()


def _binding_digest(plan_id: str, leg_id: str, suffix: str) -> str:
    return hashlib.sha256(
        f"{plan_id}\x1f{leg_id}\x1f{suffix}".encode()
    ).hexdigest()[:16]


def materialize_order_batch(
    instruction: RebalanceInstruction,
    plan: OrderPlan,
    *,
    state: ExecutionStateView,
    created_at: datetime,
) -> OrderBatch:
    """Materialize ORDERABLE legs of a SUBMIT_READY plan into intents.

    This is step 1 of the strict production order: plan adapter ->
    intents ONLY. Requests require the stored assessment authority and
    are created by :func:`materialize_bound_request` after the ledger has
    accepted the assessment.
    """
    require_aware(created_at, "created_at")
    if plan.status.value != "submit_ready":
        raise ExecutionValidationError(
            f"blocked plan {plan.plan_id} cannot materialize orders "
            f"(status {plan.status.value}, reasons "
            f"{list(plan.reason_codes)})"
        )
    if plan.instruction_id != instruction.instruction_id:
        raise ExecutionValidationError(
            "lineage mismatch: plan belongs to instruction "
            f"{plan.instruction_id}, not {instruction.instruction_id}"
        )
    if plan.instruction_fingerprint != fingerprint_rebalance_instruction(
        instruction
    ):
        raise ExecutionValidationError(
            "lineage mismatch: instruction content drifted from the plan"
        )
    if plan.availability_fingerprint != state.fingerprint:
        raise ExecutionValidationError(
            "lineage mismatch: account execution state drifted since the "
            "plan was built"
        )
    intents: list[OrderIntent] = []
    for leg in plan.legs:
        if leg.status is not OrderPlanLegStatus.ORDERABLE:
            # blocked and not-traded legs never become orders
            continue
        if leg.side is None or leg.limit_price is None:
            raise ExecutionValidationError(
                f"orderable leg {leg.leg_id} lacks a side or limit price"
            )
        digest = _binding_digest(plan.plan_id, leg.leg_id, leg.instrument_id)
        intent = OrderIntent(
            order_id=f"ord-{digest}",
            instruction_id=instruction.instruction_id,
            instrument_id=leg.instrument_id,
            side=leg.side,
            quantity=abs(leg.delta_shares),
            order_type=OrderType.LIMIT,
            limit_price=leg.limit_price,
            intended_trade_date=plan.execution_date,
            created_at=created_at,
            limit_price_basis=leg.limit_price_basis,
            limit_price_source_id=leg.limit_price_source_id,
            time_in_force=TimeInForce.DAY,
            plan_id=plan.plan_id,
            leg_id=leg.leg_id,
            availability_fingerprint=state.fingerprint,
            limit_price_source_fingerprint=leg.limit_price_source_fingerprint,
            fee_quote_fingerprint=leg.fee_quote_fingerprint,
        )
        intents.append(intent)
    return OrderBatch(plan=plan, intents=tuple(intents))


def materialize_bound_request(
    intent: OrderIntent,
    authority: AssessmentAuthority,
    *,
    fee_quote: FeeCapQuote,
    stored_authority: AssessmentAuthority,
) -> OrderRequest:
    """Materialize the broker-facing request from the STORED authority.

    Step 4 of the strict production order: only an assessment authority
    the ledger has persisted can produce a request. ``stored_authority``
    is the ledger's own copy and MUST be supplied; it must equal
    ``authority`` field for field, and the ledger re-verifies everything
    again at submission time.
    """
    if stored_authority != authority:
        raise ExecutionValidationError(
            "assessment authority does not match the authority stored in "
            "the ledger; re-assess before submitting"
        )
    if authority.intent_fingerprint != fingerprint_order_intent(intent):
        raise ExecutionValidationError(
            "assessment authority binds a different intent payload"
        )
    if authority.order_id != intent.order_id:
        raise ExecutionValidationError(
            "assessment authority is bound to "
            f"{authority.order_id}, not {intent.order_id}"
        )
    if fee_quote.instrument_id != intent.instrument_id:
        raise ExecutionValidationError(
            "fee quote instrument does not match the intent"
        )
    quote_fingerprint = fingerprint_fee_cap_quote(fee_quote)
    if intent.fee_quote_fingerprint is not None and (
        intent.fee_quote_fingerprint != quote_fingerprint
    ):
        raise ExecutionValidationError(
            "fee quote fingerprint differs from the plan-leg binding"
        )
    digest = _binding_digest(
        intent.plan_id or "adhoc",
        intent.leg_id or intent.order_id,
        intent.instrument_id,
    )
    return OrderRequest(
        request_id=f"req-{digest}",
        order_id=intent.order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        intended_trade_date=intent.intended_trade_date,
        # the request comes into existence with its authorizing assessment
        created_at=authority.assessed_at,
        session=intent.session,
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
        time_in_force=intent.time_in_force,
        instruction_id=authority.instruction_id,
        plan_id=authority.plan_id,
        leg_id=authority.leg_id,
        assessment_event_id=authority.assessment_event_id,
        assessment_decision_fingerprint=authority.decision_fingerprint,
        availability_fingerprint=authority.availability_fingerprint,
        limit_price_source_fingerprint=intent.limit_price_source_fingerprint,
        fee_quote_fingerprint=quote_fingerprint,
    )


def verify_lineage(
    intent: OrderIntent,
    instruction: RebalanceInstruction,
    plan: OrderPlan,
    state: ExecutionStateView,
) -> None:
    """Re-validate that an existing intent still matches its lineage.

    Any drift - a different plan, changed instruction content, or a moved
    account availability state - invalidates reuse of the old intent.
    """
    failures = []
    if intent.plan_id != plan.plan_id:
        failures.append(f"plan_id {intent.plan_id} != {plan.plan_id}")
    if intent.instruction_id != instruction.instruction_id:
        failures.append(
            f"instruction_id {intent.instruction_id} != "
            f"{instruction.instruction_id}"
        )
    if intent.availability_fingerprint != state.fingerprint:
        failures.append("availability_fingerprint drifted")
    if plan.instruction_fingerprint != fingerprint_rebalance_instruction(
        instruction
    ):
        failures.append("instruction fingerprint drifted from the plan")
    if failures:
        raise ExecutionValidationError(
            "order lineage invalid: " + "; ".join(failures)
        )
