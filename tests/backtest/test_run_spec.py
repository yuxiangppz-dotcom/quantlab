"""BacktestRunSpec: the single audited source of truth for formal runs.

The spec derives every audit fingerprint from the ACTUAL objects it will
submit to the engine (monitor state, risk facts, targets). Callers cannot
supply self-certifying fingerprint/mode strings, and ``run()``/audit fail
hard if the underlying objects are mutated after construction.
"""

from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    EXIT_POLICY_ID,
    LEGACY_DELIST_DATE_INCLUSIVE,
    BacktestConfig,
    DelistingSettlementConfig,
    LifecycleMonitor,
    strategy_control_symmetry_audit,
)
from quantlab.backtest.run_spec import BacktestRunSpec, fingerprint_risk_facts
from quantlab.data.models import Security, SecurityCodeChange
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)


def _prices() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": "A", "trade_date": d, "adj_close": 100.0}
            for d in (D0, D1, D2)
        ]
    )


def _targets(weight: float = 1.0) -> dict:
    return {
        D0: TargetPortfolio(
            as_of=D0,
            positions=(TargetWeight("A", weight),),
            cash_weight=1.0 - weight,
        ),
    }


def _monitor(delist: date | None = None) -> LifecycleMonitor:
    sec = Security(
        instrument_id="A.SZ", symbol="A", name="x", exchange="SZSE",
        market="SZ", board="主板", list_status="L",
        list_date=date(2000, 1, 1), delist_date=delist,
    )
    change = SecurityCodeChange(
        old_instrument_id="OLD.SZ", new_instrument_id="NEW.SZ",
        effective_date=date(2027, 1, 1), old_name="x",
        original_list_date=date(2000, 1, 1),
    )
    return LifecycleMonitor([sec], [change])


def _facts() -> dict:
    return {"A": [{"fact_id": "f1"}]}


def _spec(**overrides) -> BacktestRunSpec:
    kwargs = dict(
        label="strategy",
        price_frame=_prices(),
        open_dates=(D0, D1, D2),
        targets=_targets(),
        config=BacktestConfig(
            initial_nav=1.0,
            transaction_cost_bps=10.0,
            annualization=252,
            delisting_settlement=DelistingSettlementConfig(
                recovery_rate=1.0, settlement_fee_bps=0.0
            ),
        ),
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=_monitor(),
        requested_period_start=D0,
        requested_period_end=D2,
        risk_facts=_facts(),
        risk_policy=EXIT_POLICY_ID,
    )
    kwargs.update(overrides)
    return BacktestRunSpec(**kwargs)


def _control_spec(**overrides) -> BacktestRunSpec:
    """A production-shaped control spec: different label AND different
    target construction from the strategy."""
    overrides.setdefault("label", "control")
    overrides.setdefault("targets", _targets(weight=0.5))
    return _spec(**overrides)


def test_engine_kwargs_match_spec_fields() -> None:
    spec = _spec()
    kwargs = spec.engine_kwargs()
    assert kwargs["open_dates"] == [D0, D1, D2]
    assert kwargs["execution_lag_sessions"] == 1
    assert kwargs["mode"] == "strict"
    assert kwargs["lifecycle"] is spec.lifecycle
    assert kwargs["config"] is spec.config
    assert kwargs["risk_policy"] == EXIT_POLICY_ID


def test_spec_derives_lifecycle_mode_from_actual_monitor() -> None:
    legacy = _spec()
    assert legacy.lifecycle_mode == LEGACY_DELIST_DATE_INCLUSIVE
    v1 = _spec(lifecycle=_monitor(), mode="strict")
    v1 = BacktestRunSpec(
        label="v1",
        price_frame=_prices(),
        open_dates=(D0, D1, D2),
        targets=_targets(),
        config=BacktestConfig(annualization=252),
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=LifecycleMonitor(
            [Security(
                instrument_id="A.SZ", symbol="A", name="x", exchange="SZSE",
                market="SZ", board="主板", list_status="L",
                list_date=date(2000, 1, 1), delist_date=None,
            )],
            [],
            mode=DELIST_DATE_IS_FIRST_INVALID_V1,
        ),
        requested_period_start=D0,
        requested_period_end=D2,
        risk_facts=_facts(),
        risk_policy=EXIT_POLICY_ID,
    )
    assert v1.lifecycle_mode == DELIST_DATE_IS_FIRST_INVALID_V1
    assert v1.lifecycle_mode != legacy.lifecycle_mode


def test_spec_derives_monitor_fingerprint_from_actual_state() -> None:
    plain = _spec()
    with_delist = _spec(lifecycle=_monitor(delist=date(2027, 6, 1)))
    assert plain.lifecycle_monitor_snapshot != with_delist.lifecycle_monitor_snapshot
    # identical monitor state -> identical snapshot
    assert plain.lifecycle_monitor_snapshot == _spec().lifecycle_monitor_snapshot


def test_spec_derives_fact_fingerprint_from_actual_facts() -> None:
    spec = _spec()
    assert spec.risk_fact_snapshot == fingerprint_risk_facts(_facts())
    other = _spec(risk_facts={"A": [{"fact_id": "f2"}]})
    assert other.risk_fact_snapshot != spec.risk_fact_snapshot


def test_spec_derives_target_fingerprint_from_actual_targets() -> None:
    spec = _spec()
    assert spec.targets_fingerprint == spec.targets_fingerprint
    half = _spec(targets=_targets(weight=0.5))
    assert half.targets_fingerprint != spec.targets_fingerprint


