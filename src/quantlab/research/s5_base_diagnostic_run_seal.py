"""Immutable single-run seal for the frozen S5-B retrospective diagnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_diagnostic_inputs import S5BaseDiagnosticInputPackage
from quantlab.research.s5_base_diagnostic_metrics import (
    S5BaseDiagnosticMetrics,
    compute_s5_base_diagnostic_metrics,
)
from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)

_SCHEMA = "quantlab_s5b_diagnostic_run_seal_v1"
_REVIEW_STATUS = "awaiting_explicit_user_review"


@dataclass(frozen=True)
class S5BaseDiagnosticRunSeal:
    schema: str
    run_id: str
    strategy_id: str
    run_ordinal: int
    run_budget: int
    recorded_at: datetime
    protocol_fingerprint: str
    readiness_fingerprint: str
    input_fingerprint: str
    metrics_fingerprint: str
    review_status: str
    outcome_freeze_enforced: bool = True
    allow_parameter_rescan: bool = False
    history_already_observed: bool = True
    diagnostic_only: bool = True
    holding_policy_frozen: bool = False
    performance_claim: bool = False
    promotion_authority: bool = False
    account_mutation_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        protocol = frozen_s5_base_diagnostic_protocol()
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        for name in (
            "run_id",
            "strategy_id",
            "protocol_fingerprint",
            "readiness_fingerprint",
            "input_fingerprint",
            "metrics_fingerprint",
            "review_status",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.strategy_id != protocol.strategy_id:
            raise ValueError("strategy_id must equal the frozen S5-B strategy")
        if self.protocol_fingerprint != protocol.fingerprint:
            raise ValueError("protocol_fingerprint must equal the frozen protocol")
        if self.run_ordinal != 1 or self.run_budget != protocol.run_budget:
            raise ValueError("S5-B diagnostic seal must use the sole run ordinal")
        expected_run_id = _run_id_for(
            strategy_id=self.strategy_id,
            protocol_fingerprint=self.protocol_fingerprint,
            readiness_fingerprint=self.readiness_fingerprint,
            input_fingerprint=self.input_fingerprint,
            metrics_fingerprint=self.metrics_fingerprint,
        )
        if self.run_id != expected_run_id:
            raise ValueError("run_id does not match the sealed evidence")
        if self.recorded_at.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must be an aware UTC datetime")
        if self.review_status != _REVIEW_STATUS:
            raise ValueError("review_status cannot imply an automatic verdict")
        if (
            not self.outcome_freeze_enforced
            or self.allow_parameter_rescan
            or not self.history_already_observed
            or not self.diagnostic_only
            or self.holding_policy_frozen
            or self.performance_claim
            or self.promotion_authority
            or self.account_mutation_authority
            or self.broker_order_authority
        ):
            raise ValueError("run seal cannot acquire rescan, performance, or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_payload(self)),
        )


def seal_s5_base_diagnostic_run(
    *,
    package: S5BaseDiagnosticInputPackage,
    metrics: S5BaseDiagnosticMetrics,
    recorded_at: datetime,
    prior_seals: tuple[S5BaseDiagnosticRunSeal, ...] = (),
) -> S5BaseDiagnosticRunSeal:
    """Seal the sole diagnostic result, allowing only an exact idempotent retry."""

    protocol = frozen_s5_base_diagnostic_protocol()
    normalized_at = _utc(recorded_at)
    if protocol.run_budget != 1 or protocol.allow_parameter_rescan:
        raise ValueError("frozen protocol no longer authorizes the single-run seal")
    expected_metrics = compute_s5_base_diagnostic_metrics(package)
    if metrics != expected_metrics or metrics.fingerprint != expected_metrics.fingerprint:
        raise ValueError("metrics do not exactly match an independent recomputation")
    if (
        metrics.protocol_fingerprint != package.protocol_fingerprint
        or metrics.input_fingerprint != package.fingerprint
    ):
        raise ValueError("metrics are not bound to the admitted input package")

    _validate_prior_seals(prior_seals)
    relevant = tuple(
        seal
        for seal in prior_seals
        if seal.protocol_fingerprint == protocol.fingerprint
    )
    if relevant:
        existing = relevant[0]
        if (
            existing.strategy_id == protocol.strategy_id
            and existing.run_ordinal == 1
            and existing.run_budget == protocol.run_budget
            and existing.readiness_fingerprint == package.readiness_fingerprint
            and existing.input_fingerprint == package.fingerprint
            and existing.metrics_fingerprint == metrics.fingerprint
        ):
            return existing
        raise ValueError("single diagnostic run budget already consumed by different evidence")

    run_id = _run_id_for(
        strategy_id=protocol.strategy_id,
        protocol_fingerprint=protocol.fingerprint,
        readiness_fingerprint=package.readiness_fingerprint,
        input_fingerprint=package.fingerprint,
        metrics_fingerprint=metrics.fingerprint,
    )
    return S5BaseDiagnosticRunSeal(
        schema=_SCHEMA,
        run_id=run_id,
        strategy_id=protocol.strategy_id,
        run_ordinal=1,
        run_budget=protocol.run_budget,
        recorded_at=normalized_at,
        protocol_fingerprint=protocol.fingerprint,
        readiness_fingerprint=package.readiness_fingerprint,
        input_fingerprint=package.fingerprint,
        metrics_fingerprint=metrics.fingerprint,
        review_status=_REVIEW_STATUS,
    )


def _run_id_for(
    *,
    strategy_id: str,
    protocol_fingerprint: str,
    readiness_fingerprint: str,
    input_fingerprint: str,
    metrics_fingerprint: str,
) -> str:
    return canonical_payload_fingerprint(
        {
            "schema": _SCHEMA,
            "strategy_id": strategy_id,
            "run_ordinal": 1,
            "protocol_fingerprint": protocol_fingerprint,
            "readiness_fingerprint": readiness_fingerprint,
            "input_fingerprint": input_fingerprint,
            "metrics_fingerprint": metrics_fingerprint,
        }
    )


def _validate_prior_seals(
    prior_seals: tuple[S5BaseDiagnosticRunSeal, ...],
) -> None:
    run_ids = [seal.run_id for seal in prior_seals]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("prior seal ledger contains a duplicate run_id")
    protocol_fingerprints = [seal.protocol_fingerprint for seal in prior_seals]
    if len(protocol_fingerprints) != len(set(protocol_fingerprints)):
        raise ValueError("prior seal ledger already exceeds a protocol run budget")


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _payload(seal: S5BaseDiagnosticRunSeal) -> dict[str, object]:
    return {
        name: (
            getattr(seal, name).isoformat()
            if isinstance(getattr(seal, name), datetime)
            else getattr(seal, name)
        )
        for name in seal.__dataclass_fields__
        if name != "fingerprint"
    }
