"""Read-only bridge from existing sealed Alpha158 partitions to the v2 bundle."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.research.ml.io import research_output, seal_bundle, sha256, write_json


def export_history(history, pit_context_path, output, *, root):
    """PIT context must be supplied, never inferred from current security status.

    Context may define a smaller historical universe and date interval; only its
    keys are exported as features. All source price dates are retained so exact
    future label endpoints remain available. Parquet writers bound batch memory.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from quantlab.research.alpha158_native import load_contract
    from quantlab.research.alpha158_rolling_data import feature_path, receipt_file
    from quantlab.research.round2_dataset import sealed_read

    research_output(output)
    context_hash = sha256(pit_context_path)
    context = pd.read_parquet(pit_context_path)
    required = {"trade_date", "instrument_id", "eligible", "industry", "feature_available_at"}
    if (
        set(context.columns) != required
        or context.duplicated(["trade_date", "instrument_id"]).any()
    ):
        raise ValueError("PIT context requires unique keys and exactly the five documented columns")
    context["trade_date"] = pd.to_datetime(context.trade_date)
    if not context.eligible.map(lambda x: pd.isna(x) or isinstance(x, (bool, np.bool_))).all():
        raise ValueError("PIT eligibility must be boolean or unknown")
    if not context.feature_available_at.map(
        lambda x: pd.notna(x) and pd.Timestamp(x).tzinfo is not None
    ).all():
        raise ValueError("PIT feature availability must be timezone-aware")
    context["eligible"] = context.eligible.astype("boolean")
    context["industry"] = context.industry.astype("string")
    context["feature_available_at"] = pd.to_datetime(context.feature_available_at, utc=True)
    inventory = sealed_read(history / "inventory.json")
    plan = sealed_read(history / "plan.json")
    names = [f["name"] for f in load_contract(root)["features"]]
    output.mkdir(parents=True, exist_ok=False)
    receipts = [sealed_read(p) for p in sorted((history / "receipts").glob("raw-*.json"))]
    if not receipts:
        raise ValueError("history has no sealed raw receipts")
    feature_writer = price_writer = None
    seen_context = 0
    try:
        for number, _ in enumerate(plan["batches"]):
            features = pd.read_parquet(feature_path(history, number))
            features["trade_date"] = pd.to_datetime(features.trade_date)
            features = features[["trade_date", "instrument_id", *names]].merge(
                context, on=["trade_date", "instrument_id"], how="inner", validate="one_to_one"
            )
            seen_context += len(features)
            if len(features):
                table = pa.Table.from_pandas(features, preserve_index=False)
                if feature_writer is None:
                    feature_writer = pq.ParquetWriter(output / "features.parquet", table.schema)
                feature_writer.write_table(table)
            for receipt in receipts:
                if str(number) not in receipt["result"]["batch_rows"]:
                    continue
                matches = [
                    p for p in receipt["artifacts"] if p.endswith(f"/batch-{number:04d}.parquet")
                ]
                if len(matches) != 1:
                    raise ValueError("ambiguous raw partition receipt")
                raw = pd.read_parquet(
                    receipt_file(history, receipt, matches[0]),
                    columns=["trade_date", "instrument_id", "close", "adj_factor"],
                )
                raw = raw.loc[raw.instrument_id.isin(context.instrument_id.unique())].copy()
                price = raw.close * raw.adj_factor
                raw["adj_close"] = price.where(
                    raw.close.gt(0) & raw.adj_factor.gt(0) & np.isfinite(price)
                )
                table = pa.Table.from_pandas(
                    raw[["trade_date", "instrument_id", "adj_close"]], preserve_index=False
                )
                if price_writer is None:
                    price_writer = pq.ParquetWriter(output / "prices.parquet", table.schema)
                price_writer.write_table(table)
        if seen_context != len(context) or not seen_context:
            raise ValueError(
                "PIT context keys not fully covered exactly once by feature partitions"
            )
    finally:
        for writer in (feature_writer, price_writer):
            if writer is not None:
                writer.close()
    if sha256(pit_context_path) != context_hash:
        raise ValueError("PIT context changed during export")
    write_json(output / "calendar.json", inventory["sessions"])
    write_json(output / "feature_names.json", names)
    return seal_bundle(
        output,
        provenance={
            "source": "sealed_alpha158_history",
            "history_plan": plan["fingerprint"],
            "history_inventory": inventory["fingerprint"],
            "pit_context_sha256": context_hash,
            "pit_context_authority": "caller_supplied_not_automatically_certified",
        },
    )
