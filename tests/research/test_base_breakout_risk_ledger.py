from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.base_breakout_risk_ledger import (
    materialize_base_breakout_targets,
    run_base_breakout_risk_ledger,
)
from quantlab.research.quantity_kernel import (
    ResearchFeeScenario,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.quantity_scheduler import RawCloseMark
from quantlab.research.risk_ledger_loop import (
    LedgerSessionEvidence,
    RiskLedgerCheckpoint,
    RiskLedgerConfig,
)

START = date(2026, 1, 5)
CALENDAR = tuple(START + timedelta(days=index) for index in range(5))
RULES = ResearchQuantityRules(
    "synthetic", START, date(2027, 1, 1), 100, 100, 100, 100, 100000, True
)
FEES = ResearchFeeScenario(
    "synthetic",
    START,
    date(2027, 1, 1),
    Decimal("0"),
    0,
    Decimal("0"),
    Decimal("0"),
    Decimal("0"),
    0,
    Decimal("0"),
)
CONFIG = RiskLedgerConfig("C80", "s5b_synthetic", "s5b_nav", "synthetic_ledger", RULES)


def _signals() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "instrument_id": "000001.SZ",
                "trade_date": CALENDAR[1],
                "eligible": True,
                "selected": True,
                "target_weight": 0.5,
                "cash_weight": 0.5,
                "strategy_id": "price_volume_base_breakout",
                "research_only": True,
                "performance_claim": False,
                "broker_order_authority": False,
            },
            {
                "instrument_id": "000001.SZ",
                "trade_date": CALENDAR[2],
                "eligible": False,
                "selected": False,
                "target_weight": 0.0,
                "cash_weight": 1.0,
                "strategy_id": "price_volume_base_breakout",
                "research_only": True,
                "performance_claim": False,
                "broker_order_authority": False,
            },
        ]
    )


def _evidence() -> dict[date, LedgerSessionEvidence]:
    result = {}
    for index in range(1, 4):
        day = CALENDAR[index]
        context = ResearchSession(
            "000001.SZ",
            day,
            CALENDAR[index + 1],
            day,
            True,
            True,
            True,
            1000,
            1000,
            1000,
            900,
            1100,
            10**12,
            CALENDAR[index - 1],
            20,
            10**12,
            10**9,
            Decimal("0.05"),
            RULES,
            FEES,
        )
        result[day] = LedgerSessionEvidence(
            day, (context,), (RawCloseMark("000001.SZ", day, 1000),), True
        )
    return result


def test_targets_carry_between_signal_dates_without_creating_a_lock() -> None:
    targets = materialize_base_breakout_targets(_signals().iloc[[0]], CALENDAR[1:4])

    assert tuple(targets) == CALENDAR[1:4]
    assert all(target.positions[0].target_weight == 0.5 for target in targets.values())
    assert tuple(target.as_of for target in targets.values()) == CALENDAR[1:4]


def test_daily_s5b_exit_flows_through_t1_risk_ledger() -> None:
    checkpoint = RiskLedgerCheckpoint.start(
        signal_date=CALENDAR[0], initial_cash_fen=20_000_000, config=CONFIG
    )
    result = run_base_breakout_risk_ledger(
        signals=_signals(),
        checkpoint=checkpoint,
        calendar=CALENDAR,
        requested_end=CALENDAR[3],
        evidence=_evidence(),
        config=CONFIG,
    )

    assert result.status == "completed_scenario"
    assert result.ledger.records[0].next_intents[0].side == "buy"
    assert result.ledger.records[1].next_intents[0].side == "sell"
    assert result.ledger.records[2].book.lots == ()
    assert result.performance_eligible is False
    assert result.performance_claim is False
    assert result.broker_order_authority is False


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("strategy_id", "another_strategy", "strategy_id"),
        ("performance_claim", True, "authority boundary"),
        ("cash_weight", 0.4, "sum to one"),
    ],
)
def test_forged_identity_authority_or_budget_fails_closed(column, value, message) -> None:
    signals = _signals().iloc[[0]].copy()
    signals.loc[signals.index[0], column] = value
    with pytest.raises(DataValidationError, match=message):
        materialize_base_breakout_targets(signals, CALENDAR[1:2])


def test_missing_initial_target_fails_instead_of_assuming_cash() -> None:
    with pytest.raises(DataValidationError, match="first decision session"):
        materialize_base_breakout_targets(_signals().iloc[[1]], CALENDAR[1:3])
