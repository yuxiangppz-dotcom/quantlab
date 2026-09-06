from datetime import date, datetime
from decimal import Decimal

import pytest

from quantlab.execution import (
    EXCHANGE_TIMEZONE,
    HandoffStatus,
    InstrumentIdentity,
    PITIdentityBook,
    PlanningPrice,
    PriceBasis,
    TargetHandoffConfig,
    TradingCalendar,
    build_rebalance_instruction,
    default_a_share_rule_book,
    fingerprint_target_portfolio,
)
from quantlab.portfolio import TargetPortfolio, TargetWeight


def _calendar() -> TradingCalendar:
    return TradingCalendar(
        sessions=(date(2022, 1, 7), date(2022, 1, 10)),
        coverage_start=date(2022, 1, 6),
        coverage_end=date(2022, 1, 10),
        source_id="calendar",
        source_sha256="a" * 64,
    )


def _identity(instrument_id: str = "600000.SH", board: str = "MAIN") -> PITIdentityBook:
    return PITIdentityBook((
        InstrumentIdentity(
            instrument_id=instrument_id,
            exchange="SSE",
            board=board,
            effective_from=date(2000, 1, 1),
            effective_to=None,
            source_record_id="security-master",
        ),
    ))


def _config(minimum_cash_fen: int = 0) -> TargetHandoffConfig:
    return TargetHandoffConfig(
        portfolio_id="strategy-v1",
        signal_as_of=datetime(2022, 1, 7, 15, 30, tzinfo=EXCHANGE_TIMEZONE),
        execution_date=date(2022, 1, 10),
        planning_nav_fen=1_000_000,
        minimum_cash_fen=minimum_cash_fen,
    )


def _price(
    value: str = "10.01",
    *,
    basis: PriceBasis = PriceBasis.RAW,
    available_at: datetime | None = None,
) -> PlanningPrice:
    return PlanningPrice(
        instrument_id="600000.SH",
        price=Decimal(value),
        price_date=date(2022, 1, 7),
        available_at=available_at
        or datetime(2022, 1, 7, 15, 1, tzinfo=EXCHANGE_TIMEZONE),
        basis=basis,
        source_id="canonical-raw-daily-close",
    )


def _target(weight: float = 0.5) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=date(2022, 1, 7),
        positions=(TargetWeight("600000.SH", weight),),
        cash_weight=1.0 - weight,
    )


def _build(target=None, prices=None, identities=None, config=None):
    return build_rebalance_instruction(
        target or _target(),
        config or _config(),
        {"600000.SH": _price()} if prices is None else prices,
        calendar=_calendar(),
        identities=identities or _identity(),
        rules=default_a_share_rule_book(),
    )


def test_handoff_uses_raw_price_only_for_deterministic_lot_rounding() -> None:
    result = _build()
    assert result.status is HandoffStatus.READY
    assert result.instruction is not None
    assert result.instruction.targets[0].target_shares == 400
    assert result.audit_rows[0].unrounded_shares == 499
    assert result.audit_rows[0].rounding_cash_fen == 99_600
    assert result.planned_position_notional_fen == 400_400
    assert result.planned_cash_fen == 599_600
    assert result.instruction.source_metadata.planning_price_basis is PriceBasis.RAW


def test_planning_input_fingerprint_binds_price_even_when_shares_match() -> None:
    first = _build(prices={"600000.SH": _price("10.01")})
    second = _build(prices={"600000.SH": _price("10.02")})
    assert first.instruction is not None and second.instruction is not None
    assert first.instruction.targets == second.instruction.targets
    assert (
        first.instruction.source_metadata.planning_input_fingerprint
        != second.instruction.source_metadata.planning_input_fingerprint
    )
    assert first.instruction.instruction_id != second.instruction.instruction_id


def test_missing_price_suppresses_entire_instruction_not_just_unknown_name() -> None:
    target = TargetPortfolio(
        as_of=date(2022, 1, 7),
        positions=(
            TargetWeight("600000.SH", 0.4),
            TargetWeight("600001.SH", 0.4),
        ),
        cash_weight=0.2,
    )
    identities = PITIdentityBook(tuple(
        InstrumentIdentity(
            instrument_id=value,
            exchange="SSE",
            board="MAIN",
            effective_from=date(2000, 1, 1),
            effective_to=None,
            source_record_id="security-master",
        )
        for value in ("600000.SH", "600001.SH")
    ))
    result = _build(target=target, prices={"600000.SH": _price()}, identities=identities)
    assert result.status is HandoffStatus.UNKNOWN
    assert result.instruction is None
    assert result.reason_codes == ("planning_price_missing",)
    assert {row.status for row in result.audit_rows} == {
        HandoffStatus.READY,
        HandoffStatus.UNKNOWN,
    }


