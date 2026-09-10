from dataclasses import replace
from datetime import date
from unittest.mock import Mock

import pytest

from quantlab.data.enrichment import sync_daily_price_limits
from quantlab.data.models import DailyBar, DailyPriceLimit, DataValidationError, SecurityCodeChange
from quantlab.data.storage import ParquetStorage

CHANGE = SecurityCodeChange(
    "300114.SZ", "302132.SZ", date(2025, 2, 17), "旧名称", date(2010, 8, 27)
)


def _limit(code, day):
    return DailyPriceLimit(code, day, 10, 12, 8, "SZSE", "tushare.stk_limit", code + str(day))


@pytest.fixture
def storage(tmp_path):
    from quantlab.data.models import Security

    result = ParquetStorage(tmp_path)
    result.save_securities(
        [
            Security(
                "302132.SZ",
                "302132",
                "新名称",
                "SZSE",
                "SZ",
                "创业板",
                "L",
                CHANGE.original_list_date,
                None,
            )
        ]
    )
    return result


@pytest.mark.parametrize(
    "day,expected", [(date(2025, 2, 14), "300114.SZ"), (date(2025, 2, 17), "302132.SZ")]
)
def test_limit_scope_uses_effective_code_and_keeps_identifier(storage, day, expected):
    storage.save_daily_bars_by_date([DailyBar(expected, day, 10, 10, 10, 10, 10, 100, 1000)], day)
    provider = Mock()
    provider.get_daily_price_limits_by_date.return_value = [
        _limit(code, day) for code in ("300114.SZ", "302132.SZ")
    ]
    result = sync_daily_price_limits(provider, storage, day, security_code_changes=[CHANGE])
    assert result.rows == 1
    assert storage.load_daily_price_limits_by_date(day)[0].instrument_id == expected


def test_missing_old_code_limit_cannot_be_silently_ignored(storage):
    day = date(2025, 2, 14)
    storage.save_daily_bars_by_date(
        [DailyBar("300114.SZ", day, 10, 10, 10, 10, 10, 100, 1000)], day
    )
    provider = Mock()
    provider.get_daily_price_limits_by_date.return_value = [_limit("302132.SZ", day)]
    with pytest.raises(DataValidationError):
        sync_daily_price_limits(provider, storage, day, security_code_changes=[CHANGE])
    assert not storage.daily_price_limit_exists(day)


def test_unverified_old_code_does_not_gain_identity_authority(storage):
    day = date(2025, 2, 14)
    provider = Mock()
    provider.get_daily_price_limits_by_date.return_value = [_limit("300114.SZ", day)]
    with pytest.raises(DataValidationError):
        sync_daily_price_limits(provider, storage, day, security_code_changes=[])


def test_unknown_daily_code_cannot_hide_behind_other_valid_limits(storage):
    day = date(2025, 2, 14)
    provider = Mock()
    provider.get_daily_price_limits_by_date.return_value = [_limit("302132.SZ", day)]
    storage.save_daily_bars_by_date(
        [DailyBar("300114.SZ", day, 10, 10, 10, 10, 10, 100, 1000)], day
    )
    with pytest.raises(DataValidationError, match="unverified historical"):
        sync_daily_price_limits(provider, storage, day, security_code_changes=[])


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_historical_identity_does_not_relax_limit_validity(storage, bad):
    day = date(2025, 2, 14)
    provider = Mock()
    provider.get_daily_price_limits_by_date.return_value = [
        replace(_limit("300114.SZ", day), down_limit=bad)
    ]
    with pytest.raises(DataValidationError):
        sync_daily_price_limits(provider, storage, day, security_code_changes=[CHANGE])
    assert not storage.daily_price_limit_exists(day)
