"""Frozen outcome-input admission for the single S5-B diagnostic."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_completion import S5BaseState
from quantlab.research.s5_base_decision import S5BaseAdmissionState
from quantlab.research.s5_base_diagnostic_protocol import (
    S5BaseDiagnosticProtocol,
    frozen_s5_base_diagnostic_protocol,
)
from quantlab.research.s5_base_diagnostic_readiness import (
    S5BaseDiagnosticPopulationKey,
    S5BaseDiagnosticReadiness,
    S5BaseDiagnosticReadinessVerdict,
    S5BaseEligibilityEvidence,
)

_SCHEMA = "quantlab_s5b_diagnostic_input_v1"
_WEIGHT_PER_NAME = 0.04
_MAX_NAMES = 20
_TOLERANCE = 1e-12


@dataclass(frozen=True)
class S5BaseDiagnosticSignalRow:
    instrument_id: str
    sector_id: str
    as_of: date
    base_state: S5BaseState
    admission_state: S5BaseAdmissionState
    selected: bool
    target_weight: float

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _require_text("sector_id", self.sector_id)
        if type(self.selected) is not bool:
            raise ValueError("selected must be bool")
        _finite_range("target_weight", self.target_weight, 0.0, 1.0)
        if self.admission_state is S5BaseAdmissionState.ELIGIBLE and self.base_state not in {
            S5BaseState.BASE_READY,
            S5BaseState.BREAKOUT_CONFIRMED,
        }:
            raise ValueError("eligible admission requires a ready or confirmed base state")
        if self.selected:
            if self.admission_state is not S5BaseAdmissionState.ELIGIBLE:
                raise ValueError("selected signal must be admitted as eligible")
            if not math.isclose(
                self.target_weight,
                _WEIGHT_PER_NAME,
                rel_tol=0.0,
                abs_tol=_TOLERANCE,
            ):
                raise ValueError("selected signal must use the frozen 4% target weight")
        elif self.target_weight != 0.0:
            raise ValueError("unselected signal must have zero target weight")

    @property
    def population_key(self) -> S5BaseDiagnosticPopulationKey:
        return S5BaseDiagnosticPopulationKey(self.instrument_id, self.as_of)


@dataclass(frozen=True)
class S5BaseDiagnosticTargetRow:
    as_of: date
    cash_weight: float

    def __post_init__(self) -> None:
        _finite_range("cash_weight", self.cash_weight, 0.0, 1.0)


@dataclass(frozen=True)
class S5BaseInstrumentOutcomeRow:
    instrument_id: str
    as_of: date
    horizon: int
    label_end_date: date
    close_return: float

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _validate_outcome(self.as_of, self.horizon, self.label_end_date, self.close_return)


@dataclass(frozen=True)
class S5BaseBenchmarkOutcomeRow:
    as_of: date
    horizon: int
    label_end_date: date
    close_return: float

    def __post_init__(self) -> None:
        _validate_outcome(self.as_of, self.horizon, self.label_end_date, self.close_return)


@dataclass(frozen=True)
class S5BaseComparisonOutcomeRow:
    comparison_id: str
    as_of: date
    horizon: int
    label_end_date: date
    close_return: float

    def __post_init__(self) -> None:
        _require_text("comparison_id", self.comparison_id)
        _validate_outcome(self.as_of, self.horizon, self.label_end_date, self.close_return)


@dataclass(frozen=True, order=True)
class S5BaseDiagnosticSource:
    source_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id)
        _require_text("fingerprint", self.fingerprint)


@dataclass(frozen=True)
class S5BaseDiagnosticInputPackage:
    schema: str
    protocol_fingerprint: str
    readiness_fingerprint: str
    intended_start: date
    intended_end: date
    population_size: int
    horizons: tuple[int, ...]
    comparison_ids: tuple[str, ...]
    signal_rows: tuple[S5BaseDiagnosticSignalRow, ...]
    target_rows: tuple[S5BaseDiagnosticTargetRow, ...]
    instrument_outcomes: tuple[S5BaseInstrumentOutcomeRow, ...]
    benchmark_outcomes: tuple[S5BaseBenchmarkOutcomeRow, ...]
    comparison_outcomes: tuple[S5BaseComparisonOutcomeRow, ...]
    sources: tuple[S5BaseDiagnosticSource, ...]
    label_semantics: str
    history_already_observed: bool = True
    diagnostic_only: bool = True
    executable_pnl: bool = False
    holding_policy_frozen: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.population_size <= 0:
            raise ValueError("population_size must be positive")
        for name in (
            "protocol_fingerprint",
            "readiness_fingerprint",
            "label_semantics",
        ):
            _require_text(name, getattr(self, name))
        if self.intended_start > self.intended_end:
            raise ValueError("intended_start cannot exceed intended_end")
        if (
            not self.history_already_observed
            or not self.diagnostic_only
            or self.executable_pnl
            or self.holding_policy_frozen
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("diagnostic input cannot acquire performance or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_package_payload(self)),
        )


def admit_s5_base_diagnostic_inputs(
    *,
    protocol: S5BaseDiagnosticProtocol,
    readiness: S5BaseDiagnosticReadiness,
    eligibility_evidence: tuple[S5BaseEligibilityEvidence, ...],
    signal_rows: tuple[S5BaseDiagnosticSignalRow, ...],
    target_rows: tuple[S5BaseDiagnosticTargetRow, ...],
    instrument_outcomes: tuple[S5BaseInstrumentOutcomeRow, ...],
    benchmark_outcomes: tuple[S5BaseBenchmarkOutcomeRow, ...],
    comparison_outcomes: tuple[S5BaseComparisonOutcomeRow, ...],
    sources: tuple[S5BaseDiagnosticSource, ...],
) -> S5BaseDiagnosticInputPackage:
    """Admit an exact, complete outcome package without calculating metrics."""

    frozen = frozen_s5_base_diagnostic_protocol()
    if protocol.fingerprint != frozen.fingerprint:
        raise ValueError("protocol must equal the frozen S5-B diagnostic protocol")
    if (
        readiness.verdict is not S5BaseDiagnosticReadinessVerdict.READY
        or readiness.blockers
    ):
        raise ValueError("readiness must authorize the single exact frozen run")
    if readiness.protocol_fingerprint != protocol.fingerprint:
        raise ValueError("readiness protocol fingerprint mismatch")
    if (
        readiness.admissible_start != readiness.intended_start
        or readiness.admissible_end != readiness.intended_end
    ):
        raise ValueError("readiness cannot admit a shrunken period")

    eligibility_by_key = _unique_eligibility(eligibility_evidence)
    if any(item.eligible is not True for item in eligibility_by_key.values()):
        raise ValueError("admitted eligibility evidence must be positive")
    if (
        _eligibility_fingerprint(tuple(eligibility_by_key.values()))
        != readiness.eligibility_evidence_fingerprint
    ):
        raise ValueError("eligibility evidence does not match readiness")
    intended_keys = set(eligibility_by_key)
    intended_dates = {key.as_of for key in intended_keys}
    if (
        len(intended_keys) != readiness.population_size
        or min(intended_dates) != readiness.intended_start
        or max(intended_dates) != readiness.intended_end
    ):
        raise ValueError("eligibility population does not match readiness bounds")

    signals = _unique(
        signal_rows,
        key=lambda row: row.population_key,
        label="signal row",
    )
    if set(signals) != intended_keys:
        raise ValueError("signal population must exactly match readiness population")

    targets = _unique(target_rows, key=lambda row: row.as_of, label="target row")
    if set(targets) != intended_dates:
        raise ValueError("target dates must exactly match signal dates")
    _validate_targets(tuple(signals.values()), targets)

    expected_instrument = {
        (key.instrument_id, key.as_of, horizon)
        for key in intended_keys
        for horizon in protocol.signal_horizons
    }
    admitted_instrument = _unique(
        instrument_outcomes,
        key=lambda row: (row.instrument_id, row.as_of, row.horizon),
        label="instrument outcome",
    )
    if set(admitted_instrument) != expected_instrument:
        raise ValueError("instrument outcomes must equal population x frozen horizons")

    expected_date_horizons = {
        (as_of, horizon)
        for as_of in intended_dates
        for horizon in protocol.signal_horizons
    }
    admitted_benchmark = _unique(
        benchmark_outcomes,
        key=lambda row: (row.as_of, row.horizon),
        label="benchmark outcome",
    )
    if set(admitted_benchmark) != expected_date_horizons:
        raise ValueError("benchmark outcomes must equal dates x frozen horizons")

    expected_comparisons = {
        (comparison_id, as_of, horizon)
        for comparison_id in protocol.comparison_ids
        for as_of, horizon in expected_date_horizons
    }
    admitted_comparisons = _unique(
        comparison_outcomes,
        key=lambda row: (row.comparison_id, row.as_of, row.horizon),
        label="comparison outcome",
    )
    if set(admitted_comparisons) != expected_comparisons:
        raise ValueError(
            "comparison outcomes must equal comparison ids x dates x horizons"
        )
    _validate_label_end_dates(
        admitted_instrument,
        admitted_benchmark,
        admitted_comparisons,
        intended_keys,
        protocol.signal_horizons,
    )
    _validate_broad_market_control(admitted_benchmark, admitted_comparisons)

    source_by_id = _unique(sources, key=lambda row: row.source_id, label="source")
    if not source_by_id:
        raise ValueError("sources cannot be empty")

    return S5BaseDiagnosticInputPackage(
        schema=_SCHEMA,
        protocol_fingerprint=protocol.fingerprint,
        readiness_fingerprint=readiness.fingerprint,
        intended_start=readiness.intended_start,
        intended_end=readiness.intended_end,
        population_size=readiness.population_size,
        horizons=protocol.signal_horizons,
        comparison_ids=protocol.comparison_ids,
        signal_rows=tuple(
            sorted(
                signals.values(),
                key=lambda row: (row.as_of, row.instrument_id),
            )
        ),
        target_rows=tuple(sorted(targets.values(), key=lambda row: row.as_of)),
        instrument_outcomes=tuple(
            sorted(
                admitted_instrument.values(),
                key=lambda row: (row.as_of, row.instrument_id, row.horizon),
            )
        ),
        benchmark_outcomes=tuple(
            sorted(
                admitted_benchmark.values(),
                key=lambda row: (row.as_of, row.horizon),
            )
        ),
        comparison_outcomes=tuple(
            sorted(
                admitted_comparisons.values(),
                key=lambda row: (row.comparison_id, row.as_of, row.horizon),
            )
        ),
        sources=tuple(sorted(source_by_id.values())),
        label_semantics=protocol.horizon_semantics,
    )


def _validate_targets(
    signals: tuple[S5BaseDiagnosticSignalRow, ...],
    targets: dict[date, S5BaseDiagnosticTargetRow],
) -> None:
    by_date: dict[date, list[S5BaseDiagnosticSignalRow]] = {}
    for row in signals:
        by_date.setdefault(row.as_of, []).append(row)
    for as_of, rows in by_date.items():
        selected = [row for row in rows if row.selected]
        if len(selected) > _MAX_NAMES:
            raise ValueError("selected signal count exceeds the frozen maximum")
        invested = math.fsum(row.target_weight for row in rows)
        if not math.isclose(
            invested + targets[as_of].cash_weight,
            1.0,
            rel_tol=0.0,
            abs_tol=_TOLERANCE,
        ):
            raise ValueError("target weights and cash must sum exactly to one")


def _validate_label_end_dates(
    instruments: dict[tuple[str, date, int], S5BaseInstrumentOutcomeRow],
    benchmarks: dict[tuple[date, int], S5BaseBenchmarkOutcomeRow],
    comparisons: dict[tuple[str, date, int], S5BaseComparisonOutcomeRow],
    intended_keys: set[S5BaseDiagnosticPopulationKey],
    horizons: tuple[int, ...],
) -> None:
    for as_of in sorted({key.as_of for key in intended_keys}):
        instruments_on_date = sorted(
            key.instrument_id for key in intended_keys if key.as_of == as_of
        )
        for horizon in horizons:
            end_dates = {
                instruments[(instrument_id, as_of, horizon)].label_end_date
                for instrument_id in instruments_on_date
            }
            end_dates.add(benchmarks[(as_of, horizon)].label_end_date)
            end_dates.update(
                row.label_end_date
                for key, row in comparisons.items()
                if key[1:] == (as_of, horizon)
            )
            if len(end_dates) != 1:
                raise ValueError("label_end_date must agree across every comparison")


def _validate_broad_market_control(
    benchmarks: dict[tuple[date, int], S5BaseBenchmarkOutcomeRow],
    comparisons: dict[tuple[str, date, int], S5BaseComparisonOutcomeRow],
) -> None:
    for (as_of, horizon), benchmark in benchmarks.items():
        control = comparisons[("broad_market_control", as_of, horizon)]
        if control.close_return != benchmark.close_return:
            raise ValueError("broad_market_control must equal the benchmark outcome")


def _unique_eligibility(
    evidence: tuple[S5BaseEligibilityEvidence, ...],
) -> dict[S5BaseDiagnosticPopulationKey, S5BaseEligibilityEvidence]:
    return _unique(
        evidence,
        key=lambda row: row.key,
        label="eligibility evidence",
    )


def _eligibility_fingerprint(
    evidence: tuple[S5BaseEligibilityEvidence, ...],
) -> str:
    return canonical_payload_fingerprint(
        [
            {
                "instrument_id": item.instrument_id,
                "as_of": item.as_of.isoformat(),
                "eligible": item.eligible,
                "source_id": item.source_id,
                "evidence_fingerprint": item.evidence_fingerprint,
            }
            for item in sorted(
                evidence,
                key=lambda value: (
                    value.as_of,
                    value.instrument_id,
                    value.source_id,
                ),
            )
        ]
    )


def _unique(rows, *, key, label):
    result = {}
    for row in rows:
        identity = key(row)
        if identity in result:
            raise ValueError(f"duplicate {label}: {identity}")
        result[identity] = row
    return result


def _validate_outcome(
    as_of: date,
    horizon: int,
    label_end_date: date,
    value: float,
) -> None:
    if type(horizon) is not int or horizon <= 0:
        raise ValueError("horizon must be a positive integer")
    if label_end_date <= as_of:
        raise ValueError("label_end_date must be after as_of")
    if not math.isfinite(value) or value <= -1.0:
        raise ValueError("close_return must be finite and greater than -1")


def _finite_range(name: str, value: float, low: float, high: float) -> None:
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")


def _package_payload(package: S5BaseDiagnosticInputPackage) -> dict[str, object]:
    def convert(value):
        if isinstance(value, date):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        if hasattr(value, "__dataclass_fields__"):
            return {
                name: convert(getattr(value, name))
                for name in value.__dataclass_fields__
                if name != "fingerprint"
            }
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        return value

    return {
        name: convert(getattr(package, name))
        for name in package.__dataclass_fields__
        if name != "fingerprint"
    }


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
