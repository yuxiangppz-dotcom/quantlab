from datetime import date

import pytest

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DataValidationError,
    TradingCalendar,
)
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage
from quantlab.data.sync import (
    audit_daily_adj_coverage,
    filter_unlisted_placeholders,
    sync_adj_factor_history,
    sync_daily_basic_history,
    sync_daily_history,
    validate_adj_factors,
    validate_daily_bars,
    validate_daily_basic,
)


def _bar(**overrides) -> DailyBar:
    values = dict(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        pre_close=99.5,
        volume=1000.0,
        amount=123400.0,
    )
    values.update(overrides)
    return DailyBar(**values)


def _calendar(*open_dates, closed_dates=()):
    entries = [
        TradingCalendar(exchange="SSE", trade_date=d, is_open=True) for d in open_dates
    ]
    entries += [
        TradingCalendar(exchange="SSE", trade_date=d, is_open=False) for d in closed_dates
    ]
    return entries


def _factor(**overrides) -> AdjFactor:
    values = dict(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        adj_factor=1.5,
    )
    values.update(overrides)
    return AdjFactor(**values)


def _daily_basic(**overrides) -> DailyBasic:
    values = dict(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        turnover_rate=0.052,
        total_mv=1_230_000.0,
        circ_mv=1_000_000.0,
    )
    values.update(overrides)
    return DailyBasic(**values)


class FakeProvider(DataProvider):
    def __init__(self, calendar, daily_by_date, adj_by_date=None, basic_by_date=None):
        self._calendar = calendar
        self._daily_by_date = daily_by_date
        self._adj_by_date = adj_by_date or {}
        self._basic_by_date = basic_by_date or {}
        self.downloaded_dates = []
        self.adj_downloaded_dates = []
        self.basic_downloaded_dates = []

    def get_securities(self):
        return []

    def get_trading_calendar(self, start_date, end_date):
        return [c for c in self._calendar if start_date <= c.trade_date <= end_date]

    def get_daily_bars(self, instrument_ids, start_date, end_date):
        return []

    def get_daily_bars_by_date(self, trade_date):
        self.downloaded_dates.append(trade_date)
        return self._daily_by_date.get(trade_date, [])

    def get_adj_factors_by_date(self, trade_date):
        self.adj_downloaded_dates.append(trade_date)
        return self._adj_by_date.get(trade_date, [])

    def get_daily_basic_by_date(self, trade_date):
        self.basic_downloaded_dates.append(trade_date)
        return self._basic_by_date.get(trade_date, [])


def test_validate_ok() -> None:
    validate_daily_bars([_bar()], date(2026, 1, 2))


def test_validate_empty_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([], date(2026, 1, 2))


def test_validate_duplicate_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(), _bar()], date(2026, 1, 2))


def test_validate_wrong_trade_date_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(trade_date=date(2026, 1, 3))], date(2026, 1, 2))


def test_validate_invalid_high_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(high=99.0)], date(2026, 1, 2))


def test_validate_invalid_low_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(low=101.0)], date(2026, 1, 2))


def test_validate_negative_volume_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(volume=-1.0)], date(2026, 1, 2))


def test_validate_negative_amount_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(amount=-1.0)], date(2026, 1, 2))


def test_validate_inf_rejected() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(open=float("inf"))], date(2026, 1, 2))
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(volume=float("-inf"))], date(2026, 1, 2))


def test_validate_nonpositive_price_rejected() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(close=0.0)], date(2026, 1, 2))
    with pytest.raises(DataValidationError):
        validate_daily_bars([_bar(open=-1.0)], date(2026, 1, 2))


def test_filter_unlisted_placeholder() -> None:
    trade_date = date(2011, 12, 12)
    bar = _bar(instrument_id="920090.BJ", trade_date=trade_date, pre_close=None)
    list_dates = {"920090.BJ": date(2021, 8, 9)}
    kept, filtered = filter_unlisted_placeholders([bar], list_dates, trade_date)
    assert kept == []
    assert filtered == 1


def test_filter_listed_stock_missing_pre_close_not_filtered() -> None:
    trade_date = date(2026, 1, 2)
    bar = _bar(trade_date=trade_date, pre_close=None)
    list_dates = {"600519.SH": date(2001, 8, 27)}
    kept, filtered = filter_unlisted_placeholders([bar], list_dates, trade_date)
    assert kept == [bar]
    assert filtered == 0
    with pytest.raises(DataValidationError):
        validate_daily_bars(kept, trade_date)


