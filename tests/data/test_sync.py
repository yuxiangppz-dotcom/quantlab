from datetime import date

import pytest

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DataValidationError,
    IndexDailyBar,
    RawLifecycleAnnouncement,
    StockSTStatus,
    SuspensionRecord,
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
    sync_index_daily_history,
    sync_lifecycle_announcement_index,
    sync_lifecycle_context,
    validate_adj_factors,
    validate_daily_bars,
    validate_daily_basic,
    validate_index_daily_bars,
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
    def __init__(
        self, calendar, daily_by_date, adj_by_date=None, basic_by_date=None, st=None, susp=None,
        index_daily=None,
    ):
        self._calendar = calendar
        self._daily_by_date = daily_by_date
        self._adj_by_date = adj_by_date or {}
        self._basic_by_date = basic_by_date or {}
        self.downloaded_dates = []
        self.adj_downloaded_dates = []
        self.basic_downloaded_dates = []
        self._st = st or {}
        self._susp = susp or {}
        self.st_dates = []
        self.susp_dates = []
        self._index_daily = index_daily or {}
        self.index_downloaded = []

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

    def get_lifecycle_announcements_by_date(self, announcement_date):
        return getattr(self, "_announcements", {}).get(announcement_date, [])

    def get_stock_st_by_date(self, trade_date):
        self.st_dates.append(trade_date)
        return self._st.get(trade_date, [])

    def get_suspensions_by_date(self, trade_date):
        self.susp_dates.append(trade_date)
        return self._susp.get(trade_date, [])

    def get_name_changes(self, instrument_id, start_date, end_date):
        return []

    def get_index_daily(self, instrument_id, start_date, end_date):
        self.index_downloaded.append(instrument_id)
        return [
            bar
            for day, bars in self._index_daily.items()
            for bar in bars
            if bar.instrument_id == instrument_id
            and start_date <= day <= end_date
        ]


def _st(day, **overrides):
    values = {"instrument_id": "002509.SZ", "trade_date": day, "name": "*ST天广",
              "status": "ST", "type_name": "风险警示板", "source_record_id": "st"}
    values.update(overrides)
    return StockSTStatus(**values)


def _susp(day, **overrides):
    values = {"instrument_id": "002509.SZ", "trade_date": day, "suspend_type": "S",
              "suspend_timing": None, "source_record_id": "susp"}
    values.update(overrides)
    return SuspensionRecord(**values)


def _announcement(day, row_id="a"):
    return RawLifecycleAnnouncement(
        source="tushare.anns_d", source_record_id=row_id, instrument_id="002509.SZ",
        announcement_date=day, announcement_time=None, title="公告", source_url=None,
        raw_payload="{}", content_fingerprint=row_id,
    )


def test_context_sync_is_per_open_session_resumable_and_complete(tmp_path) -> None:
    day1, day2 = date(2020, 1, 2), date(2020, 1, 3)
    provider = FakeProvider(_calendar(day1, day2), {}, st={day1: [_st(day1)], day2: []},
                            susp={day1: [_susp(day1)], day2: []})
    storage = ParquetStorage(tmp_path)
    first = sync_lifecycle_context(provider, storage, day1, day2)
    assert first["stock_st"].complete and first["suspend_d"].complete
    assert provider.st_dates == [day1, day2]
    assert provider.susp_dates == [day1, day2]
    second = sync_lifecycle_context(provider, storage, day1, day2)
    assert second["stock_st"].completed_sessions == 2
    assert provider.st_dates == [day1, day2]


def test_context_sync_rejects_wrong_date_and_marks_limit_incomplete(tmp_path) -> None:
    day = date(2020, 1, 2)
    provider = FakeProvider(_calendar(day), {}, st={day: [_st(date(2020, 1, 3))]}, susp={day: []})
    with pytest.raises(DataValidationError, match="scope mismatch"):
        sync_lifecycle_context(provider, ParquetStorage(tmp_path), day, day)

    provider = FakeProvider(_calendar(day), {}, st={day: [_st(day)] * 1000}, susp={day: []})
    result = sync_lifecycle_context(provider, ParquetStorage(tmp_path), day, day)
    assert not result["stock_st"].complete
    assert result["stock_st"].truncated_sessions == ("2020-01-02",)


def test_announcement_limit_2000_is_never_saved_as_complete(tmp_path) -> None:
    day = date(2020, 1, 2)
    provider = FakeProvider(_calendar(day), {})
    provider._announcements = {day: [_announcement(day, str(index)) for index in range(2000)]}
    storage = ParquetStorage(tmp_path)
    with pytest.raises(DataValidationError, match="page limit"):
        sync_lifecycle_announcement_index(provider, storage, day, day)
    assert not storage.lifecycle_announcements_exists(day)


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


