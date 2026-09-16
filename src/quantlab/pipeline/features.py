"""Single Canonical -> native Alpha158 implementation for research and daily use."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from quantlab.pipeline.ingestion import calendar_days, verify_session
from quantlab.research.alpha158_native import (
    apply_evidence_mask,
    evaluate_native,
    load_contract,
    map_inputs,
    write_binary_cache,
)
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.data import validate_features
from quantlab.research.ml.io import research_output, seal_bundle, sha256, write_json
from quantlab.research.ml.pit import validate_lineage

KEYS = ["trade_date", "instrument_id"]


def native_features(raw, sessions, securities, contract, scratch):
    instruments = sorted(raw.instrument_id.unique())
    identity = {s.instrument_id: s for s in securities}
    mapped, _ = map_inputs(
        raw,
        sessions,
        instruments,
        {k: identity[k].list_date for k in instruments},
        {k: identity[k].delist_date for k in instruments},
    )
    cache = Path(scratch) / "qlib"
    write_binary_cache(mapped, cache, sessions)
    native = evaluate_native(mapped, cache, contract)
    return apply_evidence_mask(native, mapped, contract)[0]


def _build_chunk(
    storage,
    receipts,
    context_path,
    availability_path,
    output,
    start,
    end,
    config,
    *,
    root,
    engine=native_features,
    forward=False,
):
    """Context: dated universe/industry. Availability: independently evidenced source times.

    History cannot infer vendor publication from the timestamp of a later download.
    `known_at` is supplied explicitly and stays marked unverified. Forward inputs
    additionally use the actual local ingestion observation as a lower bound.
    """
    output = research_output(Path(output))
    contract = load_contract(root)
    names = [f["name"] for f in contract["features"]]
    lookback = max(f["lookback_sessions"] for f in contract["features"])
    if any(f["future_sessions"] for f in contract["features"]):
        raise ValueError("feature contract contains future dependencies")
    context_path, availability_path = Path(context_path), Path(availability_path)
    bindings = {
        str(p): sha256(p)
        for p in (context_path, availability_path, storage.calendar_path, storage.securities_path)
    }
    import pyarrow.dataset as ds

    from quantlab.research.ml.panel import read_range

    source_context = ds.dataset(context_path, format="parquet")
    price_universe = set(
        source_context.to_table(columns=["instrument_id"]).column(0).unique().to_pylist()
    )
    context = read_range(context_path, start, end, max_bytes=config.max_matrix_bytes)
    required = {*KEYS, "eligible", "industry", "known_at", "source_id", "revision_id"}
    if set(context) != required or context.duplicated(KEYS).any():
        raise ValueError("PIT context requires unique keys and exact documented columns")
    context["trade_date"] = pd.to_datetime(context.trade_date)
    context = context.loc[context.trade_date.between(pd.Timestamp(start), pd.Timestamp(end))].copy()
    if context.empty:
        raise ValueError("no PIT context in requested feature interval")
    availability = pd.read_parquet(availability_path)
    if set(availability) != {"trade_date", "known_at", "source_id", "revision_id"}:
        raise ValueError("availability requires trade_date/known_at/source_id/revision_id")
    availability["trade_date"] = pd.to_datetime(availability.trade_date)
    if availability.duplicated("trade_date").any():
        raise ValueError("ambiguous source availability")
    for frame in (context, availability):
        if frame[["source_id", "revision_id", "known_at"]].isna().any().any():
            raise ValueError("unknown source provenance")
        if not frame.known_at.map(lambda v: pd.Timestamp(v).tzinfo is not None).all():
            raise ValueError("source known_at must include timezone")
        frame["known_at"] = pd.to_datetime(frame.known_at, utc=True)
    close_time = availability.trade_date.dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
    if (availability.known_at < close_time).any():
        raise ValueError("completed daily bar cannot be known before market close")
    calendar = storage.load_trading_calendar()
    first = min(c.trade_date for c in calendar)
    last = max(c.trade_date for c in calendar)
    days = calendar_days(calendar, first, last)
    selected = [d for d in days if start <= d <= end]
    if set(context.trade_date.dt.date) != set(selected):
        raise ValueError("PIT context must explicitly cover every requested open session")
    if not selected or days.index(selected[0]) < lookback:
        raise ValueError(f"need {lookback} prior calendar sessions for shared features")
    first_index = days.index(selected[0]) - lookback
    raw_days = days[first_index : days.index(selected[-1]) + 1]
    universe = set(context.instrument_id)
    estimated = len(universe) * len(raw_days) * (len(names) + 20) * 8 * 6
    if estimated > config.max_matrix_bytes:
        raise MemoryError("feature batch exceeds memory budget; narrow universe or raise budget")
    rows, receipts_by_day = [], {}
    for day in raw_days:
        receipt = verify_session(storage, receipts, day)
        receipts_by_day[day] = receipt
        bindings[str(Path(receipts) / "sessions" / f"{day}.json")] = sha256(
            Path(receipts) / "sessions" / f"{day}.json"
        )
        factors = {x.instrument_id: x.adj_factor for x in storage.load_adj_factors_by_date(day)}
        for bar in storage.load_daily_bars_by_date(day):
            if bar.instrument_id in price_universe:
                rows.append({**asdict(bar), "adj_factor": factors.get(bar.instrument_id)})
    raw = pd.DataFrame(rows)
    if raw.empty or not universe.issubset(set(raw.instrument_id)):
        raise ValueError("context instrument has no source history")
    raw["trade_date"] = pd.to_datetime(raw.trade_date)
    prices = raw[[*KEYS]].copy()
    prices["adj_close"] = raw.close * raw.adj_factor
    # Availability propagates through the full trailing feature dependency window.
    source = availability.set_index("trade_date").reindex(pd.DatetimeIndex(raw_days))
    if source.isna().any().any():
        raise ValueError("missing publication evidence for feature lookback sessions")
    if forward:
        observed = pd.Series(
            [pd.Timestamp(receipts_by_day[d]["observed_at"]) for d in raw_days], index=source.index
        )
        source["known_at"] = pd.concat([source.known_at, observed], axis=1).max(axis=1)
    source["available_at"] = pd.to_datetime(
        [
            max(source.known_at.iloc[i - lookback : i + 1]) if i >= lookback else pd.NaT
            for i in range(len(source))
        ],
        utc=True,
    )
    with exclusive_job(output.parent):
        if output.exists():
            raise FileExistsError("bundle already exists; verify/reuse or select a new snapshot")
        with TemporaryDirectory(dir=output.parent) as scratch:
            features = engine(
                raw.loc[raw.instrument_id.isin(universe)],
                pd.DatetimeIndex(raw_days),
                storage.load_securities(),
                contract,
                scratch,
            )
            features = features.merge(context, on=KEYS, how="right", validate="one_to_one")
            features = features.sort_values(KEYS).reset_index(drop=True).copy()
            vendor = features.trade_date.map(source.available_at)
            features["feature_available_at"] = pd.concat([vendor, features.known_at], axis=1).max(
                axis=1
            )
            features = validate_features(
                features, names, pd.DatetimeIndex(days), config, allow_late=forward
            )
            lineage = []
            for source_id, known in (
                ("canonical_prices", vendor),
                ("pit_context", features.known_at),
            ):
                part = features[KEYS].copy()
                part["source_id"], part["known_at"] = source_id, known
                part["effective_at"] = part.trade_date.dt.tz_localize("Asia/Shanghai")
                part["revision_id"] = sha256(
                    availability_path if source_id == "canonical_prices" else context_path
                )
                lineage.append(part)
            lineage = pd.concat(lineage, ignore_index=True)
            dependencies = {name: ["canonical_prices", "pit_context"] for name in names}
            validate_lineage(
                features,
                lineage,
                dependencies,
                decision_hour=config.decision_hour,
                allow_late=forward,
            )
            feature_contract = {
                "schema": "canonical_alpha158_v1",
                "native_contract": contract,
                "feature_dependencies": dependencies,
                "units": {"prices": "CNY", "volume": "shares", "amount": "CNY"},
                "adjustment": "OHLC*factor; volume/factor; no price fill",
            }
            # Check sources again before publishing any ready bundle.
            for path, digest in bindings.items():
                if sha256(path) != digest:
                    raise ValueError("feature source changed while building")
            for day in raw_days:
                verify_session(storage, receipts, day)
            stage = Path(scratch) / "bundle"
            stage.mkdir()
            features[[*KEYS, "eligible", "industry", "feature_available_at", *names]].to_parquet(
                stage / "features.parquet", index=False
            )
            prices.to_parquet(stage / "prices.parquet", index=False)
            lineage.to_parquet(stage / "pit_lineage.parquet", index=False)
            write_json(stage / "feature_names.json", names)
            write_json(stage / "calendar.json", [str(d) for d in days])
            write_json(stage / "feature_contract.json", feature_contract)
            seal_bundle(
                stage,
                provenance={
                    "source": "canonical_shared_alpha158",
                    "inputs": bindings,
                    "forward_observation_required": forward,
                    "historical_publication_certified": False,
                },
            )
            stage.rename(output)
    return {
        "status": "complete",
        "rows": len(features),
        "features": len(names),
        "output": str(output),
    }


def build_bundle(
    storage,
    receipts,
    context_path,
    availability_path,
    output,
    start,
    end,
    config,
    *,
    root,
    engine=native_features,
    forward=False,
    batch_sessions=63,
):
    """Stream bounded feature batches into one immutable bundle (shared daily engine)."""
    if type(batch_sessions) is not int or not 1 <= batch_sessions <= 252:
        raise ValueError("invalid feature batch size")
    calendar = storage.load_trading_calendar()
    days = calendar_days(calendar, start, end)
    if len(days) <= batch_sessions:
        return _build_chunk(
            storage,
            receipts,
            context_path,
            availability_path,
            output,
            start,
            end,
            config,
            root=root,
            engine=engine,
            forward=forward,
        )
    import shutil

    import pyarrow as pa
    import pyarrow.parquet as pq

    output = research_output(Path(output))
    paths = (
        Path(context_path),
        Path(availability_path),
        storage.calendar_path,
        storage.securities_path,
    )
    bindings = {str(p): sha256(p) for p in paths}
    writers, count = {}, 0
    with exclusive_job(output.parent):
        if output.exists():
            raise FileExistsError("bundle already exists; select a new snapshot")
        with TemporaryDirectory(dir=output.parent) as temp:
            temp = Path(temp)
            stage = temp / "bundle"
            stage.mkdir()
            all_sources = dict(bindings)
            try:
                for number, offset in enumerate(range(0, len(days), batch_sessions)):
                    chunk = temp / f"chunk-{number}"
                    last = min(offset + batch_sessions - 1, len(days) - 1)
                    _build_chunk(
                        storage,
                        receipts,
                        context_path,
                        availability_path,
                        chunk,
                        days[offset],
                        days[last],
                        config,
                        root=root,
                        engine=engine,
                        forward=forward,
                    )
                    manifest = json.loads((chunk / "manifest.json").read_text())
                    all_sources.update(manifest["provenance"]["inputs"])
                    for name in ("features.parquet", "prices.parquet", "pit_lineage.parquet"):
                        frame = pd.read_parquet(chunk / name)
                        if name == "prices.parquet" and number:
                            frame = frame.loc[
                                pd.to_datetime(frame.trade_date) >= pd.Timestamp(days[offset])
                            ]
                        if name == "features.parquet":
                            count += len(frame)
                        table = pa.Table.from_pandas(frame, preserve_index=False)
                        if name not in writers:
                            writers[name] = pq.ParquetWriter(stage / name, table.schema)
                        writers[name].write_table(table)
                    for name in ("calendar.json", "feature_names.json", "feature_contract.json"):
                        if number == 0:
                            shutil.copyfile(chunk / name, stage / name)
                        elif sha256(chunk / name) != sha256(stage / name):
                            raise ValueError(
                                "shared feature contract/calendar changed between batches"
                            )
                    shutil.rmtree(chunk)
            finally:
                for writer in writers.values():
                    writer.close()
            for path, digest in all_sources.items():
                if sha256(path) != digest:
                    raise ValueError("source changed during batched feature preparation")
            # Verify Canonical again, rather than only the receipt files themselves.
            for path in all_sources:
                if Path(path).parent.name == "sessions":
                    verify_session(storage, receipts, pd.Timestamp(Path(path).stem).date())
            seal_bundle(
                stage,
                provenance={
                    "source": "canonical_shared_alpha158",
                    "inputs": all_sources,
                    "forward_observation_required": forward,
                    "batch_sessions": batch_sessions,
                    "historical_publication_certified": False,
                },
            )
            stage.rename(output)
    return {"status": "complete", "rows": count, "output": str(output)}
