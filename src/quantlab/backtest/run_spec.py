"""BacktestRunSpec: the single audited source of truth for formal runs.

A spec is constructed once from the actual invocation inputs and is then the
ONLY way the runner submits a backtest to the engine — ``spec.run()`` generates
the ``run_backtest`` kwargs from the spec's own fields. Every audit
fingerprint (lifecycle mode, monitor state, risk facts, targets) is DERIVED
from the actual objects the spec holds — callers cannot supply self-certifying
strings — and ``run()``/``strategy_control_symmetry_audit`` fail hard if the
underlying objects drift after construction. The strategy/control symmetry
audit therefore compares what was actually executed; post-hoc mirror dicts
re-typed from constants are rejected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from quantlab.backtest.audit import fingerprint_targets
from quantlab.backtest.engine import MISSING_PRICE_POLICY, run_backtest
from quantlab.portfolio.models import TargetPortfolio

# (spec-derived value, audit check name) — every field that can move
# performance; target construction and its fingerprint are the ONLY allowed
# asymmetry and are deliberately not part of this table.
_SYMMETRY_FIELDS: tuple[tuple[str, str], ...] = (
    ("open_dates", "same_open_dates"),
    ("signal_dates", "same_signal_schedule"),
    ("execution_lag_sessions", "same_execution_lag"),
    ("cost_bps", "same_cost_bps"),
    ("missing_price_policy", "same_missing_price_policy"),
    ("mode", "same_run_mode"),
    ("lifecycle_mode", "same_lifecycle_boundary_mode"),
    ("lifecycle_monitor_snapshot", "same_lifecycle_monitor_snapshot"),
    ("risk_policy", "same_risk_policy_id"),
    ("risk_fact_snapshot", "same_risk_fact_snapshot"),
    ("settlement_recovery_rate", "same_settlement_recovery_rate"),
    ("settlement_fee_bps", "same_settlement_fee_bps"),
    ("initial_nav", "same_initial_nav"),
    ("requested_period", "same_requested_period"),
    ("annualization", "same_annualization"),
)


def fingerprint_risk_facts(risk_facts: Mapping) -> str:
    """Deterministic content fingerprint of a risk-fact snapshot."""
    payload = json.dumps(risk_facts, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def fingerprint_lifecycle_monitor(monitor) -> str:
    """Deterministic fingerprint of the monitor's ACTUAL state.

    Hashes the mode and the delist/code-change maps the monitor will
    actually enforce, so a snapshot can never certify anything other than
    the live object's behavior.
    """
    payload = {
        "mode": monitor.mode,
        "delist_map": sorted(
            (k, v.isoformat()) for k, v in monitor.delist_map.items()
        ),
        "code_change_map": sorted(
            (k, v.isoformat()) for k, v in monitor.code_change_map.items()
        ),
        "conflicts": sorted(monitor.conflicts),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BacktestRunSpec:
    """One formal backtest invocation, executable and auditable.

    The lifecycle mode, monitor snapshot, risk-fact fingerprint and target
    fingerprint are derived from the actual objects at construction time;
    ``run()`` and the symmetry audit re-derive them and fail hard on any
    drift, so a caller can never present a stale or forged certification.
    """

    label: str
    price_frame: pd.DataFrame
    open_dates: tuple[date, ...]
    targets: Mapping[date, TargetPortfolio]
    config: object  # BacktestConfig (kept untyped to avoid a cycle)
    execution_lag_sessions: int
    mode: str
    lifecycle: object  # LifecycleMonitor
    requested_period_start: date
    requested_period_end: date
    risk_facts: Mapping
    risk_policy: str
    notes: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        self._bind()

    def _bind(self) -> None:
        """Derive and freeze the audit fingerprints from actual objects."""
        object.__setattr__(
            self, "lifecycle_mode", getattr(self.lifecycle, "mode", None)
        )
        object.__setattr__(
            self,
            "lifecycle_monitor_snapshot",
            fingerprint_lifecycle_monitor(self.lifecycle),
        )
        object.__setattr__(
            self, "risk_fact_snapshot", fingerprint_risk_facts(self.risk_facts)
        )
        object.__setattr__(
            self, "targets_fingerprint", fingerprint_targets(dict(self.targets))
        )

    def verify_bindings(self) -> None:
        """Fail hard if the underlying objects drifted after construction."""
        expected = {
            "lifecycle_mode": getattr(self.lifecycle, "mode", None),
            "lifecycle_monitor_snapshot": fingerprint_lifecycle_monitor(
                self.lifecycle
            ),
            "risk_fact_snapshot": fingerprint_risk_facts(self.risk_facts),
            "targets_fingerprint": fingerprint_targets(dict(self.targets)),
        }
        drifted = {
            name: (getattr(self, name), value)
            for name, value in expected.items()
            if getattr(self, name) != value
        }
        if drifted:
            raise RuntimeError(
                "run spec binding drift detected (objects were mutated after "
                f"construction): {drifted}"
            )

    def engine_kwargs(self) -> dict:
        """The exact kwargs submitted to ``run_backtest`` — generated from
        this spec's own fields, so audit and execution cannot drift apart."""
        return {
            "price_frame": self.price_frame,
            "open_dates": list(self.open_dates),
            "targets": dict(self.targets),
            "config": self.config,
            "execution_lag_sessions": self.execution_lag_sessions,
            "mode": self.mode,
            "lifecycle": self.lifecycle,
            "requested_period_start": self.requested_period_start,
            "requested_period_end": self.requested_period_end,
            "risk_facts": self.risk_facts,
            "risk_policy": self.risk_policy,
        }

    def run(self):
        """Execute the backtest described by this spec."""
        self.verify_bindings()
        return run_backtest(**self.engine_kwargs())

    def symmetry_fields(self) -> dict:
        settlement = getattr(self.config, "delisting_settlement", None)
        self.verify_bindings()
        return {
            "open_dates": list(self.open_dates),
            "signal_dates": sorted(self.targets),
            "execution_lag_sessions": self.execution_lag_sessions,
            "cost_bps": self.config.transaction_cost_bps,
            "missing_price_policy": MISSING_PRICE_POLICY,
            "mode": self.mode,
            "lifecycle_mode": self.lifecycle_mode,
            "lifecycle_monitor_snapshot": self.lifecycle_monitor_snapshot,
            "risk_policy": self.risk_policy,
            "risk_fact_snapshot": self.risk_fact_snapshot,
            "settlement_recovery_rate": (
                settlement.recovery_rate if settlement is not None else None
            ),
            "settlement_fee_bps": (
                settlement.settlement_fee_bps if settlement is not None else None
            ),
            "initial_nav": self.config.initial_nav,
            "requested_period": (
                self.requested_period_start.isoformat(),
                self.requested_period_end.isoformat(),
            ),
            "annualization": self.config.annualization,
        }