@pytest.mark.parametrize("basis", [PriceBasis.ADJUSTED, PriceBasis.UNKNOWN])
def test_non_raw_price_never_produces_an_instruction(basis: PriceBasis) -> None:
    result = _build(prices={"600000.SH": _price(basis=basis)})
    assert result.instruction is None
    assert result.reason_codes == ("planning_price_not_raw",)


def test_price_available_after_signal_is_rejected_as_future_information() -> None:
    future = datetime(2022, 1, 10, 15, 1, tzinfo=EXCHANGE_TIMEZONE)
    result = _build(prices={"600000.SH": _price(available_at=future)})
    assert result.status is HandoffStatus.REJECTED
    assert result.instruction is None
    assert result.reason_codes == ("planning_price_after_signal_cutoff",)


def test_price_date_must_be_a_declared_session() -> None:
    price = PlanningPrice(
        instrument_id="600000.SH", price=Decimal("10.00"),
        price_date=date(2022, 1, 6),
        available_at=datetime(2022, 1, 6, 15, 1, tzinfo=EXCHANGE_TIMEZONE),
        basis=PriceBasis.RAW, source_id="raw",
    )
    result = _build(prices={"600000.SH": price})
    assert result.status is HandoffStatus.REJECTED
    assert result.instruction is None
    assert result.reason_codes == ("planning_price_date_not_session",)


def test_unresolved_rule_is_unknown_and_never_borrowed_from_another_date() -> None:
    identity = PITIdentityBook((
        InstrumentIdentity(
            instrument_id="600000.SH",
            exchange="SZSE",
            board="MAIN",
            effective_from=date(2000, 1, 1),
            effective_to=None,
            source_record_id="security-master",
        ),
    ))
    config = TargetHandoffConfig(
        portfolio_id="strategy-v1",
        signal_as_of=datetime(2024, 1, 4, 15, 30, tzinfo=EXCHANGE_TIMEZONE),
        execution_date=date(2024, 1, 5),
        planning_nav_fen=1_000_000,
    )
    calendar = TradingCalendar(
        sessions=(date(2024, 1, 4), date(2024, 1, 5)),
        coverage_start=date(2024, 1, 4),
        coverage_end=date(2024, 1, 5),
        source_id="calendar",
        source_sha256="a" * 64,
    )
    price = PlanningPrice(
        instrument_id="600000.SH", price=Decimal("10.00"),
        price_date=date(2024, 1, 4),
        available_at=datetime(2024, 1, 4, 15, 1, tzinfo=EXCHANGE_TIMEZONE),
        basis=PriceBasis.RAW, source_id="raw",
    )
    result = build_rebalance_instruction(
        TargetPortfolio(
            as_of=date(2024, 1, 4),
            positions=(TargetWeight("600000.SH", 0.5),), cash_weight=0.5,
        ),
        config,
        {"600000.SH": price},
        calendar=calendar,
        identities=identity,
        rules=default_a_share_rule_book(),
    )
    assert result.status is HandoffStatus.UNKNOWN
    assert result.instruction is None
    assert result.reason_codes == ("pit_rule_missing",)


def test_fractional_fen_price_and_minimum_cash_fail_closed() -> None:
    fractional = _build(prices={"600000.SH": _price("10.001")})
    assert fractional.instruction is None
    assert fractional.reason_codes == ("planning_price_requires_fractional_fen",)

    no_cash = _build(target=_target(1.0), config=_config(minimum_cash_fen=200_000))
    assert no_cash.instruction is None
    assert no_cash.reason_codes == ("minimum_cash_not_met_after_rounding",)


def test_all_cash_smoke_handoff_needs_no_price_or_identity() -> None:
    result = _build(
        target=TargetPortfolio(as_of=date(2022, 1, 7), positions=(), cash_weight=1.0),
        prices={},
        identities=PITIdentityBook(()),
    )
    assert result.status is HandoffStatus.READY
    assert result.instruction is not None
    assert result.instruction.targets == ()
    assert result.planned_cash_fen == 1_000_000


def test_target_fingerprint_is_position_order_invariant() -> None:
    first = TargetPortfolio(
        as_of=date(2022, 1, 7),
        positions=(TargetWeight("600001.SH", 0.2), TargetWeight("600000.SH", 0.3)),
        cash_weight=0.5,
    )
    second = TargetPortfolio(
        as_of=date(2022, 1, 7),
        positions=tuple(reversed(first.positions)),
        cash_weight=0.5,
    )
    assert fingerprint_target_portfolio(first) == fingerprint_target_portfolio(second)


def test_handoff_requires_exact_next_session() -> None:
    with pytest.raises(ValueError, match="next declared"):
        _build(config=TargetHandoffConfig(
            portfolio_id="strategy-v1",
            signal_as_of=datetime(2022, 1, 7, 15, 30, tzinfo=EXCHANGE_TIMEZONE),
            execution_date=date(2022, 1, 7),
            planning_nav_fen=1_000_000,
        ))
