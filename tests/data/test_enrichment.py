from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from quantlab.data.enrichment import (
    dividend_context_warnings,
    inspect_enrichment_status,
    sync_daily_price_limits,
    sync_financial_indicator_observation,
)
from quantlab.data.models import (
    DailyBar,
    DailyPriceLimit,
    DataValidationError,
    DividendObservation,
    FinancialIndicatorObservation,
    Security,
)
from quantlab.data.storage import ParquetStorage

DAY = date(2026, 9, 9)


def _security() -> Security:
    return Security(
        "000001.SZ",
        "000001",
        "平安银行",
        "SZSE",
        "SZ",
        "主板",
        "L",
        date(1991, 4, 3),
        None,
    )


def _limit(up: float = 11.0) -> DailyPriceLimit:
    return DailyPriceLimit(
        "000001.SZ", DAY, 10.0, up, 9.0, "SZSE", "tushare.stk_limit", f"limit-{up}"
    )


class _Provider:
    def get_daily_price_limits_by_date(self, trade_date: date):
        assert trade_date == DAY
        return [_limit()]

    def get_financial_indicators_by_period(self, period_end: date):
        observed = datetime(2026, 9, 10, tzinfo=UTC)
        return [
            FinancialIndicatorObservation(
                "000001.SZ",
                date(2025, 3, 14),
                period_end,
                "1",
                9.0,
                0.8,
                None,
                31.0,
                4.0,
                5.0,
                20.0,
                91.0,
                observed,
                date(2026, 9, 10),
                "prospective_from_first_local_observation",
                "tushare.fina_indicator_vip",
                "financial-1",
            )
        ]


def _storage(tmp_path) -> ParquetStorage:
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_securities([_security()])
    storage.save_daily_bars_by_date(
        [DailyBar("000001.SZ", DAY, 10, 10, 10, 10, 10, 100, 1000)], DAY
    )
    return storage


def test_price_limit_sync_requires_daily_coverage_and_is_immutable(tmp_path) -> None:
    storage = _storage(tmp_path)
    first = sync_daily_price_limits(_Provider(), storage, DAY)
    second = sync_daily_price_limits(_Provider(), storage, DAY)
    assert first.status == "synced"
    assert second.status == "reused"
    assert storage.load_daily_price_limits_by_date(DAY) == [_limit()]
    with pytest.raises(DataValidationError, match="immutable stored fact"):
        storage.save_daily_price_limits_by_date([_limit(12.0)], DAY)


def test_price_limit_sync_fails_when_a_daily_bar_has_no_official_limit(tmp_path) -> None:
    storage = _storage(tmp_path)

    class Empty(_Provider):
        def get_daily_price_limits_by_date(self, trade_date: date):
            return []

    with pytest.raises(DataValidationError, match="no canonical A-share rows"):
        sync_daily_price_limits(Empty(), storage, DAY)


def test_price_limit_sync_excludes_beijing_outside_daily_v1_scope(tmp_path) -> None:
    storage = _storage(tmp_path)
    beijing = Security(
        "920268.BJ",
        "920268",
        "北交所样本",
        "BSE",
        "BJ",
        "北交所",
        "L",
        date(2025, 1, 1),
        None,
    )
    storage.upsert_securities([beijing])

    class WithBeijing(_Provider):
        def get_daily_price_limits_by_date(self, trade_date: date):
            return [
                _limit(),
                DailyPriceLimit(
                    "920268.BJ",
                    DAY,
                    20.0,
                    26.0,
                    0.0,
                    "BSE",
                    "tushare.stk_limit",
                    "beijing-limit",
                ),
            ]

    result = sync_daily_price_limits(WithBeijing(), storage, DAY)
    assert result.rows == 1


def test_financial_snapshot_is_prospective_and_idempotent(tmp_path) -> None:
    storage = _storage(tmp_path)
    period = date(2024, 12, 31)
    first = sync_financial_indicator_observation(_Provider(), storage, period)
    second = sync_financial_indicator_observation(_Provider(), storage, period)
    assert first.status == "observed_prospective_only"
    assert first.path == second.path
    loaded = storage.load_financial_indicator_observations()
    assert len(loaded) == 1
    assert loaded[0].available_from == date(2026, 9, 10)
    before = inspect_enrichment_status(storage, date(2026, 9, 9))
    after = inspect_enrichment_status(storage, date(2026, 9, 10))
    assert before["fina_indicator_vip"]["eligible_versions_as_of"] == 0
    assert after["fina_indicator_vip"]["eligible_versions_as_of"] == 1


def test_dividend_warning_is_observation_bounded_and_never_posts_account_state(tmp_path) -> None:
    storage = _storage(tmp_path)
    storage.save_dividend_observations(
        [
            DividendObservation(
                instrument_id="000001.SZ",
                period_end=date(2025, 12, 31),
                announcement_date=date(2026, 8, 1),
                process_status="实施",
                stock_dividend_per_share=None,
                stock_bonus_rate=None,
                stock_conversion_rate=None,
                cash_dividend_after_tax=0.1,
                cash_dividend_before_tax=0.1,
                record_date=date(2026, 9, 15),
                ex_date=date(2026, 9, 16),
                pay_date=date(2026, 9, 16),
                share_listing_date=None,
                implementation_announcement_date=date(2026, 9, 10),
                observed_at=datetime(2026, 9, 10, tzinfo=UTC),
                available_from=date(2026, 9, 10),
                source="tushare.dividend",
                source_record_id="dividend-1",
            )
        ]
    )
    assert dividend_context_warnings(storage, {"000001.SZ"}, as_of=date(2026, 9, 9)) == []
    warnings = dividend_context_warnings(storage, {"000001.SZ"}, as_of=date(2026, 9, 10))
    assert warnings[0]["ex_date"] == "2026-09-16"
    assert warnings[0]["warning"] == "CORPORATE_ACTION_CONTEXT_REQUIRES_ACCOUNT_RECONCILIATION"
    assert (
        inspect_enrichment_status(storage, date(2026, 9, 10))["dividend"]["account_postings"]
        is False
    )
