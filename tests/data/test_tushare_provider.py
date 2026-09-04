from datetime import date

import pandas as pd
import pytest

import quantlab.data.tushare_provider as tushare_provider
from quantlab.data.models import DailyBar, DataValidationError, Security, TradingCalendar
from quantlab.data.tushare_provider import TushareProvider


def _daily_row(**overrides):
    row = {
        "ts_code": "600519.SH",
        "trade_date": "20260102",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "pre_close": 99.5,
        "vol": 10.0,
        "amount": 123.4,
    }
    row.update(overrides)
    return row


def test_security_from_row() -> None:
    row = {
        "ts_code": "600519.SH",
        "symbol": "600519",
        "name": "贵州茅台",
        "exchange": "SSE",
        "market": "主板",
        "list_status": "L",
        "list_date": "20010827",
        "delist_date": None,
    }
    assert tushare_provider.security_from_row(row) == Security(
        instrument_id="600519.SH",
        symbol="600519",
        name="贵州茅台",
        exchange="SSE",
        market="SH",
        board="主板",
        list_status="L",
        list_date=date(2001, 8, 27),
        delist_date=None,
    )


def test_calendar_from_row() -> None:
    row = {"exchange": "SSE", "cal_date": "20260101", "is_open": 1}
    assert tushare_provider.calendar_from_row(row) == TradingCalendar(
        exchange="SSE",
        trade_date=date(2026, 1, 1),
        is_open=True,
    )


def test_daily_bar_from_row() -> None:
    assert tushare_provider.daily_bar_from_row(_daily_row()) == DailyBar(
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


def test_daily_bar_volume_unit_conversion() -> None:
    # Tushare vol (hands) -> canonical volume (shares)
    assert tushare_provider.daily_bar_from_row(_daily_row(vol=10)).volume == 1000.0


def test_daily_bar_amount_unit_conversion() -> None:
    # Tushare amount (thousands of yuan) -> canonical amount (yuan)
    assert tushare_provider.daily_bar_from_row(_daily_row(amount=123.4)).amount == 123400.0


def test_required_trade_date_missing_raises() -> None:
    with pytest.raises(DataValidationError):
        tushare_provider.daily_bar_from_row(_daily_row(trade_date=None))


def test_missing_token_raises(monkeypatch) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TUSHARE_TOKEN"):
        TushareProvider(token=None)


_SECURITY_ROWS = {
    "L": {
        "ts_code": "600519.SH",
        "symbol": "600519",
        "name": "贵州茅台",
        "exchange": "SSE",
        "market": "主板",
        "list_status": "L",
        "list_date": "20010827",
        "delist_date": None,
    },
    "D": {
        "ts_code": "000001.SZ",
        "symbol": "000001",
        "name": "平安银行",
        "exchange": "SZSE",
        "market": "主板",
        "list_status": "D",
        "list_date": "19910403",
        "delist_date": "20260101",
    },
    "P": {
        "ts_code": "300001.SZ",
        "symbol": "300001",
        "name": "特锐德",
        "exchange": "SZSE",
        "market": "创业板",
        "list_status": "P",
        "list_date": "20091030",
        "delist_date": None,
    },
}


class _FakePro:
    def stock_basic(self, **kwargs):
        return pd.DataFrame([_SECURITY_ROWS[kwargs["list_status"]]])

    def trade_cal(self, **kwargs):
        return pd.DataFrame([
            {"exchange": kwargs["exchange"], "cal_date": "20260101", "is_open": 1},
        ])

    def daily(self, **kwargs):
        return pd.DataFrame([_daily_row()])


def test_get_securities(monkeypatch) -> None:
    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: _FakePro())
    provider = TushareProvider(token="dummy")
    result = provider.get_securities()
    assert {item.list_status for item in result} == {"L", "D", "P"}
    assert {item.instrument_id for item in result} == {"600519.SH", "000001.SZ", "300001.SZ"}
    assert {item.board for item in result} == {"主板", "创业板"}


def test_get_trading_calendar(monkeypatch) -> None:
    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: _FakePro())
    provider = TushareProvider(token="dummy")
    result = provider.get_trading_calendar(date(2026, 1, 1), date(2026, 1, 5))
    assert [item.exchange for item in result] == ["SSE", "SZSE"]


def test_get_daily_bars(monkeypatch) -> None:
    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: _FakePro())
    provider = TushareProvider(token="dummy")
    result = provider.get_daily_bars(["600519.SH"], date(2026, 1, 1), date(2026, 1, 5))
    assert len(result) == 1
    assert result[0].instrument_id == "600519.SH"
