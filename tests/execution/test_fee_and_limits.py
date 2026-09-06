"""Fee-budget and limit-order fill correctness (v0.2.1 Part D).

Fee-cap semantics, frozen: the cap is the CUMULATIVE fee ceiling over the
WHOLE lifetime of one order. The reservation independently tracks the
remaining worst-case limit notional of unfilled shares and the remaining
fee capacity; every partial fill must leave the reservation exactly equal
to that sum (price improvement releases the excess), cumulative fees can
never exceed the cap, and BUY fills above the limit (SELL below it) are
rejected before any ledger mutation.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantlab.execution.ledger import (
    ExecutionLedger,
    FillRecorded,
    LedgerAccountingError,
    OrderIntended,
    OrderSubmitted,
)
from quantlab.execution.models import (
    AccountSnapshot,
    OrderIntent,
    OrderRequest,
    OrderType,
    PositionLot,
    PriceBasis,
    Side,
    TimeInForce,
)
from quantlab.execution.planning import FeeCapQuote

TZ = ZoneInfo("Asia/Shanghai")
THU = date(2024, 11, 28)
FRI = date(2024, 11, 29)
MON = date(2024, 12, 2)
LIMIT = Decimal("10.00")
CAP = 1_000


def _instant(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(9, 30), TZ) + timedelta(minutes=minute)


def _calendar():
    from quantlab.execution.rules import TradingCalendar

    return TradingCalendar(
        sessions=(THU, FRI, MON),
        coverage_start=THU,
        coverage_end=MON,
        source_id="fee-calendar",
        source_sha256="a" * 64,
    )


def _account(cash_fen: int = 10_000_000, lots: tuple[PositionLot, ...] = ()):
    return AccountSnapshot(
        account_id="fee-account",
        as_of=_instant(FRI, 0),
        cash_fen=cash_fen,
        lots=lots,
    )


def _intent(order_id: str, side: Side, quantity: int) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        instruction_id="fee-instruction",
        instrument_id="600000.SH",
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=LIMIT,
        intended_trade_date=FRI,
        created_at=_instant(FRI, 1),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="fee-price",
        time_in_force=TimeInForce.DAY,
    )


def _submit_event(
    order_id: str,
    side: Side,
    quantity: int,
    *,
    minute: int = 5,
    fingerprint: str | None = None,
) -> OrderSubmitted:
    intent = _intent(order_id, side, quantity)
    request = OrderRequest(
        request_id=f"fee-request-{order_id}",
        order_id=order_id,
        instrument_id=intent.instrument_id,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=LIMIT,
        intended_trade_date=FRI,
        created_at=_instant(FRI, minute),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="fee-price",
        time_in_force=TimeInForce.DAY,
    )
    return OrderSubmitted(
        f"fee-{order_id}-submitted",
        _instant(FRI, minute),
        request,
        worst_case_fee_fen=CAP if side is Side.BUY else 0,
        fee_quote_fingerprint=fingerprint,
    )


def _prepare(ledger: ExecutionLedger, order_id: str, side: Side, quantity: int):
    intent = _intent(order_id, side, quantity)
    ledger.append(
        OrderIntended(f"fee-{order_id}-intended", _instant(FRI, 1), intent)
    )
    from quantlab.execution.ledger import ConstraintsAssessed
    from quantlab.execution.models import (
        ConstraintDecision,
        ConstraintDimension,
        ConstraintStatus,
    )

    def decision(dimension: ConstraintDimension) -> ConstraintDecision:
        status = ConstraintStatus.ALLOWED
        if dimension is ConstraintDimension.FILLABILITY:
            status = ConstraintStatus.UNKNOWN
        return ConstraintDecision(
            decision_id=f"fee-{order_id}-{dimension.value}",
            dimension=dimension,
            status=status,
            reason_code="fee-test-allowed",
            message="fee test",
            assessed_at=_instant(FRI, 3),
        )

    dimensions = tuple(decision(item) for item in ConstraintDimension)
    ledger.append(
        ConstraintsAssessed(
            f"fee-{order_id}-assessed",
            _instant(FRI, 3),
            order_id,
            dimensions,
            execution_state_fingerprint=ledger.execution_state_fingerprint(),
        )
    )


def _quote() -> FeeCapQuote:
    return FeeCapQuote(
        instrument_id="600000.SH",
        account_id="fee-account",
        trade_date=FRI,
        cap_fen=CAP,
        evidence_id="fee-quote-1",
        source_fingerprint="c" * 64,
        synthetic=True,
    )


def _state(ledger: ExecutionLedger) -> tuple:
    return (
        ledger.cash_fen,
        ledger.lots,
        tuple(ledger.orders),
        ledger.reservations,
        tuple(ledger.events),
        set(ledger._fill_ids),  # noqa: SLF001
        set(ledger._request_ids),  # noqa: SLF001
    )


def _fill(
    order_id: str,
    fill_id: str,
    quantity: int,
    price: str,
    fee: int,
    *,
    minute: int,
    side: Side = Side.BUY,
) -> FillRecorded:
    from quantlab.execution.models import exchange_date

    del side, exchange_date
    decimal_price = Decimal(price)
    return FillRecorded(
        event_id=f"fee-{fill_id}",
        fill_id=fill_id,
        occurred_at=_instant(FRI, minute),
        order_id=order_id,
        trade_date=FRI,
        quantity=quantity,
        price=decimal_price,
        gross_notional_fen=int(decimal_price * quantity * 100),
        fee_fen=fee,
        buy_lot_sellable_from=MON,
    )


def test_buy_fill_above_limit_rejected_without_mutation() -> None:
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    _prepare(ledger, "fee-buy", Side.BUY, 100)
    before = _state(ledger)
    ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), _quote())])
    reserved = _state(ledger)
    with pytest.raises(LedgerAccountingError, match="limit"):
        ledger.append(
            _fill("fee-buy", "fee-bad", 40, "10.04", 100, minute=30)
        )
    assert _state(ledger) == reserved != before


def test_sell_fill_below_limit_rejected_without_mutation() -> None:
    lot = PositionLot(
        lot_id="fee-lot",
        instrument_id="600000.SH",
        quantity=200,
        acquired_trade_date=THU,
        sellable_from=FRI,
    )
    ledger = ExecutionLedger(_account(lots=(lot,)), calendar=_calendar())
    _prepare(ledger, "fee-sell", Side.SELL, 200)
    ledger.submit_orders([(_submit_event("fee-sell", Side.SELL, 200), _quote())])
    before = _state(ledger)
    with pytest.raises(LedgerAccountingError, match="limit"):
        ledger.append(
            _fill("fee-sell", "fee-sell-bad", 200, "9.96", 100, minute=30)
        )
    assert _state(ledger) == before


def test_multi_partial_fill_fee_budget_and_reconciliation() -> None:
    """40@10.00 fee 500, then 60@10.00 fee 501 must exceed the cap."""
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    _prepare(ledger, "fee-buy", Side.BUY, 100)
    ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), _quote())])
    # initial reservation: 100 * 10.00 * 100 fen + 1000 fen fee cap
    assert ledger.reserved_cash_fen() == 101_000
    ledger.append(_fill("fee-buy", "fee-f1", 40, "10.00", 500, minute=30))
    # after fill 1: 60 unfilled shares worst-case 60_000 + 500 remaining
    # fee capacity == reservation (exact identity, not a leftover lump)
    assert ledger.reserved_cash_fen() == 60_500
    assert ledger.cash_fen == 10_000_000 - 40_500
    with pytest.raises(LedgerAccountingError, match="fee cap"):
        ledger.append(_fill("fee-buy", "fee-f2", 60, "10.00", 501, minute=31))
    assert ledger.reserved_cash_fen() == 60_500
    ledger.append(_fill("fee-buy", "fee-f3", 60, "10.00", 500, minute=32))
    assert ledger.order("fee-buy").status.value == "filled"
    assert ledger.order("fee-buy").fee_fen == 1_000
    # a fully filled order keeps no reservation
    assert ledger.reserved_cash_fen() == 0
    assert ledger.cash_fen == 10_000_000 - 100_000 - 1_000


def test_price_improvement_releases_excess_reservation() -> None:
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    _prepare(ledger, "fee-buy", Side.BUY, 100)
    ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), _quote())])
    ledger.append(_fill("fee-buy", "fee-f1", 40, "9.90", 500, minute=30))
    # 60 unfilled * 10.00 + 500 remaining fee capacity (exact release)
    assert ledger.reserved_cash_fen() == 60_500
    assert ledger.cash_fen == 10_000_000 - 39_600 - 500


def test_cancel_after_partial_fill_releases_remaining_reservation() -> None:
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    _prepare(ledger, "fee-buy", Side.BUY, 100)
    ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), _quote())])
    ledger.append(_fill("fee-buy", "fee-f1", 40, "10.00", 500, minute=30))
    from quantlab.execution.ledger import OrderCanceled

    ledger.append(
        OrderCanceled(
            "fee-cancel", _instant(FRI, 40), "fee-buy", "fee test cancel"
        )
    )
    assert ledger.reserved_cash_fen() == 0
    assert ledger.order("fee-buy").fee_fen == 500


def test_submission_requires_matching_typed_fee_quote() -> None:
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    _prepare(ledger, "fee-buy", Side.BUY, 100)
    mismatched = FeeCapQuote(
        instrument_id="600001.SH",
        account_id="fee-account",
        trade_date=FRI,
        cap_fen=CAP,
        evidence_id="fee-quote-2",
        source_fingerprint="d" * 64,
        synthetic=True,
    )
    with pytest.raises(Exception, match="fee quote"):
        ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), mismatched)])
    wrong_account = FeeCapQuote(
        instrument_id="600000.SH",
        account_id="other-account",
        trade_date=FRI,
        cap_fen=CAP,
        evidence_id="fee-quote-3",
        source_fingerprint="e" * 64,
        synthetic=True,
    )
    with pytest.raises(Exception, match="fee quote"):
        ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), wrong_account)])
    wrong_date = FeeCapQuote(
        instrument_id="600000.SH",
        account_id="fee-account",
        trade_date=MON,
        cap_fen=CAP,
        evidence_id="fee-quote-4",
        source_fingerprint="0" * 64,
        synthetic=True,
    )
    with pytest.raises(Exception, match="fee quote"):
        ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), wrong_date)])
    # the bound quote records its fingerprint on the submission event
    ledger.submit_orders([(_submit_event("fee-buy", Side.BUY, 100), _quote())])
    event = ledger.events[-1]
    assert event.fee_quote_fingerprint == "c" * 64
    assert event.worst_case_fee_fen == CAP
