"""BacktestRunSpec: the audited single source of truth for formal runs.

The symmetry audit must read the specs that were ACTUALLY submitted to the
engine — never mirror dicts re-typed from constants after the fact.
"""

from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    EXIT_POLICY_ID,
    LEGACY_DELIST_DATE_INCLUSIVE,
    BacktestConfig,
    LifecycleMonitor,
    strategy_control_symmetry_audit,
)
from quantlab.backtest.run_spec import BacktestRunSpec, fingerprint_risk_facts
from quantlab.data.models import Security
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


def _monitor() -> LifecycleMonitor:
    sec = Security(
        instrument_id="A.SZ", symbol="A", name="x", exchange="SZSE",
        market="SZ", board="主板", list_status="L",
        list_date=date(2000, 1, 1), delist_date=None,
    )
    return LifecycleMonitor([sec], [])


def _facts() -> dict:
    return {"A": [{"fact_id": "f1"}]}


def _spec(**overrides) -> BacktestRunSpec:
    from quantlab.backtest import DelistingSettlementConfig

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
        lifecycle_mode=LEGACY_DELIST_DATE_INCLUSIVE,
        lifecycle_monitor_snapshot="sha256:monitor",
        requested_period_start=D0,
        requested_period_end=D2,
        risk_facts=_facts(),
        risk_fact_snapshot="sha256:facts",
        risk_policy=EXIT_POLICY_ID,
    )
    kwargs.update(overrides)
    return BacktestRunSpec(**kwargs)


def test_engine_kwargs_match_spec_fields() -> None:
    """The spec itself generates the engine invocation: audit and execution
    cannot drift apart."""
    spec = _spec()
    kwargs = spec.engine_kwargs()
    assert kwargs["open_dates"] == [D0, D1, D2]
    assert kwargs["execution_lag_sessions"] == 1
    assert kwargs["mode"] == "strict"
    assert kwargs["lifecycle"] is spec.lifecycle
    assert kwargs["config"] is spec.config
    assert kwargs["risk_policy"] == EXIT_POLICY_ID
    assert kwargs["requested_period_start"] == D0
    assert kwargs["requested_period_end"] == D2


def test_symmetry_audit_passes_for_two_real_specs() -> None:
    checks = strategy_control_symmetry_audit(_spec(), _spec(label="control"))
    assert checks["same_execution_lag"] is True
    assert checks["same_cost_bps"] is True
    assert checks["same_lifecycle_boundary_mode"] is True
    assert checks["same_lifecycle_monitor_snapshot"] is True
    assert checks["same_risk_fact_snapshot"] is True
    assert checks["same_settlement_fee_bps"] is True
    assert checks["same_settlement_recovery_rate"] is True
    assert checks["same_initial_nav"] is True
    assert checks["same_annualization"] is True
    assert checks["same_requested_period"] is True
    assert checks["same_signal_schedule"] is True
    assert checks["same_open_dates"] is True
    assert checks["same_run_mode"] is True
    assert checks["same_risk_policy_id"] is True
    assert checks["same_missing_price_policy"] is True


_CONFIG_LEVEL = (
    "settlement_fee_bps", "settlement_recovery_rate", "initial_nav",
    "annualization", "cost_bps",
)


def _control_kwargs(override: dict) -> dict:
    """Map config-level overrides into a replacement BacktestConfig."""
    config_level = {k: override.pop(k) for k in list(override) if k in _CONFIG_LEVEL}
    if not config_level:
        return dict(override)
    from quantlab.backtest import DelistingSettlementConfig

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
        ({"lifecycle_mode": DELIST_DATE_IS_FIRST_INVALID_V1}, "same_lifecycle_boundary_mode"),
        ({"lifecycle_monitor_snapshot": "sha256:other"}, "same_lifecycle_monitor_snapshot"),
        ({"risk_fact_snapshot": "sha256:otherfacts"}, "same_risk_fact_snapshot"),
        ({"risk_policy": "other_policy"}, "same_risk_policy_id"),
        ({"mode": "diagnostic"}, "same_run_mode"),
        ({"initial_nav": 2.0}, "same_initial_nav"),
        ({"annualization": 244}, "same_annualization"),
        ({"requested_period_start": D1}, "same_requested_period"),
    ],
)
def test_symmetry_audit_fails_on_any_tampered_field(override, field) -> None:
    """Changing control's ACTUAL execution parameters must flip the audit."""
    control = _spec(label="control", **_control_kwargs(dict(override)))
    checks = strategy_control_symmetry_audit(_spec(), control)
    flipped = [k for k, v in checks.items() if v is False]
    assert flipped, "tampering must flip at least one check"
    assert field in flipped


def test_symmetry_audit_rejects_non_spec_inputs() -> None:
    """Mirror dicts re-typed after the fact are no longer acceptable audit
    inputs."""
    with pytest.raises(TypeError):
        strategy_control_symmetry_audit({"open_dates": [D0]}, {"open_dates": [D0]})


def test_fact_fingerprint_binds_content() -> None:
    """Two fact snapshots differing in content must carry different
    fingerprints, so the audit cannot confuse them."""
    a = fingerprint_risk_facts(_facts())
    b = fingerprint_risk_facts({"A": [{"fact_id": "f2"}]})
    assert a != b
    assert a == fingerprint_risk_facts(_facts())