def test_run_fails_when_monitor_mutated_after_construction() -> None:
    spec = _spec()
    spec.lifecycle.code_change_map["OLD.SZ"] = date(2026, 1, 1)  # mutate state
    with pytest.raises(RuntimeError, match="drift"):
        spec.run()


def test_symmetry_audit_fails_when_monitor_mutated_after_construction() -> None:
    strategy, control = _spec(), _control_spec()
    control.lifecycle.delist_map["A.SZ"] = date(2026, 6, 1)  # mutate state
    with pytest.raises(RuntimeError, match="drift"):
        strategy_control_symmetry_audit(strategy, control)


def test_symmetry_audit_passes_for_two_real_specs() -> None:
    checks = strategy_control_symmetry_audit(_spec(), _control_spec())
    assert all(
        value is True
        for name, value in checks.items()
        if name != "same_target_construction_allowed_to_differ"
    )
    assert checks["same_target_construction_allowed_to_differ"] is True


def test_symmetry_audit_requires_different_labels_and_targets() -> None:
    """Formal strategy/control must differ in label AND actual target
    fingerprints; identical construction can never pass as a control."""
    with pytest.raises(RuntimeError, match="label"):
        strategy_control_symmetry_audit(_spec(), _spec())
    with pytest.raises(RuntimeError, match="target"):
        strategy_control_symmetry_audit(
            _spec(label="strategy", targets=_targets(weight=0.5)),
            _control_spec(),
        )


_CONFIG_LEVEL = (
    "settlement_fee_bps", "settlement_recovery_rate", "initial_nav",
    "annualization", "cost_bps",
)


def _control_kwargs(override: dict) -> dict:
    """Map config-level overrides into a replacement BacktestConfig."""
    config_level = {k: override.pop(k) for k in list(override) if k in _CONFIG_LEVEL}
    if not config_level:
        return dict(override)
    settlement = DelistingSettlementConfig(
        recovery_rate=config_level.get("settlement_recovery_rate", 1.0),
        settlement_fee_bps=config_level.get("settlement_fee_bps", 0.0),
    )
    config = BacktestConfig(
        initial_nav=config_level.get("initial_nav", 1.0),
        transaction_cost_bps=config_level.get("cost_bps", 10.0),
        annualization=config_level.get("annualization", 252),
        delisting_settlement=settlement,
    )
    return dict(override, config=config)


@pytest.mark.parametrize(
    "override,field",
    [
        ({"execution_lag_sessions": 2}, "same_execution_lag"),
        ({"cost_bps": 0.0}, "same_cost_bps"),
        ({"settlement_fee_bps": 5.0}, "same_settlement_fee_bps"),
        ({"settlement_recovery_rate": 0.0}, "same_settlement_recovery_rate"),
        ({"mode": "diagnostic"}, "same_run_mode"),
        ({"initial_nav": 2.0}, "same_initial_nav"),
        ({"annualization": 244}, "same_annualization"),
        ({"requested_period_start": D1}, "same_requested_period"),
        ({"risk_policy": "other_policy"}, "same_risk_policy_id"),
    ],
)
def test_symmetry_audit_fails_on_any_tampered_field(override, field) -> None:
    """Changing control's ACTUAL execution parameters must flip the audit."""
    control = _control_spec(**_control_kwargs(dict(override)))
    checks = strategy_control_symmetry_audit(_spec(), control)
    flipped = [k for k, v in checks.items() if v is False]
    assert flipped, "tampering must flip at least one check"
    assert field in flipped


def test_symmetry_audit_fails_on_different_actual_monitors() -> None:
    control = _control_spec(lifecycle=_monitor(delist=date(2026, 6, 1)))
    checks = strategy_control_symmetry_audit(_spec(), control)
    assert checks["same_lifecycle_monitor_snapshot"] is False
    assert checks["same_lifecycle_boundary_mode"] is True  # mode alone is not identity


def test_symmetry_audit_fails_on_different_lifecycle_modes() -> None:
    v1_monitor = LifecycleMonitor(
        [Security(
            instrument_id="A.SZ", symbol="A", name="x", exchange="SZSE",
            market="SZ", board="主板", list_status="L",
            list_date=date(2000, 1, 1), delist_date=None,
        )],
        [],
        mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )
    control = _control_spec(lifecycle=v1_monitor)
    checks = strategy_control_symmetry_audit(_spec(), control)
    assert checks["same_lifecycle_boundary_mode"] is False
    assert checks["same_lifecycle_monitor_snapshot"] is False


def test_symmetry_audit_fails_on_different_risk_facts() -> None:
    control = _control_spec(risk_facts={"A": [{"fact_id": "f2"}]})
    checks = strategy_control_symmetry_audit(_spec(), control)
    assert checks["same_risk_fact_snapshot"] is False


def test_symmetry_audit_rejects_non_spec_inputs() -> None:
    with pytest.raises(TypeError):
        strategy_control_symmetry_audit({"open_dates": [D0]}, {"open_dates": [D0]})


def test_fact_fingerprint_binds_content() -> None:
    a = fingerprint_risk_facts(_facts())
    b = fingerprint_risk_facts({"A": [{"fact_id": "f2"}]})
    assert a != b
    assert a == fingerprint_risk_facts(_facts())
