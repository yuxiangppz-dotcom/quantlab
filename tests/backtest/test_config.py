import pytest

from quantlab.backtest import BacktestConfig


def test_config_valid_defaults() -> None:
    cfg = BacktestConfig()
    assert cfg.initial_nav == 1.0
    assert cfg.transaction_cost_bps == 0.0
    assert cfg.annualization == 252
    assert cfg.cost_rate == 0.0


def test_config_cost_rate() -> None:
    cfg = BacktestConfig(transaction_cost_bps=10.0)
    assert cfg.cost_rate == pytest.approx(0.001)


def test_config_rejects_nan_initial_nav() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(initial_nav=float("nan"))


def test_config_rejects_inf_initial_nav() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(initial_nav=float("inf"))


def test_config_rejects_nonpositive_initial_nav() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(initial_nav=0.0)
    with pytest.raises(ValueError):
        BacktestConfig(initial_nav=-1.0)


def test_config_rejects_negative_cost() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(transaction_cost_bps=-1.0)


def test_config_rejects_nan_cost() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(transaction_cost_bps=float("nan"))


def test_config_rejects_inf_cost() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(transaction_cost_bps=float("inf"))


def test_config_rejects_non_positive_annualization() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(annualization=0)
    with pytest.raises(ValueError):
        BacktestConfig(annualization=-5)


def test_config_rejects_non_integer_annualization() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(annualization=252.0)


def test_config_rejects_bool_annualization() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(annualization=True)