def test_filter_normal_stock_unaffected() -> None:
    trade_date = date(2026, 1, 2)
    bar = _bar(trade_date=trade_date)
    list_dates = {"600519.SH": date(2001, 8, 27)}
    kept, filtered = filter_unlisted_placeholders([bar], list_dates, trade_date)
    assert kept == [bar]
    assert filtered == 0
    validate_daily_bars(kept, trade_date)


def test_sync_only_downloads_open_days(tmp_path) -> None:
    open_day = date(2026, 1, 5)
    closed_day = date(2026, 1, 4)
    provider = FakeProvider(
        calendar=_calendar(open_day, closed_dates=(closed_day,)),
        daily_by_date={open_day: [_bar(trade_date=open_day)]},
    )
    storage = ParquetStorage(tmp_path)
    result = sync_daily_history(provider, storage, date(2026, 1, 1), date(2026, 1, 5))
    assert provider.downloaded_dates == [open_day]
    assert result.total == 1
    assert result.synced == 1
    assert result.skipped == 0


def test_sync_skips_existing(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_daily_bars_by_date([_bar(trade_date=day)], day)
    provider = FakeProvider(
        calendar=_calendar(day),
        daily_by_date={day: [_bar(trade_date=day)]},
    )
    result = sync_daily_history(provider, storage, date(2026, 1, 1), date(2026, 1, 5))
    assert provider.downloaded_dates == []
    assert result.skipped == 1


def test_sync_force_overwrites(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_daily_bars_by_date([_bar(trade_date=day)], day)
    provider = FakeProvider(
        calendar=_calendar(day),
        daily_by_date={day: [_bar(trade_date=day, close=100.6)]},
    )
    result = sync_daily_history(
        provider, storage, date(2026, 1, 1), date(2026, 1, 5), force=True
    )
    assert provider.downloaded_dates == [day]
    assert result.synced == 1
    assert storage.load_daily_bars_by_date(day)[0].close == 100.6


def test_sync_failure_keeps_prior_days(tmp_path) -> None:
    day1 = date(2026, 1, 5)
    day2 = date(2026, 1, 6)
    provider = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={day1: [_bar(trade_date=day1)], day2: []},
    )
    storage = ParquetStorage(tmp_path)
    with pytest.raises(DataValidationError):
        sync_daily_history(provider, storage, date(2026, 1, 1), date(2026, 1, 6))
    assert storage.daily_bars_exists(day1)
    assert not storage.daily_bars_exists(day2)


def test_sync_resumes_missing_dates(tmp_path) -> None:
    day1 = date(2026, 1, 5)
    day2 = date(2026, 1, 6)
    storage = ParquetStorage(tmp_path)
    failing = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={day1: [_bar(trade_date=day1)], day2: []},
    )
    with pytest.raises(DataValidationError):
        sync_daily_history(failing, storage, date(2026, 1, 1), date(2026, 1, 6))

    resumed = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={day2: [_bar(trade_date=day2)]},
    )
    result = sync_daily_history(resumed, storage, date(2026, 1, 1), date(2026, 1, 6))
    assert resumed.downloaded_dates == [day2]
    assert result.synced == 1
    assert result.skipped == 1


def test_validate_adj_factors_ok() -> None:
    validate_adj_factors([_factor()], date(2026, 1, 2))


def test_validate_adj_factors_nonpositive_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_adj_factors([_factor(adj_factor=0.0)], date(2026, 1, 2))


def test_validate_adj_factors_nan_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_adj_factors([_factor(adj_factor=float("nan"))], date(2026, 1, 2))


def test_validate_adj_factors_inf_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_adj_factors([_factor(adj_factor=float("inf"))], date(2026, 1, 2))


def test_validate_adj_factors_duplicate_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_adj_factors([_factor(), _factor()], date(2026, 1, 2))


def test_validate_adj_factors_wrong_date_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_adj_factors([_factor(trade_date=date(2026, 1, 3))], date(2026, 1, 2))


def test_sync_adj_factor_only_open_days(tmp_path) -> None:
    open_day = date(2026, 1, 5)
    closed_day = date(2026, 1, 4)
    provider = FakeProvider(
        calendar=_calendar(open_day, closed_dates=(closed_day,)),
        daily_by_date={},
        adj_by_date={open_day: [_factor(trade_date=open_day)]},
    )
    storage = ParquetStorage(tmp_path)
    result = sync_adj_factor_history(provider, storage, date(2026, 1, 1), date(2026, 1, 5))
    assert provider.adj_downloaded_dates == [open_day]
    assert result.synced == 1


