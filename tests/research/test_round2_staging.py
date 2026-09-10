from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import AdjFactor, DailyBar, DailyBasic, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.research import round2_dataset as staging
from quantlab.research.round2_diagnostics import load_config


def test_staging_retains_source_and_continuous_macd_across_year(tmp_path, monkeypatch):
    config = load_config()
    config.update(read_start="2021-10-01", data_start="2021-10-01", data_end="2022-02-01")
    all_days = pd.date_range(config["read_start"], config["data_end"])
    sessions = list(pd.bdate_range(config["read_start"], config["data_end"]).date)
    storage = ParquetStorage(tmp_path / "data/canonical")
    storage.save_securities(
        [
            Security(
                "000001.SZ",
                "000001",
                "SYNTHETIC",
                "SZSE",
                "SZ",
                "主板",
                "L",
                date(2000, 1, 1),
                None,
            )
        ]
    )
    storage.save_trading_calendar(
        [
            TradingCalendar(exchange, day.date(), day.weekday() < 5)
            for day in all_days
            for exchange in ("SSE", "SZSE")
        ]
    )
    for i, day in enumerate(sessions):
        price = 10 + i / 10 + np.sin(i / 5)
        storage.save_daily_bars_by_date(
            [DailyBar("000001.SZ", day, price, price + 1, price - 1, price, price, 100.0, 1000.0)],
            day,
        )
        storage.save_adj_factors_by_date([AdjFactor("000001.SZ", day, 1.0)], day)
        storage.save_daily_basic_by_date([DailyBasic("000001.SZ", day, 1.0, 200.0, 100.0)], day)
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "security_code_changes.csv").write_text(
        "old_instrument_id,new_instrument_id,effective_date,old_name,original_list_date\n"
    )
    snapshot = tmp_path / config["index_snapshot"]
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("synthetic source bound in test")
    bundle = {
        "request": {"semantics": {"execution_authority": False}},
        "series": [
            {
                "metadata": {"ts_code": code},
                "rows": [
                    {"trade_date": str(day), "close": i + 100.0, "index_published_on_date": True}
                    for i, day in enumerate(sessions)
                ],
            }
            for code in ("000001.SH", "000688.SH")
        ],
    }
    monkeypatch.setattr(staging, "code_binding", lambda *args: "synthetic-only")
    monkeypatch.setattr(staging, "load_index_context", lambda *args, **kw: bundle)
    out = tmp_path / "stage"
    result = staging.prepare_dataset(tmp_path, out, config, progress=lambda *a, **kw: None)
    staging.verify_entries(tmp_path, result["inputs"])
    staging.verify_entries(out, result["artifacts"])
    dataset = pd.read_parquet(out / "dataset.parquet").sort_values("trade_date")
    assert len(dataset) == len(sessions)
    assert dataset.macd_hist_normalized.iloc[:59].isna().all()
    assert dataset.macd_hist_normalized.iloc[59:].notna().all()
    prices = pd.Series([10 + i / 10 + np.sin(i / 5) for i in range(len(sessions))])
    np.testing.assert_allclose(
        dataset.future_return_20d.iloc[:-20], (prices.shift(-20) / prices - 1).iloc[:-20]
    )
    assert dataset.future_return_20d.iloc[-20:].isna().all()
    assert len(result["source_coverage"]) == len(sessions)
    assert result["performance_eligible"] is False
    with pytest.raises(FileExistsError):
        staging.prepare_dataset(tmp_path, out, config)
