from datetime import date

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.portfolio import (
    DAILY_FIXED_COUNT_TIE_POLICY,
    construct_daily_fixed_count_portfolio,
    fixed_count_config_from_daily,
)

DAY = date(2026, 9, 9)


def _config(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_count": 2,
        "score_direction": "higher_is_better",
        "gross_exposure": 1.0,
        "max_weight_per_name": 0.4,
        "tie_policy": DAILY_FIXED_COUNT_TIE_POLICY,
    }
    payload.update(overrides)
    return payload


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": "000003.SZ", "trade_date": DAY, "alpha_score": 2.0},
            {"instrument_id": "000001.SZ", "trade_date": DAY, "alpha_score": 3.0},
            {"instrument_id": "000002.SZ", "trade_date": DAY, "alpha_score": 2.0},
        ]
    )


def test_daily_adapter_preserves_exact_product_contract() -> None:
    config = fixed_count_config_from_daily(_config())

    assert config.target_count == 2
    assert config.score_direction == "higher_is_better"
    assert config.gross_exposure == pytest.approx(1.0)
    assert config.max_weight_per_name == pytest.approx(0.4)

    portfolio = construct_daily_fixed_count_portfolio(_frame(), DAY, _config())
    assert [item.instrument_id for item in portfolio.positions] == ["000001.SZ", "000002.SZ"]
    assert all(item.target_weight == pytest.approx(0.4) for item in portfolio.positions)
    assert portfolio.cash_weight == pytest.approx(0.2)


def test_daily_adapter_fails_closed_on_tie_policy_drift() -> None:
    with pytest.raises(DataValidationError, match="tie_policy"):
        fixed_count_config_from_daily(_config(tie_policy="keep_all_cutoff_ties"))


def test_daily_adapter_fails_closed_on_missing_or_retyped_fields() -> None:
    missing = _config()
    missing.pop("target_count")
    with pytest.raises(DataValidationError, match="missing fields"):
        fixed_count_config_from_daily(missing)

    with pytest.raises(DataValidationError, match="target_count"):
        fixed_count_config_from_daily(_config(target_count=True))
    with pytest.raises(DataValidationError, match="gross_exposure"):
        fixed_count_config_from_daily(_config(gross_exposure="1.0"))
    with pytest.raises(DataValidationError, match="max_weight_per_name"):
        fixed_count_config_from_daily(_config(max_weight_per_name="0.05"))


def test_daily_adapter_converts_core_validation_to_data_error() -> None:
    with pytest.raises(DataValidationError, match="invalid Daily fixed-count"):
        fixed_count_config_from_daily(_config(target_count=0))
    with pytest.raises(DataValidationError, match="invalid Daily fixed-count"):
        fixed_count_config_from_daily(_config(score_direction="sideways"))
