from datetime import date

import pandas as pd
import pytest

import quantlab.data.tushare_provider as tushare_provider
from quantlab.data.models import DailyBar, Security, TradingCalendar
from quantlab.data.tushare_provider import TushareProvider


def test_security_from_row() -> None:
    row = {
        "ts_code": "600519.SH",
        "symbol": "600519",
        "name": "贵州茅台",
        "list_date": "20010827",
        "delist_date": None,
    }
    assert tushare_provider.security_from_row(row) == Security(
        instrument_id="600519.SH",
        symbol="600519",
        name="贵州茅台",
        exchange="SSE",
        market="SH",
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
    row = {
        "ts_code": "600519.SH",
        "trade_date": "20260102",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "pre_close": 99.5,
        "vol": 10000.0,
        "amount": 100000.0,
    }
    assert tushare_provider.daily_bar_from_row(row) == DailyBar(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        pre_close=99.5,
        volume=10000.0,
        amount=100000.0,
    )


def test_missing_token_raises(monkeypatch) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TUSHARE_TOKEN"):
        TushareProvider(token=None)


class _FakePro:
    def stock_basic(self, **kwargs):
        return pd.DataFrame([
            {
                "ts_code": "600519.SH",
                "symbol": "600519",
                "name": "贵州茅台",
                "list_date": "20010827",
                "delist_date": None,
            },
        ])

    def trade_cal(self, **kwargs):
        return pd.DataFrame([
            {"exchange": kwargs["exchange"], "cal_date": "20260101", "is_open": 1},
        ])

    def daily(self, **kwargs):
        return pd.DataFrame([
            {
                "ts_code": "600519.SH",
                "trade_date": "20260102",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "pre_close": 99.5,
                "vol": 10000.0,
                "amount": 100000.0,
            },
        ])


def test_get_securities(monkeypatch) -> None:
    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: _FakePro())
    provider = TushareProvider(token="dummy")
    result = provider.get_securities()
    assert len(result) == 1
    assert result[0].instrument_id == "600519.SH"
    assert result[0].market == "SH"


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
