import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.s4_entry_plan import (
    CAPITAL,
    DECISION,
    EXECUTION,
    FOLLOWING,
    SLOT,
    code_scope,
    fee_context,
    load_inputs,
    nominal_quantity,
    project,
    quantity_rule,
    rank_proposals,
    required_unknowns,
)


def rule(star=False):
    return SimpleNamespace(
        rule_id="synthetic",
        effective_from=date(2022, 1, 1),
        effective_to=date(2022, 12, 31),
        buy_min_quantity=200 if star else 100,
        buy_quantity_step=1 if star else 100,
        sell_min_quantity=200 if star else 100,
        sell_quantity_step=1 if star else 100,
        max_limit_quantity=100_000 if star else 1_000_000,
        allow_full_odd_lot_exit=True,
    )


class Catalogue:
    def resolve(self, exchange, board, day, *, session):
        assert day == date(2022, 1, 4)
        assert session.value == "closing_auction"
        return rule(board == "STAR")


@pytest.fixture
def inputs():
    rows, bars, liquidity = [], {}, {}
    for i in range(40):
        code = f"600{i:03}.SH"
        rows.append(
            dict(
                instrument_id=code,
                copied_S4_A=i / 100,
                signal_date=DECISION,
                st_source_records=[],
                suspensions_source_records=[],
                session=dict(
                    instrument_id=code,
                    execution_date=EXECUTION,
                    next_session=FOLLOWING,
                    evidence_date=EXECUTION,
                    calendar_verified=True,
                    market_open=None,
                    corporate_actions_processed=None,
                    raw_close_fen=1100,
                    low_fen=950,
                    high_fen=1100,
                    down_limit_fen=900,
                    up_limit_fen=1100,
                    prior20_amount_fen=None,
                    prior20_asof=DECISION,
                    prior20_sessions=19,
                    session_amount_fen=100_000_000,
                    session_volume_shares=100_000,
                    participation=None,
                    rules=None,
                    fees=None,
                ),
            )
        )
        bars[code] = dict(
            open=10, high=10, low=10, close=10, adj_factor=20, amount=1_000_000, volume=100_000
        )
        liquidity[code] = dict(
            copied_exact_adv20_fen=None, adv20_floor_fen=100_000_000, unknown_dates=[]
        )
    return (
        rows,
        bars,
        liquidity,
        Catalogue(),
        {"commission_rate": "0.000086", "stock_minimum_commission_fen": 500},
    )


def test_selected_quantities_use_only_decision_raw_close(inputs):
    rows, bars, liquidity, cat, fees = inputs
    original = deepcopy((rows, bars, liquidity))
    out = project(*inputs)
    assert (rows, bars, liquidity) == original
    assert len(out["rows"]) == 40
    selected = [r for r in out["rows"] if r["selected_raw_proposal"]]
    assert len(selected) == 20
    assert all(r["nominal_quantity"] == 800 for r in selected)
    assert sum(r["nominal_notional_fen"] for r in selected) == 16_000_000
    assert out["target_gross_fen"] + out["cash_outside_nominal_slots_fen"] == CAPITAL
    for r in rows:
        r["session"]["raw_close_fen"] = 9999
        r["st_source_records"] = [{"status": "synthetic"}]
        r["session"]["session_volume_shares"] = 0
    changed = project(*inputs)
    assert out["selected_in_priority_order"] == changed["selected_in_priority_order"]
    assert [r["nominal_quantity"] for r in out["rows"]] == [
        r["nominal_quantity"] for r in changed["rows"]
    ]


def test_tie_order_minimum_and_unknowns(inputs):
    rows = inputs[0]
    for r in rows:
        r["copied_S4_A"] = 0.5
    assert rank_proposals(rows)[0] == tuple(r["instrument_id"] for r in rows[:20])
    for r in rows[:10]:
        r["copied_S4_A"] = None
    assert len(rank_proposals(rows)[0]) == 20
    rows[10]["copied_S4_A"] = None
    assert rank_proposals(rows) == ((), 29)
    out = project(*inputs)
    assert out["target_gross_fen"] == 0 and out["net_return"] is None
    assert len(out["rows"]) == 40


@pytest.mark.parametrize("value", [True, False, "0.1", float("nan"), float("inf"), Decimal("1")])
def test_bad_saved_score_rejected(inputs, value):
    inputs[0][0]["copied_S4_A"] = value
    with pytest.raises(DataValidationError):
        project(*inputs)


@pytest.mark.parametrize(
    "price,star,expected",
    [
        (1000, False, 800),
        (1100, False, 700),
        (4000, True, 200),
        (4001, True, 0),
        (3900, True, 205),
        (8001, False, 0),
        (8000, False, 100),
        (1, False, 800000),
    ],
)
def test_nominal_grids(price, star, expected):
    q = nominal_quantity(price, quantity_rule(rule(star)))
    assert q == expected and q * price <= SLOT


def test_single_order_max_and_missing():
    r = rule()
    r.max_limit_quantity = 500
    assert nominal_quantity(1, quantity_rule(r)) == 500
    assert nominal_quantity(None, quantity_rule(r)) is None
    assert nominal_quantity(100, None) is None


@pytest.mark.parametrize("price", [True, 0, -1, 1.2, "100", 10**16])
def test_price_type(price):
    with pytest.raises(DataValidationError):
        nominal_quantity(price, quantity_rule(rule()))


