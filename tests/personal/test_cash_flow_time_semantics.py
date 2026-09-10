from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quantlab.personal.cash_flow import (
    CashFlowDirection,
    CashFlowTimingQuality,
    ExternalCashFlow,
)


def _flow(**overrides) -> ExternalCashFlow:
    effective = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    values = {
        "event_id": "cash:mine:D1",
        "flow_id": "D1",
        "occurred_at": effective,
        "reported_at": effective + timedelta(minutes=5),
        "account_id": "mine",
        "direction": CashFlowDirection.DEPOSIT,
        "amount_fen": 10_000,
        "source_sha256": "a" * 64,
        "source_row_sha256": "b" * 64,
        "timing_quality": CashFlowTimingQuality.EXACT_EFFECTIVE_TIME,
    }
    values.update(overrides)
    return ExternalCashFlow(**values)


def test_exact_cash_flow_separates_effective_and_reported_time() -> None:
    flow = _flow()
    assert flow.effective_at == flow.occurred_at
    assert flow.reported_at > flow.effective_at
    assert flow.is_performance_timing_eligible is True
    assert flow.signed_amount_fen == 10_000


def test_report_before_effective_is_rejected() -> None:
    effective = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="reported_at cannot precede"):
        _flow(occurred_at=effective, reported_at=effective - timedelta(seconds=1))


def test_exact_timing_requires_reported_at_provenance() -> None:
    with pytest.raises(ValueError, match="requires reported_at"):
        _flow(reported_at=None)


def test_legacy_single_timestamp_remains_replayable_but_not_performance_grade() -> None:
    effective = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    flow = ExternalCashFlow(
        event_id="cash:mine:legacy",
        flow_id="legacy",
        occurred_at=effective,
        account_id="mine",
        direction=CashFlowDirection.WITHDRAWAL,
        amount_fen=5_000,
        source_sha256="c" * 64,
        source_row_sha256="d" * 64,
    )
    assert flow.reported_at is None
    assert flow.effective_at == effective
    assert flow.timing_quality is CashFlowTimingQuality.LEGACY_REPORTED_AS_EFFECTIVE_UNVERIFIED
    assert flow.is_performance_timing_eligible is False
    assert flow.signed_amount_fen == -5_000


def test_legacy_quality_cannot_claim_distinct_report_time() -> None:
    effective = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="legacy cash-flow timing"):
        ExternalCashFlow(
            event_id="cash:mine:legacy",
            flow_id="legacy",
            occurred_at=effective,
            reported_at=effective + timedelta(minutes=1),
            account_id="mine",
            direction=CashFlowDirection.DEPOSIT,
            amount_fen=1,
            source_sha256="e" * 64,
            source_row_sha256="f" * 64,
        )