def _index_bar(instrument_id="000300.SH", day=date(2026, 1, 5), close=4000.0, **overrides):
    values = dict(
        instrument_id=instrument_id,
        trade_date=day,
        open=close * 0.999,
        high=close * 1.001,
        low=close * 0.998,
        close=close,
        pre_close=close * 0.997,
        volume=1_000_000.0,
        amount=4_000_000_000.0,
    )
    values.update(overrides)
    return IndexDailyBar(**values)


def test_validate_index_daily_ok() -> None:
    validate_index_daily_bars([_index_bar()], date(2026, 1, 5))


def test_validate_index_daily_empty_is_accepted() -> None:
    # an index launched after the requested window legitimately has no row
    validate_index_daily_bars([], date(2026, 1, 5))


def test_validate_index_daily_duplicate_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_index_daily_bars([_index_bar(), _index_bar()], date(2026, 1, 5))


def test_validate_index_daily_wrong_date_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_index_daily_bars(
            [_index_bar(day=date(2026, 1, 6))], date(2026, 1, 5)
        )


def test_validate_index_daily_nonpositive_close_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_index_daily_bars([_index_bar(close=0.0)], date(2026, 1, 5))


def test_validate_index_daily_invalid_high_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_index_daily_bars([_index_bar(high=1.0)], date(2026, 1, 5))


def test_validate_index_daily_tolerates_missing_turnover() -> None:
    # early index history may omit volume/amount (NaN); returns use close only
    validate_index_daily_bars(
        [_index_bar(volume=float("nan"), amount=float("nan"))], date(2026, 1, 5)
    )
    with pytest.raises(DataValidationError):
        validate_index_daily_bars([_index_bar(volume=-1.0)], date(2026, 1, 5))


def test_sync_index_daily_writes_partitions_per_open_day(tmp_path) -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    provider = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={},
        index_daily={
            day1: [_index_bar(day=day1), _index_bar("000905.SH", day1, close=5000.0)],
            day2: [_index_bar(day=day2), _index_bar("000905.SH", day2, close=5010.0)],
        },
    )
    storage = ParquetStorage(tmp_path)
    result = sync_index_daily_history(
        provider, storage, day1, day2,
        instrument_ids=["000300.SH", "000905.SH"],
    )
    assert result.total == 2 and result.synced == 2 and result.skipped == 0
    assert provider.index_downloaded == ["000300.SH", "000905.SH"]
    day1_rows = {b.instrument_id: b for b in storage.load_index_daily_by_date(day1)}
    assert set(day1_rows) == {"000300.SH", "000905.SH"}
    assert day1_rows["000905.SH"].close == 5000.0
    assert storage.index_daily_exists(day2)


def test_sync_index_daily_skips_existing_and_resumes(tmp_path) -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    storage = ParquetStorage(tmp_path)
    storage.save_index_daily_by_date([_index_bar(day=day1)], day1)
    provider = FakeProvider(
        calendar=_calendar(day1, day2),
        daily_by_date={},
        index_daily={day2: [_index_bar(day=day2)]},
    )
    result = sync_index_daily_history(
        provider, storage, day1, day2, instrument_ids=["000300.SH"]
    )
    assert provider.index_downloaded == ["000300.SH"]
    assert result.synced == 1 and result.skipped == 1
    # resume again: everything covered, no fetch
    provider2 = FakeProvider(_calendar(day1, day2), daily_by_date={})
    result2 = sync_index_daily_history(
        provider2, storage, day1, day2, instrument_ids=["000300.SH"]
    )
    assert provider2.index_downloaded == []
    assert result2.synced == 0 and result2.skipped == 2


def test_sync_index_daily_persists_empty_day(tmp_path) -> None:
    day = date(2026, 1, 5)
    provider = FakeProvider(_calendar(day), daily_by_date={}, index_daily={})
    storage = ParquetStorage(tmp_path)
    result = sync_index_daily_history(
        provider, storage, day, day, instrument_ids=["000300.SH"]
    )
    assert result.synced == 1
    assert storage.index_daily_exists(day)
    assert storage.load_index_daily_by_date(day) == []
    # a rerun skips the persisted empty day instead of re-querying
    provider2 = FakeProvider(_calendar(day), daily_by_date={}, index_daily={})
    result2 = sync_index_daily_history(
        provider2, storage, day, day, instrument_ids=["000300.SH"]
    )
    assert provider2.index_downloaded == []
    assert result2.skipped == 1


def test_sync_index_daily_requires_instruments(tmp_path) -> None:
    provider = FakeProvider(_calendar(date(2026, 1, 5)), daily_by_date={})
    with pytest.raises(DataValidationError):
        sync_index_daily_history(
            provider, ParquetStorage(tmp_path), date(2026, 1, 5), date(2026, 1, 5),
            instrument_ids=[],
        )
