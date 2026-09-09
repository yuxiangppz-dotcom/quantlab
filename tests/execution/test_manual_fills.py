from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantlab.execution import (
    AccountSnapshot,
    ExecutionLedger,
    LedgerAccountingError,
    LedgerTransitionError,
    ManualFillImported,
    PositionLot,
    Side,
    TradingCalendar,
)

TZ = ZoneInfo("Asia/Shanghai")
MON = date(2026, 9, 7)
TUE = date(2026, 9, 8)


def _event(side: Side, fill_id: str, *, quantity: int = 100) -> ManualFillImported:
    return ManualFillImported(
        event_id=f"manual:{fill_id}",
        fill_id=fill_id,
        occurred_at=datetime(2026, 9, 7, 16, tzinfo=TZ),
        account_id="mine",
        instrument_id="000001.SZ",
        side=side,
        trade_date=MON,
        quantity=quantity,
        price=Decimal("10.00"),
        gross_notional_fen=quantity * 1000,
        fee_fen=500,
        buy_lot_sellable_from=TUE if side is Side.BUY else None,
        source_sha256="a" * 64,
        source_row_sha256="b" * 64,
    )


def _calendar() -> TradingCalendar:
    return TradingCalendar((MON, TUE), MON, TUE, "manual-test", "c" * 64)


def test_manual_buy_uses_same_cash_lot_and_t1_ledger_invariants() -> None:
    initial = AccountSnapshot("mine", datetime(2026, 9, 7, 9, tzinfo=TZ), 200_000)
    ledger = ExecutionLedger(initial, calendar=_calendar())
    event = _event(Side.BUY, "broker-1")
    with pytest.raises(LedgerTransitionError, match="manual-import entry point"):
        ledger.append(event)
    ledger.append_manual_imports([event])
    assert ledger.events == (event,)
    ledger.append_manual_imports([event])
    assert ledger.cash_fen == 99_500
    assert ledger.position_quantity("000001.SZ") == 100
    assert ledger.sellable_quantity("000001.SZ", MON) == 0
    assert ledger.sellable_quantity("000001.SZ", TUE) == 100


def test_manual_fill_batch_failure_rolls_back_and_sell_cannot_oversell() -> None:
    initial = AccountSnapshot(
        "mine",
        datetime(2026, 9, 7, 9, tzinfo=TZ),
        0,
        (PositionLot("opening", "000001.SZ", 100, date(2020, 1, 1), MON),),
    )
    ledger = ExecutionLedger(initial, calendar=_calendar())
    before = ledger.snapshot(datetime(2026, 9, 7, 9, tzinfo=TZ))
    with pytest.raises(LedgerAccountingError, match="oversell"):
        ledger.append_manual_imports([_event(Side.SELL, "broker-sell", quantity=200)])
    assert ledger.snapshot(datetime(2026, 9, 7, 16, tzinfo=TZ)).cash_fen == before.cash_fen
    assert ledger.position_quantity("000001.SZ") == 100
