import pandas as pd

from quantlab.research import filter_v1_universe


def _frame(instrument_ids) -> pd.DataFrame:
    return pd.DataFrame({
        "instrument_id": list(instrument_ids),
        "trade_date": ["2026-01-05"] * len(instrument_ids),
    })


def test_v1_universe_inclusion() -> None:
    frame = _frame(["600519.SH", "688981.SH", "000001.SZ", "300750.SZ"])
    result = filter_v1_universe(frame)
    assert set(result["instrument_id"]) == {
        "600519.SH", "688981.SH", "000001.SZ", "300750.SZ",
    }


def test_v1_universe_exclusion() -> None:
    frame = _frame(["900901.SH", "200001.SZ", "920002.BJ", "830001.BJ"])
    assert filter_v1_universe(frame).empty


def test_v1_universe_mixed() -> None:
    frame = _frame(["600519.SH", "000001.SZ", "900901.SH", "920002.BJ"])
    result = filter_v1_universe(frame)
    assert set(result["instrument_id"]) == {"600519.SH", "000001.SZ"}


def test_v1_universe_unique_preserved() -> None:
    frame = _frame(["600519.SH", "000001.SZ"])
    result = filter_v1_universe(frame)
    assert not result.duplicated(subset=["instrument_id", "trade_date"]).any()
