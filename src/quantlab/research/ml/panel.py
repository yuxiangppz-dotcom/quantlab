"""Date-filtered Arrow reads; no full-history pandas materialization."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from quantlab.research.ml.data import execution_labels, validate_features


def read_range(
    path,
    start,
    end,
    *,
    columns=None,
    max_bytes=8_000_000_000,
    eligible_only=False,
    instruments=None,
):
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
    if eligible_only:
        where = where & (ds.field("eligible") == True)  # noqa: E712 -- Arrow expression
    if instruments is not None:
        where = where & ds.field("instrument_id").isin(list(instruments))
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
        eligible_only=True,
    )
    features = validate_features(features, names, sessions, config)
    audit_window(bundle, features, names, config)
    tail = min(len(sessions) - 1, sessions.get_loc(fold.test_end) + 1 + config.horizon_sessions)
    prices = read_range(
        bundle / "prices.parquet",
        fold.train_start,
        sessions[tail],
        columns=["trade_date", "instrument_id", "adj_close"],
        instruments=features.instrument_id.unique(),
        max_bytes=config.max_matrix_bytes,
    )
    labels = execution_labels(prices, sessions, config)
    return features.merge(
        labels, on=["trade_date", "instrument_id"], how="left", validate="one_to_one"
    )


def audit_window(bundle, features, names, config):
    import json

    from quantlab.research.ml.pit import validate_lineage

    contract = bundle / "feature_contract.json"
    if not contract.exists():
        return {"status": "source_lineage_not_supplied", "historical_data_certified": False}
    dependencies = json.loads(contract.read_text())["feature_dependencies"]
    if set(dependencies) != set(names):
        raise ValueError("feature dependency contract must cover the exact feature allowlist")
    if features.empty:
        return {"status": "empty_window"}
    lineage = read_range(
        bundle / "pit_lineage.parquet",
        features.trade_date.min(),
        features.trade_date.max(),
        max_bytes=config.max_matrix_bytes,
    )
    return validate_lineage(features, lineage, dependencies, decision_hour=config.decision_hour)
