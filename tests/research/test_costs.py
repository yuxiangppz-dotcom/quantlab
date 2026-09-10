import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from streamlit.testing.v1 import AppTest

from quantlab.research.costs import (
    DEFAULT_COST_PROFILE,
    estimate_research_order_components,
    load_research_cost_profile,
)


def _estimate(amounts, *, asset="stock", side="sell", day=date(2026, 9, 10), **kw):
    return estimate_research_order_components(
        amounts, asset_type=asset, side=side, trade_date=day, **kw
    )


@pytest.mark.parametrize("day,tax", [(date(2023, 8, 25), 1000), (date(2023, 8, 28), 500)])
def test_stock_stamp_is_separate_and_date_specific(day, tax):
    result = _estimate([1000000], day=day)
    assert result["commission_fen"] == 500
    assert result["stamp_duty_fen"] == tax
    assert result["known_components_subtotal_fen"] == 500 + tax
    assert result["complete_trading_cost_fen"] is None
    assert result["additional_fees_fen"] is None
    assert result["execution_authority"] is False and result["performance_claim"] is False
    assert _estimate([1000000], side="buy", day=day)["stamp_duty_fen"] == 0


@pytest.mark.parametrize("amount,expected", [(1000, 10), (500000, 43), (1000000, 86)])
def test_etf_minimum_and_no_secondary_market_stamp(amount, expected):
    for side in ("buy", "sell"):
        result = _estimate([amount], asset="etf", side=side)
        assert result["commission_fen"] == expected and result["stamp_duty_fen"] == 0


def test_single_order_aggregation_is_distinct_from_separate_orders():
    assert _estimate([250000, 250000])["commission_fen"] == 500
    assert sum(_estimate([250000])["commission_fen"] for _ in range(2)) == 1000
    assert _estimate([10000000])["commission_fen"] == 860


@pytest.mark.parametrize("amounts", [[], [0], [0, 0]])
def test_no_filled_amount_never_triggers_minimum_fee(amounts):
    result = _estimate(amounts)
    assert result["commission_fen"] == result["stamp_duty_fen"] == 0


def test_component_rounding_is_explicit_half_up_to_fen():
    assert _estimate([1000])["stamp_duty_fen"] == 1
    assert _estimate([1500], day=date(2023, 8, 25))["stamp_duty_fen"] == 2
    assert _estimate([10005814], side="buy")["commission_fen"] == 861


@pytest.mark.parametrize("amounts", [[-1], [True], [1.5], [Decimal("NaN")], "500"])
def test_invalid_amounts_are_rejected(amounts):
    with pytest.raises(ValueError, match="integer fen"):
        _estimate(amounts)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"asset": "bond"},
        {"side": "SELL"},
        {"day": date(2019, 12, 31)},
        {"day": date(2026, 9, 12)},
        {"day": datetime(2026, 9, 10)},
    ],
)
def test_unsupported_asset_side_or_rule_date_is_rejected(kwargs):
    with pytest.raises(ValueError):
        _estimate([500000], **kwargs)


@pytest.mark.parametrize(
    "key,value",
    [
        ("commission_rate", "NaN"),
        ("commission_rate", "not a number"),
        ("commission_rate", "-0.1"),
        ("commission_rate", 0.000086),
        ("stock_minimum_commission_fen", True),
        ("etf_minimum_commission_fen", 0),
        ("broker_verified", True),
        ("commission_includes_stamp_duty", True),
        ("execution_authority", 0),
        ("rules_reviewed_through", "2026-09-12"),
    ],
)
def test_profile_cannot_silently_upgrade_assumptions_or_bad_values(tmp_path, key, value):
    profile = json.loads(DEFAULT_COST_PROFILE.read_text())
    profile[key] = value
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError):
        _estimate([500000], profile_path=path)


def test_profile_fingerprint_changes_with_declared_commission(tmp_path):
    profile = json.loads(DEFAULT_COST_PROFILE.read_text())
    profile["commission_rate"] = "0.0001"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    assert (
        load_research_cost_profile(path)["profile_fingerprint"]
        != load_research_cost_profile()["profile_fingerprint"]
    )


def _screen():
    from quantlab.ui.research_controls import render_research_costs

    render_research_costs()


def test_research_fee_ui_shows_components_and_changes_historical_rule():
    app = AppTest.from_function(_screen).run()
    assert not app.exception
    frame = app.dataframe[0].value
    assert list(frame["佣金（元）"]) == ["5.00", "5.00", "0.43", "0.43"]
    assert list(frame["印花税（元）"]) == ["0.00", "2.50", "0.00", "0.00"]
    app.date_input[0].set_value(date(2023, 8, 25)).run()
    assert app.dataframe[0].value.iloc[1]["印花税（元）"] == "5.00"
    assert "不能把小计当作全部成本" in app.warning[0].value