def strategy_control_symmetry_audit(
    strategy_spec: BacktestRunSpec,
    control_spec: BacktestRunSpec,
) -> dict[str, bool]:
    """Audit that the two ACTUAL run specs differ only in target construction.

    Both arguments must be :class:`BacktestRunSpec` instances — the objects
    whose ``engine_kwargs()`` were submitted to the engine. Mirror dicts are
    rejected (``TypeError``). Every spec-derived field that can move
    performance is compared explicitly. A formal strategy/control pair must
    differ in label AND in actual target fingerprints — identical
    construction can never pass as a control — and every execution field
    outside target construction must be identical.
    """
    if not isinstance(strategy_spec, BacktestRunSpec) or not isinstance(
        control_spec, BacktestRunSpec
    ):
        raise TypeError(
            "symmetry audit requires BacktestRunSpec instances taken from the "
            "actual engine invocations, not mirror dicts"
        )
    strategy_fields = strategy_spec.symmetry_fields()
    control_fields = control_spec.symmetry_fields()
    missing = {
        name
        for name, _ in _SYMMETRY_FIELDS
        if name not in strategy_fields or name not in control_fields
    }
    if missing:
        raise ValueError(f"run specs missing symmetry fields: {sorted(missing)}")
    checks = {
        audit_name: strategy_fields[spec_key] == control_fields[spec_key]
        for spec_key, audit_name in _SYMMETRY_FIELDS
    }
    if strategy_spec.label == control_spec.label:
        raise RuntimeError(
            "formal strategy and control must carry different labels; "
            f"got {strategy_spec.label!r} twice"
        )
    if strategy_spec.targets_fingerprint == control_spec.targets_fingerprint:
        raise RuntimeError(
            "formal strategy and control must be built by different target "
            "constructions (identical target fingerprints)"
        )
    checks["same_target_construction_allowed_to_differ"] = True
    return checks