def test_sync_adj_factor_skips_existing(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_adj_factors_by_date([_factor(trade_date=day)], day)
    provider = FakeProvider(
        calendar=_calendar(day),
        daily_by_date={},
        adj_by_date={day: [_factor(trade_date=day)]},
    )
    result = sync_adj_factor_history(provider, storage, date(2026, 1, 1), date(2026, 1, 5))
    assert provider.adj_downloaded_dates == []
    assert result.skipped == 1


def test_sync_adj_factor_force_overwrites(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_adj_factors_by_date([_factor(trade_date=day)], day)
    provider = FakeProvider(
        calendar=_calendar(day),
        daily_by_date={},
        adj_by_date={day: [_factor(trade_date=day, adj_factor=2.0)]},
    )
    result = sync_adj_factor_history(
        provider, storage, date(2026, 1, 1), date(2026, 1, 5), force=True
    )
    assert result.synced == 1
    assert provider.adj_downloaded_dates == [day]
    assert storage.load_adj_factors_by_date(day)[0].adj_factor == 2.0


def test_sync_adj_factor_resumes_missing(tmp_path) -> None:
    day1 = date(2026, 1, 5)
    day2 = date(2026, 1, 6)
    storage = ParquetStorage(tmp_path)
    failing = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={},
        adj_by_date={day1: [_factor(trade_date=day1)], day2: []},
    )
    with pytest.raises(DataValidationError):
        sync_adj_factor_history(failing, storage, date(2026, 1, 1), date(2026, 1, 6))
    assert storage.adj_factor_exists(day1)
    assert not storage.adj_factor_exists(day2)

    resumed = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={},
        adj_by_date={day2: [_factor(trade_date=day2)]},
    )
    result = sync_adj_factor_history(resumed, storage, date(2026, 1, 1), date(2026, 1, 6))
    assert resumed.adj_downloaded_dates == [day2]
    assert result.synced == 1
    assert result.skipped == 1


def test_audit_daily_adj_coverage(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_daily_bars_by_date(
        [
            _bar(instrument_id="600519.SH", trade_date=day),
            _bar(instrument_id="000001.SZ", trade_date=day),
        ],
        day,
    )
    storage.save_adj_factors_by_date(
        [
            _factor(instrument_id="600519.SH", trade_date=day),
            _factor(instrument_id="300750.SZ", trade_date=day),
        ],
        day,
    )
    cov = audit_daily_adj_coverage(storage, day)
    assert cov.daily_count == 2
    assert cov.adj_factor_count == 2
    assert cov.overlap_count == 1
    assert cov.daily_missing_adj_count == 1
    assert cov.adj_extra_count == 1
    assert cov.missing_sample == ("000001.SZ",)
    assert cov.extra_sample == ("300750.SZ",)


def test_validate_daily_basic_ok() -> None:
    validate_daily_basic([_daily_basic()], date(2026, 1, 2))


def test_validate_daily_basic_invalid_total_mv() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_basic([_daily_basic(total_mv=0.0)], date(2026, 1, 2))


def test_validate_daily_basic_invalid_circ_mv() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_basic([_daily_basic(circ_mv=-1.0)], date(2026, 1, 2))


def test_validate_daily_basic_negative_turnover() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_basic([_daily_basic(turnover_rate=-0.1)], date(2026, 1, 2))


def test_validate_daily_basic_duplicate() -> None:
    with pytest.raises(DataValidationError):
        validate_daily_basic([_daily_basic(), _daily_basic()], date(2026, 1, 2))


def test_sync_daily_basic_skips_existing(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_daily_basic_by_date([_daily_basic(trade_date=day)], day)
    provider = FakeProvider(
        calendar=_calendar(day),
        daily_by_date={},
        basic_by_date={day: [_daily_basic(trade_date=day)]},
    )
    result = sync_daily_basic_history(provider, storage, date(2026, 1, 1), date(2026, 1, 5))
    assert provider.basic_downloaded_dates == []
    assert result.skipped == 1


def test_sync_daily_basic_force_overwrites(tmp_path) -> None:
    day = date(2026, 1, 5)
    storage = ParquetStorage(tmp_path)
    storage.save_daily_basic_by_date([_daily_basic(trade_date=day)], day)
    provider = FakeProvider(
        calendar=_calendar(day),
        daily_by_date={},
        basic_by_date={day: [_daily_basic(trade_date=day, total_mv=999.0)]},
    )
    result = sync_daily_basic_history(
        provider, storage, date(2026, 1, 1), date(2026, 1, 5), force=True
    )
    assert result.synced == 1
    assert storage.load_daily_basic_by_date(day)[0].total_mv == 999.0
