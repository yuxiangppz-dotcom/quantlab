from datetime import date

import pandas as pd

from quantlab.data.models import DailyBasic, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.research.characteristics import load_market_characteristics


def _basic(instrument_id, trade_date, **overrides) -> DailyBasic:
    values = dict(
        instrument_id=instrument_id,
        trade_date=trade_date,
        turnover_rate=0.05,
        total_mv=1_000_000.0,
        circ_mv=800_000.0,
    )
    values.update(overrides)
    return DailyBasic(**values)


def _make_storage(tmp_path, dates, basic_by_date) -> ParquetStorage:
    storage = ParquetStorage(tmp_path)
    calendar = [TradingCalendar(exchange="SSE", trade_date=d, is_open=True) for d in dates]
    calendar += [TradingCalendar(exchange="SZSE", trade_date=d, is_open=True) for d in dates]
    storage.save_trading_calendar(calendar)
    for d in dates:
        storage.save_daily_basic_by_date(basic_by_date.get(d, []), d)
    return storage


def test_load_market_characteristics_columns(tmp_path) -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    storage = _make_storage(tmp_path, dates, {
        dates[0]: [_basic("600519.SH", dates[0])],
        dates[1]: [_basic("600519.SH", dates[1])],
    })
    df = load_market_characteristics(storage, dates[0], dates[1])
    assert list(df.columns) == [
        "instrument_id", "trade_date", "turnover_rate", "total_mv", "circ_mv",
    ]
    assert len(df) == 2


def test_characteristics_exact_date_join_no_fill(tmp_path) -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    storage = _make_storage(tmp_path, dates, {
        dates[0]: [_basic("600519.SH", dates[0])],  # dates[1] missing
    })
    chars = load_market_characteristics(storage, dates[0], dates[1])
    research = pd.DataFrame({
        "instrument_id": ["600519.SH", "600519.SH"],
        "trade_date": [dates[0], dates[1]],
        "close": [100.0, 101.0],
    })
    joined = research.merge(chars, on=["instrument_id", "trade_date"], how="left")
    assert joined.loc[joined["trade_date"] == dates[0], "circ_mv"].iloc[0] == 800_000.0
    assert pd.isna(joined.loc[joined["trade_date"] == dates[1], "circ_mv"].iloc[0])