def test_unknown_price_never_backfills_rank21(inputs):
    inputs[1].pop("600039.SH")
    out = project(*inputs)
    assert len(out["selected_in_priority_order"]) == 20
    assert "600019.SH" not in out["selected_in_priority_order"]
    item = out["rows"][-1]
    assert item["selected_raw_proposal"] and item["nominal_quantity"] is None
    assert "decision_raw_price_unknown" in item["required_unknowns"]["baseline"]


def test_all_unknowns_and_two_fee_scenarios_retained(inputs):
    out = project(*inputs)
    assert out["economic_paths_started"] == out["completed_trading_days"] == 0
    assert out["net_return"] is out["drawdown"] is None
    expected = {
        "historical_identity_and_signal_eligibility_unverified",
        "market_open_unknown",
        "corporate_actions_processed_unknown",
        "additional_fee_rate_unknown",
        "additional_fee_fixed_fen_unknown",
    }
    assert set(out["necessary_unknown_counts"]) == expected
    assert set(out["necessary_unknown_counts"].values()) == {20}
    row = out["rows"][-1]
    for name, slip in [("baseline", "0.0005"), ("stress", "0.0015")]:
        context = row["contexts"][name]
        assert context["fees"]["adverse_slippage_rate"] == Decimal(slip)
        assert context["fees"]["additional_fee_rate"] is None
        assert context["fees"]["additional_fee_fixed_fen"] is None
        assert context["participation"] == Decimal("0.01")
        assert context["prior20_amount_fen"] == 100_000_000
        assert context["market_open"] is context["corporate_actions_processed"] is None


def test_unaffordable_is_not_unknown_or_replaced(inputs):
    inputs[1]["600039.SH"].update(open=90, high=90, low=90, close=90)
    out = project(*inputs)
    assert out["rows"][-1]["sizing_status"] == "below_minimum"
    assert out["rows"][-1]["required_unknowns"]["baseline"] == []
    assert len(out["selected_in_priority_order"]) == 20


@pytest.mark.parametrize(
    "key,value",
    [
        ("execution_date", "2022-01-05"),
        ("evidence_date", "2022-01-05"),
        ("prior20_asof", "2022-01-04"),
        ("next_session", "2022-01-06"),
        ("instrument_id", "wrong"),
        ("participation", "0.01"),
    ],
)
def test_changed_source_scope_rejected(inputs, key, value):
    inputs[0][0]["session"][key] = value
    with pytest.raises(DataValidationError):
        project(*inputs)


def test_liquidity_source_mismatch(inputs):
    inputs[2]["600000.SH"]["copied_exact_adv20_fen"] = 1
    with pytest.raises(DataValidationError):
        project(*inputs)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("688001.SH", ("SSE", "STAR")),
        ("300001.SZ", ("SZSE", "CHINEXT")),
        ("600000.SH", ("SSE", "MAIN")),
        ("000001.SZ", ("SZSE", "MAIN")),
        ("900001.SH", None),
        ("200001.SZ", None),
        ("800001.BJ", None),
        ("600001oops.SH", None),
    ],
)
def test_prefix_scope_is_narrow(code, expected):
    assert code_scope(code) == expected


def test_unsupported_fee_name_and_all_missing_fields(inputs):
    with pytest.raises(DataValidationError):
        fee_context(inputs[-1], "new")
    row = project(*inputs)["rows"][-1]
    context = row["contexts"]["baseline"]
    context["raw_close_fen"] = context["session_amount_fen"] = None
    context["prior20_sessions"] = 19
    problems = required_unknowns(context, 800, 1000)
    assert {
        "raw_close_fen_unknown",
        "session_amount_fen_unknown",
        "prior20_coverage_incomplete",
    } <= set(problems)


@pytest.mark.parametrize("change", ["resource", "extra_file", "path", "pin", "digest"])
def test_contract_change_fails_before_any_actual_data_read(tmp_path, change):
    name = Path("config/s4_first_entry_plan_v1.json")
    config = json.loads(name.read_text())
    if change == "resource":
        config["resources"]["max_wakeup_seconds"] += 1
    elif change == "extra_file":
        config["inputs"]["unexpected"] = {"sha256": "0" * 64, "bytes": 0}
    elif change == "path":
        config["decision_price_path"] = "future.parquet"
    elif change == "pin":
        config["pins"]["selection"] = "0" * 64
    else:
        next(iter(config["inputs"].values()))["sha256"] = "0" * 64
    (tmp_path / "config").mkdir()
    (tmp_path / name).write_text(json.dumps(config))
    with pytest.raises(DataValidationError, match="scope changed"):
        load_inputs(tmp_path)


def test_missing_rule_is_unknown_without_replacement(inputs):
    class UnknownCatalogue:
        def resolve(self, *args, **kwargs):
            return None

    out = project(*inputs[:3], UnknownCatalogue(), inputs[-1])
    assert len(out["selected_in_priority_order"]) == 20
    selected = [r for r in out["rows"] if r["selected_raw_proposal"]]
    assert all(r["nominal_quantity"] is None for r in selected)
    assert out["necessary_unknown_counts"]["rules_unknown"] == 20


def test_duplicate_or_reordered_original_population_rejected(inputs):
    inputs[0].reverse()
    with pytest.raises(DataValidationError, match="ordered identifiers"):
        rank_proposals(inputs[0])
    inputs[0].sort(key=lambda r: r["instrument_id"])
    inputs[0].append(deepcopy(inputs[0][-1]))
    with pytest.raises(DataValidationError, match="ordered identifiers"):
        rank_proposals(inputs[0])
