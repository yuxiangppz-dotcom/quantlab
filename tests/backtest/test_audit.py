from datetime import date

import pandas as pd

from quantlab.backtest.audit import consumer_impact_audit, symmetry_audit
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)


def _frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["instrument_id", "trade_date"])


def _target(as_of, weights) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=as_of,
        positions=tuple(TargetWeight(i, w) for i, w in weights.items()),
        cash_weight=1.0 - sum(weights.values()),
    )


def test_consumer_impact_universe_other_date_not_counted() -> None:
    # A has a price row ON D1 (its delist date) but no universe/alpha/target
    # row ON D1; being in the universe on D0 must NOT count as delist impact.
    df = _frame([
        {"instrument_id": "A", "trade_date": D0},
        {"instrument_id": "A", "trade_date": D1},
    ])
    universe = _frame([{"instrument_id": "A", "trade_date": D0}])
    alpha_df = _frame([{"instrument_id": "A", "trade_date": D0}])
    targets = {D0: _target(D0, {"A": 1.0})}
    delist_map = {"A": D1}

    impact = consumer_impact_audit(df, universe, alpha_df, targets, delist_map)

    assert impact["price_rows_on_raw_delist_date"] == 1
    assert impact["universe_rows_on_raw_delist_date"] == 0
    assert impact["alpha_rows_on_raw_delist_date"] == 0
    assert impact["target_occurrences_on_raw_delist_date"] == 0


def test_consumer_impact_layers_counted_on_delist_date() -> None:
    # A has price + universe + alpha + target all ON its raw delist date D1.
    df = _frame([
        {"instrument_id": "A", "trade_date": D0},
        {"instrument_id": "A", "trade_date": D1},
    ])
    universe = _frame([
        {"instrument_id": "A", "trade_date": D0},
        {"instrument_id": "A", "trade_date": D1},
    ])
    alpha_df = _frame([
        {"instrument_id": "A", "trade_date": D0},
        {"instrument_id": "A", "trade_date": D1},
    ])
    targets = {
        D0: _target(D0, {"A": 1.0}),
        D1: _target(D1, {"A": 1.0}),
    }
    delist_map = {"A": D1}

    impact = consumer_impact_audit(df, universe, alpha_df, targets, delist_map)

    assert impact["price_rows_on_raw_delist_date"] == 1
    assert impact["universe_rows_on_raw_delist_date"] == 1
    assert impact["alpha_rows_on_raw_delist_date"] == 1
    assert impact["target_occurrences_on_raw_delist_date"] == 1
    assert impact["first_affected_date"] == D1.isoformat()
    assert impact["first_affected_stock"] == "A"


def test_consumer_impact_no_overlap_returns_zero() -> None:
    # A delisted at D1 but never appears in any frame -> zeros are computed,
    # not hardcoded.
    df = _frame([{"instrument_id": "B", "trade_date": D0}])
    universe = _frame([{"instrument_id": "B", "trade_date": D0}])
    alpha_df = _frame([{"instrument_id": "B", "trade_date": D0}])
    targets = {}
    delist_map = {"A": D1}

    impact = consumer_impact_audit(df, universe, alpha_df, targets, delist_map)

    assert impact["price_rows_on_raw_delist_date"] == 0
    assert impact["universe_rows_on_raw_delist_date"] == 0
    assert impact["alpha_rows_on_raw_delist_date"] == 0
    assert impact["target_occurrences_on_raw_delist_date"] == 0
    assert impact["first_affected_date"] is None
    assert impact["first_affected_stock"] is None


def test_symmetry_audit_mode_only_difference() -> None:
    legacy = {
        "data_fingerprint": "d1",
        "target_fingerprint": "t1",
        "config": {"cost_bps": 10.0},
        "lifecycle_mode": "legacy_delist_date_inclusive",
    }
    v1 = {
        "data_fingerprint": "d1",
        "target_fingerprint": "t1",
        "config": {"cost_bps": 10.0},
        "lifecycle_mode": "delist_date_is_first_invalid_v1",
    }
    result = symmetry_audit(legacy, v1)
    assert result["same_data_fingerprint"] == "d1"
    assert result["same_target_fingerprint"] == "t1"
    assert result["same_strategy_config"] == {"cost_bps": 10.0}
    assert result["different_lifecycle_mode_only"] is True


def test_symmetry_audit_detects_tampered_config() -> None:
    legacy = {
        "data_fingerprint": "d1",
        "target_fingerprint": "t1",
        "config": {"cost_bps": 10.0},
        "lifecycle_mode": "legacy_delist_date_inclusive",
    }
    tampered_config = dict(legacy)
    tampered_config["lifecycle_mode"] = "delist_date_is_first_invalid_v1"
    tampered_config["config"] = {"cost_bps": 50.0}

    result = symmetry_audit(legacy, tampered_config)
    assert result["same_strategy_config"] == "MISMATCH"
    assert result["different_lifecycle_mode_only"] is False


def test_symmetry_audit_detects_tampered_targets() -> None:
    legacy = {
        "data_fingerprint": "d1",
        "target_fingerprint": "t1",
        "config": {"cost_bps": 10.0},
        "lifecycle_mode": "legacy_delist_date_inclusive",
    }
    tampered_targets = dict(legacy)
    tampered_targets["lifecycle_mode"] = "delist_date_is_first_invalid_v1"
    tampered_targets["target_fingerprint"] = "t2"

    result = symmetry_audit(legacy, tampered_targets)
    assert result["same_target_fingerprint"] == "MISMATCH"
    assert result["different_lifecycle_mode_only"] is False


def test_symmetry_audit_same_mode_not_mode_only() -> None:
    legacy = {
        "data_fingerprint": "d1",
        "target_fingerprint": "t1",
        "config": {"cost_bps": 10.0},
        "lifecycle_mode": "legacy_delist_date_inclusive",
    }
    same_mode = dict(legacy)  # same mode -> not a "mode-only" difference

    result = symmetry_audit(legacy, same_mode)
    assert result["different_lifecycle_mode_only"] is False
