"""Date-filtered Arrow reads; no full-history pandas materialization."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from quantlab.research.ml.data import execution_labels, validate_features


def read_range(path, start, end, *, columns=None, max_bytes=8_000_000_000):
    dataset = ds.dataset(path, format="parquet")
    field = dataset.schema.field("trade_date")
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if pa.types.is_date(field.type):
        left, right = start.date(), end.date()
    elif pa.types.is_timestamp(field.type):
        left, right = start.to_pydatetime(), end.to_pydatetime()
    elif pa.types.is_string(field.type):
        left, right = str(start.date()), str(end.date())
    else:
        raise ValueError("unsupported trade_date storage type")
    where = (ds.field("trade_date") >= left) & (ds.field("trade_date") <= right)
    rows = dataset.count_rows(filter=where)
    width = len(columns) if columns else len(dataset.schema)
    if rows * (width + 12) * 8 * 6 > max_bytes:
        raise MemoryError("selected window exceeds memory budget; narrow universe or raise budget")
    return dataset.to_table(filter=where, columns=columns).to_pandas()


def fold_panel(bundle, fold, names, sessions, config):
    features = read_range(
        bundle / "features.parquet",
        fold.train_start,
        fold.test_end,
        max_bytes=config.max_matrix_bytes,
    )
    features = validate_features(features, names, sessions, config)
    tail = min(len(sessions) - 1, sessions.get_loc(fold.test_end) + 1 + config.horizon_sessions)
    prices = read_range(
        bundle / "prices.parquet",
        fold.train_start,
        sessions[tail],
        columns=["trade_date", "instrument_id", "adj_close"],
        max_bytes=config.max_matrix_bytes,
    )
    labels = execution_labels(prices, sessions, config)
    return features.merge(
        labels, on=["trade_date", "instrument_id"], how="left", validate="one_to_one"
    )
