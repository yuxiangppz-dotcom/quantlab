from datetime import date

import pandas as pd
import pytest

import quantlab.data.tushare_provider as tushare_provider
from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DataValidationError,
    Security,
    TradingCalendar,
)
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


def test_daily_bar_pre_close_none_becomes_nan() -> None:
    bar = tushare_provider.daily_bar_from_row(_daily_row(pre_close=None))
    assert bar.pre_close != bar.pre_close  # NaN


def test_adj_factor_from_row() -> None:
    row = {"ts_code": "600519.SH", "trade_date": "20260901", "adj_factor": 1.5}
    assert tushare_provider.adj_factor_from_row(row) == AdjFactor(
        instrument_id="600519.SH",
        trade_date=date(2026, 9, 1),
        adj_factor=1.5,
    )


def test_adj_factor_required_trade_date_raises() -> None:
    with pytest.raises(DataValidationError):
        tushare_provider.adj_factor_from_row(
            {"ts_code": "600519.SH", "trade_date": None, "adj_factor": 1.5}
        )


def test_daily_basic_from_row_units() -> None:
    row = {
        "ts_code": "600519.SH",
        "trade_date": "20260701",
        "turnover_rate": 5.2,
        "total_mv": 123.0,
        "circ_mv": 100.0,
    }
    db = tushare_provider.daily_basic_from_row(row)
    assert db.instrument_id == "600519.SH"
    assert db.trade_date == date(2026, 7, 1)
    assert db.turnover_rate == pytest.approx(0.052)   # 5.2% -> decimal
    assert db.total_mv == pytest.approx(1_230_000.0)  # 123 万元 -> CNY
    assert db.circ_mv == pytest.approx(1_000_000.0)   # 100 万元 -> CNY


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

    def stock_st(self, **kwargs):
        return pd.DataFrame([{
            "ts_code": "002509.SZ", "trade_date": kwargs["trade_date"],
            "name": "*ST天广", "type": "ST", "type_name": "风险警示板",
        }])

    def suspend_d(self, **kwargs):
        return pd.DataFrame([{
            "ts_code": "002509.SZ", "trade_date": kwargs["trade_date"],
            "suspend_timing": "09:30-10:00", "suspend_type": "S",
        }])

    def anns_d(self, **kwargs):
        return pd.DataFrame(columns=["ann_date"])

    def namechange(self, **kwargs):
        return pd.DataFrame(columns=["ts_code", "start_date"])


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


def test_get_daily_bars_by_date(monkeypatch) -> None:
    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: _FakePro())
    provider = TushareProvider(token="dummy")
    result = provider.get_daily_bars_by_date(date(2026, 1, 2))
    assert len(result) == 1
    assert result[0].trade_date == date(2026, 1, 2)


def test_suspend_d_uses_trade_date_and_preserves_raw_s_r_fields(monkeypatch) -> None:
    fake = _FakePro()
    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: fake)
    result = TushareProvider(token="dummy").get_suspensions_by_date(date(2020, 5, 15))
    assert result[0].trade_date == date(2020, 5, 15)
    assert result[0].suspend_type == "S"
    assert result[0].suspend_timing == "09:30-10:00"
    assert not hasattr(result[0], "resume_date")


def test_capability_rejects_successful_wrong_date_scope(monkeypatch) -> None:
    class WrongDatePro(_FakePro):
        def suspend_d(self, **kwargs):
            return pd.DataFrame([{
                "ts_code": "002509.SZ", "trade_date": "20200514",
                "suspend_timing": None, "suspend_type": "S",
            }])

    monkeypatch.setattr(tushare_provider.ts, "pro_api", lambda token: WrongDatePro())
    capability = TushareProvider(token="dummy").probe_lifecycle_capabilities(date(2020, 5, 15))
    assert capability["suspend_d"]["status"] == "parameter_filter_mismatch"
