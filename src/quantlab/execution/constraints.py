"""Fail-closed pre-trade constraint assessment for the modeled rule subset."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from quantlab.execution.ledger import ConstraintsAssessed, account_state_fingerprint
from quantlab.execution.models import (
    AccountSnapshot,
    AssessmentAuthority,
    ConstraintDecision,
    ConstraintDimension,
    ConstraintStatus,
    ExecutionValidationError,
    OrderIntent,
    OrderStatus,
    OrderType,
    PriceBasis,
    Side,
    derive_order_status,
    fingerprint_decisions,
    fingerprint_order_intent,
    require_aware,
    require_identifier,
)
from quantlab.execution.rules import PITIdentityBook, PITRuleBook, TradingCalendar


class SuspensionState(StrEnum):
    VERIFIED_OPEN = "verified_open"
    VERIFIED_SUSPENDED = "verified_suspended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SuspensionEvidence:
    instrument_id: str
    trade_date: date
    state: SuspensionState
    observed_at: datetime
    source_record_ids: tuple[str, ...]
    coverage_complete: bool

    def __post_init__(self) -> None:
        require_identifier(self.instrument_id, "instrument_id")
        require_aware(self.observed_at, "observed_at")
        if not isinstance(self.state, SuspensionState):
            raise ExecutionValidationError("state must be a SuspensionState enum")
        if self.state is SuspensionState.VERIFIED_OPEN and not self.coverage_complete:
            raise ExecutionValidationError(
                "verified_open requires complete suspension coverage"
            )
        if (
            self.state is SuspensionState.VERIFIED_SUSPENDED
            and not self.source_record_ids
        ):
            raise ExecutionValidationError(
                "verified_suspended requires a positive source record"
            )
        if not isinstance(self.coverage_complete, bool):
            raise ExecutionValidationError("coverage_complete must be bool")
        for source_id in self.source_record_ids:
            require_identifier(source_id, "source_record_id")


@dataclass(frozen=True)
class FeeScheduleEvidence:
    schedule_id: str
    effective_from: date
    effective_to: date
    broker_commission_exact: bool
    statutory_fees_exact: bool
    source_sha256: str

    def __post_init__(self) -> None:
        require_identifier(self.schedule_id, "schedule_id")
        if self.effective_to < self.effective_from:
            raise ExecutionValidationError("fee schedule interval is reversed")
        if not isinstance(self.broker_commission_exact, bool) or not isinstance(
            self.statutory_fees_exact, bool
        ):
            raise ExecutionValidationError("fee exactness flags must be bool")
        if len(self.source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_sha256
        ):
            raise ExecutionValidationError("fee source_sha256 must be SHA-256")

    def covers(self, value: date) -> bool:
        return self.effective_from <= value <= self.effective_to


@dataclass(frozen=True)
class AssessmentResult:
    event: ConstraintsAssessed
    derived_status: OrderStatus


class AShareConstraintEngine:
    """Assess five independent dimensions without inventing fill evidence."""

    def __init__(
        self,
        *,
        calendar: TradingCalendar,
        identities: PITIdentityBook,
        rules: PITRuleBook,
    ) -> None:
        self.calendar = calendar
        self.identities = identities
        self.rules = rules

    def assess(
        self,
        intent: OrderIntent,
        account: AccountSnapshot,
        assessed_at: datetime,
        *,
        suspension: SuspensionEvidence | None,
        fee_schedule: FeeScheduleEvidence | None,
        daily_bar_available: bool | None,
        st_status: str | None = None,
        availability_fingerprint: str | None = None,
    ) -> AssessmentResult:
        """Return an auditable event; no side effect and no order submission.

        ``st_status`` is accepted only as disclosed context. It never rejects
        an order, forces an exit, or changes a target in this execution layer.
        ``availability_fingerprint`` binds the assessment to the full
        reservation-aware ledger state; the production path always supplies
        it (``ExecutionLedger.availability_fingerprint``), and the ledger
        rejects the assessment if that state has moved by append time.
        """
        require_aware(assessed_at, "assessed_at")
        if daily_bar_available is not None and not isinstance(
            daily_bar_available, bool
        ):
            raise ExecutionValidationError("daily_bar_available must be bool or None")
        if account.as_of > assessed_at:
            raise ExecutionValidationError("account snapshot is from the future")
        if suspension is not None:
            if suspension.instrument_id != intent.instrument_id:
                raise ExecutionValidationError("suspension evidence instrument mismatch")
            if suspension.trade_date != intent.intended_trade_date:
                raise ExecutionValidationError("suspension evidence trade_date mismatch")
            if suspension.observed_at > assessed_at:
                raise ExecutionValidationError("suspension evidence is from the future")
        _ = st_status  # explicitly non-binding in v0.1

        identity = self.identities.resolve(
            intent.instrument_id,
            intent.intended_trade_date,
        )
        rule = (
            self.rules.resolve(
                identity.exchange,
                identity.board,
                intent.intended_trade_date,
            )
            if identity is not None
            else None
        )
        decisions = (
            self._admissibility(intent, account, assessed_at, identity, rule),
            self._sellability(intent, account, assessed_at),
            self._market_access(intent, assessed_at, suspension),
            self._fillability(intent, assessed_at, daily_bar_available),
            self._fee_determinability(intent, assessed_at, fee_schedule),
        )
        derived = derive_order_status(intent.side, decisions)
        event_id = self._stable_id(
            intent.order_id,
            "assessment",
            assessed_at.isoformat(),
            *(decision.decision_id for decision in decisions),
        )
        fee_authority = (
            (fee_schedule.schedule_id, fee_schedule.source_sha256)
            if fee_schedule is not None
            else (None, None)
        )
        authority = AssessmentAuthority(
            assessment_event_id=event_id,
            order_id=intent.order_id,
            intent_fingerprint=fingerprint_order_intent(intent),
            decision_fingerprint=fingerprint_decisions(decisions),
            availability_fingerprint=availability_fingerprint or "unbound",
            assessed_at=assessed_at,
            dimension_statuses=tuple(
                (decision.dimension.value, decision.status.value)
                for decision in decisions
            ),
            fee_schedule_evidence_id=fee_authority[0],
            fee_schedule_source_fingerprint=fee_authority[1],
            instruction_id=intent.instruction_id,
            plan_id=intent.plan_id,
            leg_id=intent.leg_id,
        )
        return AssessmentResult(
            event=ConstraintsAssessed(
                event_id=event_id,
                occurred_at=assessed_at,
                order_id=intent.order_id,
                decisions=decisions,
                account_fingerprint=account_state_fingerprint(account),
                availability_fingerprint=availability_fingerprint,
                authority=authority,
            ),
            derived_status=derived,
        )

    def _decision(
        self,
        intent: OrderIntent,
        assessed_at: datetime,
        dimension: ConstraintDimension,
        status: ConstraintStatus,
        reason_code: str,
        message: str,
        rule_ids: tuple[str, ...] = (),
    ) -> ConstraintDecision:
        return ConstraintDecision(
            decision_id=self._stable_id(
                intent.order_id,
                dimension.value,
                assessed_at.isoformat(),
                status.value,
                reason_code,
                *rule_ids,
            ),
            dimension=dimension,
            status=status,
            reason_code=reason_code,
            message=message,
            assessed_at=assessed_at,
            rule_ids=rule_ids,
        )

    def _admissibility(self, intent, account, assessed_at, identity, rule):
        dimension = ConstraintDimension.ORDER_ADMISSIBILITY
        session_status = self.calendar.session_status(intent.intended_trade_date)
        if session_status is None:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "calendar_outside_coverage", "trade date is outside calendar coverage",
            )
        if session_status is False:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                "not_a_trading_session", "trade date is not an open session",
            )
        if identity is None:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "pit_identity_missing", "no identity record is valid on trade date",
            )
        if rule is None:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "pit_rule_missing", "no byte-verified historical rule covers this date",
            )
        if intent.order_type not in rule.supported_order_types:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "order_type_not_modeled", "official admissibility is not implemented",
                (rule.rule_id,),
            )
        if intent.session not in rule.supported_sessions:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "session_not_modeled", "auction/session mechanics are not implemented",
                (rule.rule_id,),
            )
        if intent.order_type is not OrderType.LIMIT:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "non_limit_not_modeled", "only limit orders are modeled in v0.1",
                (rule.rule_id,),
            )
        if intent.limit_price_basis is PriceBasis.ADJUSTED:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                "adjusted_price_not_executable",
                "adjusted research prices are never valid execution prices",
                (rule.rule_id,),
            )
        if intent.limit_price_basis is not PriceBasis.RAW:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "price_basis_unknown", "raw unadjusted price provenance is required",
                (rule.rule_id,),
            )
        if intent.limit_price % rule.price_tick != 0:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                "invalid_price_tick", f"price must be a multiple of {rule.price_tick}",
                (rule.rule_id,),
            )
        total_position = sum(
            lot.quantity
            for lot in account.lots
            if lot.instrument_id == intent.instrument_id
        )
        if not rule.quantity_is_admissible(
            side=intent.side,
            quantity=intent.quantity,
            total_position_quantity=total_position,
        ):
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                "invalid_order_quantity", "quantity violates board/date lot rules",
                (rule.rule_id,),
            )
        if intent.side is Side.BUY:
            gross_fen = intent.limit_price * intent.quantity * 100
            if gross_fen != gross_fen.to_integral_value():
                return self._decision(
                    intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                    "fractional_fen_notional", "explicit price rounding is required",
                    (rule.rule_id,),
                )
            if int(gross_fen) > account.cash_fen:
                return self._decision(
                    intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                    "insufficient_cash_before_fees",
                    "cash is below gross limit notional; fees would only increase need",
                    (rule.rule_id,),
                )
        return self._decision(
            intent, assessed_at, dimension, ConstraintStatus.ALLOWED,
            "modeled_order_terms_admissible",
            "calendar, identity, raw price, tick, quantity, and gross cash passed",
            (rule.rule_id,),
        )

    def _sellability(self, intent, account, assessed_at):
        dimension = ConstraintDimension.POSITION_SELLABILITY
        if intent.side is Side.BUY:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.NOT_APPLICABLE,
                "buy_has_no_position_sellability_test", "T+1 sellability applies to sells",
            )
        sellable = sum(
            lot.quantity
            for lot in account.lots
            if lot.instrument_id == intent.instrument_id
            and lot.sellable_from <= intent.intended_trade_date
        )
        if sellable < intent.quantity:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                "insufficient_sellable_shares",
                f"requested {intent.quantity}, sellable {sellable}; T+1 is binding",
            )
        return self._decision(
            intent, assessed_at, dimension, ConstraintStatus.ALLOWED,
            "shares_sellable", f"{sellable} shares are sellable on trade date",
        )

    def _market_access(self, intent, assessed_at, suspension):
        dimension = ConstraintDimension.MARKET_ACCESSIBILITY
        if suspension is None or suspension.state is SuspensionState.UNKNOWN:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "suspension_coverage_unknown",
                "absence of a suspension row is not evidence that trading was open",
            )
        if suspension.state is SuspensionState.VERIFIED_SUSPENDED:
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.REJECTED,
                "verified_suspension", "positive PIT suspension record blocks access",
                suspension.source_record_ids,
            )
        return self._decision(
            intent, assessed_at, dimension, ConstraintStatus.ALLOWED,
            "verified_not_suspended", "complete PIT coverage verifies market access",
            suspension.source_record_ids,
        )

    def _fillability(self, intent, assessed_at, daily_bar_available):
        if daily_bar_available is True:
            reason = "daily_bar_not_fill_evidence"
        elif daily_bar_available is False:
            reason = "missing_daily_bar_not_suspension_evidence"
        else:
            reason = "daily_bar_availability_unknown"
        return self._decision(
            intent,
            assessed_at,
            ConstraintDimension.FILLABILITY,
            ConstraintStatus.UNKNOWN,
            reason,
            "daily OHLCV cannot prove queue position, executable liquidity, or a fill",
        )

    def _fee_determinability(self, intent, assessed_at, fee_schedule):
        dimension = ConstraintDimension.FEE_DETERMINABILITY
        if (
            fee_schedule is None
            or not fee_schedule.covers(intent.intended_trade_date)
            or not fee_schedule.broker_commission_exact
            or not fee_schedule.statutory_fees_exact
        ):
            return self._decision(
                intent, assessed_at, dimension, ConstraintStatus.UNKNOWN,
                "exact_fee_schedule_missing",
                "account-specific commission and effective statutory fees are required",
            )
        return self._decision(
            intent, assessed_at, dimension, ConstraintStatus.ALLOWED,
            "exact_fee_schedule_available", "exact fee schedule covers trade date",
            (fee_schedule.schedule_id,),
        )

    @staticmethod
    def _stable_id(*parts: str) -> str:
        payload = "\x1f".join(parts).encode()
        return hashlib.sha256(payload).hexdigest()
