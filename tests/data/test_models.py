from datetime import date

import pytest

from quantlab.data.models import (
    format_yyyymmdd,
    market_from_symbol,
    parse_instrument_id,
    parse_yyyymmdd,
    to_instrument_id,
)


def test_market_from_symbol_shanghai() -> None:
    assert market_from_symbol("600519") == "SH"
    assert market_from_symbol("688981") == "SH"


def test_market_from_symbol_shenzhen() -> None:
    assert market_from_symbol("000001") == "SZ"
    assert market_from_symbol("300750") == "SZ"


def test_market_from_symbol_beijing() -> None:
    assert market_from_symbol("920002") == "BJ"


def test_market_from_symbol_invalid() -> None:
    with pytest.raises(ValueError):
        market_from_symbol("12345")


def test_to_instrument_id() -> None:
    assert to_instrument_id("600519") == "600519.SH"
    assert to_instrument_id("000001") == "000001.SZ"
    assert to_instrument_id("600519", "SH") == "600519.SH"


def test_parse_instrument_id() -> None:
    assert parse_instrument_id("600519.SH") == ("600519", "SH")
    assert parse_instrument_id("000001.SZ") == ("000001", "SZ")


def test_parse_yyyymmdd() -> None:
    assert parse_yyyymmdd("20260101") == date(2026, 1, 1)
    assert parse_yyyymmdd(None) is None


def test_format_yyyymmdd() -> None:
    assert format_yyyymmdd(date(2026, 9, 4)) == "20260904"
