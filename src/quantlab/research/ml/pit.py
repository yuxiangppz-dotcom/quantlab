"""Bitemporal source selection and explicit lineage validation; no certification."""

import pandas as pd


def _aware(series, name):
    if not series.map(lambda x: pd.notna(x) and pd.Timestamp(x).tzinfo is not None).all():
        raise ValueError(f"{name} requires timezone-aware timestamps")
    return pd.to_datetime(series, utc=True, errors="raise")


def asof_facts(requests, facts, value_columns):
    """Latest effective period, latest revision KNOWN at each decision timestamp."""
    required = {"instrument_id", "effective_at", "known_at", "revision_id", "source_id"}
    if required - set(facts) or {"instrument_id", "decision_at"} - set(requests):
        raise ValueError("missing bitemporal identity")
    if set(value_columns) - set(facts) or set(value_columns) & required:
        raise ValueError("invalid fact value columns")
    source = facts.copy()
    source["known_at"] = _aware(source.known_at, "known_at")
    source["effective_at"] = _aware(source.effective_at, "effective_at")
    if source[list(required)].isna().any().any():
        raise ValueError("missing fact provenance")
    # Call one source at a time so unrelated sources cannot overwrite one another.
    if source.source_id.nunique() != 1:
        raise ValueError("select one source per asof join")
    if source.duplicated(["instrument_id", "effective_at", "known_at"]).any():
        raise ValueError("ambiguous source revisions")
    queries = requests.copy()
    queries["decision_at"] = _aware(queries.decision_at, "decision_at")
    rows = []
    for query in queries.to_dict("records"):
        candidates = source.loc[
            source.instrument_id.eq(query["instrument_id"])
            & source.known_at.le(query["decision_at"])
            & source.effective_at.le(query["decision_at"])
        ]
        row = dict(query)
        if candidates.empty:
            row.update(
                {key: None for key in [*value_columns, "source_known_at", "source_revision_id"]}
            )
        else:
            selected = candidates.sort_values(["effective_at", "known_at"]).iloc[-1]
            row.update({key: selected[key] for key in value_columns})
            row.update(source_known_at=selected.known_at, source_revision_id=selected.revision_id)
        rows.append(row)
    return pd.DataFrame(rows)


def validate_lineage(features, lineage, dependencies):
    """Each declared feature dependency must have a known source revision by cutoff.

    This validates the supplied receipts, not the truth of vendor publication times.
    Pass per-date partitions to bound memory on a full history.
    """
    keys = ["trade_date", "instrument_id"]
    required = {*keys, "source_id", "known_at", "effective_at", "revision_id"}
    if required - set(lineage) or not dependencies:
        raise ValueError("lineage and feature dependency contract required")
    source = lineage.copy()
    source["trade_date"] = pd.to_datetime(source.trade_date)
    if source[list(required)].isna().any().any():
        raise ValueError("unknown lineage field")
    if source.duplicated([*keys, "source_id"]).any():
        raise ValueError("ambiguous lineage dependency row")
    source["known_at"] = _aware(source.known_at, "known_at")
    source["effective_at"] = _aware(source.effective_at, "effective_at")
    panel = features[keys + ["feature_available_at"]].copy()
    panel["trade_date"] = pd.to_datetime(panel.trade_date)
    panel["feature_available_at"] = _aware(panel.feature_available_at, "feature_available_at")
    cutoff = panel.trade_date.dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
    if (panel.feature_available_at > cutoff).any():
        raise ValueError("feature receipt after decision cutoff")
    expected = set()
    for name, sources in dependencies.items():
        if (
            name not in features
            or not sources
            or not all(isinstance(x, str) and x for x in sources)
        ):
            raise ValueError("invalid feature dependency contract")
        expected.update(sources)
    for source_id in expected:
        joined = panel.merge(
            source.loc[source.source_id.eq(source_id)], on=keys, how="left", validate="one_to_one"
        )
        if joined.known_at.isna().any() or joined.effective_at.isna().any():
            raise ValueError(f"missing source dependency:{source_id}")
        if (
            (joined.known_at > joined.feature_available_at)
            | (joined.effective_at > joined.feature_available_at)
        ).any():
            raise ValueError(f"future source dependency:{source_id}")
    return {
        "status": "supplied_lineage_contract_valid",
        "rows": len(panel),
        "sources": sorted(expected),
        "historical_data_certified": False,
    }
