"""Audit helpers for the lifecycle date-semantics experiment.

These are pure, testable helpers shared by the research backtest runner and the
test suite. They never mutate inputs, never read ``TUSHARE_TOKEN``, and never
claim a strategy actually held or was blocked by an instrument — they report
only observed data/configuration evidence.
"""

from __future__ import annotations

import hashlib

import pandas as pd


def consumer_matrix() -> list[dict]:
    """List every delist_date consumer and its boundary semantics."""
    return [
        {
            "file": "src/quantlab/backtest/lifecycle.py",
            "function": "is_instrument_invalid_on_delist_boundary",
            "comparison": "> (legacy) / >= (v1)",
            "semantics": "single source of truth for the delist boundary",
            "first_invalid_impact": "the only place the comparison is maintained",
            "modified_this_round": True,
        },
        {
            "file": "src/quantlab/backtest/lifecycle.py",
            "function": "LifecycleMonitor._delist_fired",
            "comparison": "delegates to is_instrument_invalid_on_delist_boundary",
            "semantics": "held-position lifecycle validity boundary",
            "first_invalid_impact": "blocks one session earlier under v1",
            "modified_this_round": True,
        },
        {
            "file": "src/quantlab/research/price.py",
            "function": "filter_point_in_time",
            "comparison": "price.trade_date > delist_date -> drop",
            "semantics": "inclusive: keeps price rows on delist_date",
            "first_invalid_impact": "would drop delist_date price rows",
            "modified_this_round": False,
        },
        {
            "file": "src/quantlab/research/dataset.py",
            "function": "_build_delist_dates",
            "comparison": "none (map construction)",
            "semantics": "builds delist_dates incl. code-change offsets",
            "first_invalid_impact": "none directly",
            "modified_this_round": False,
        },
        {
            "file": "scripts/run_research_backtest.py",
            "function": "_date_semantics_table",
            "comparison": "delegates to first_invalid_open_session",
            "semantics": "theoretical boundary table (not observed blocking)",
            "first_invalid_impact": "reporting only",
            "modified_this_round": True,
        },
    ]


def consumer_impact_audit(df, universe, alpha_df, targets, delist_map) -> dict:
    """Report observed delist_date impact at each research/alpha/target layer.

    Every count is measured strictly ON the raw ``delist_date`` for the same
    instrument: a price row, a universe row, an alpha row, or a target
    originating from a signal date equal to that instrument's ``delist_date``.
    An instrument appearing in the universe on *other* dates is not counted.
    """
    delist_df = pd.DataFrame(
        [{"instrument_id": i, "delist_date": d} for i, d in delist_map.items()]
    )

    def _rows_on_delist(frame: pd.DataFrame) -> pd.DataFrame:
        merged = frame[["instrument_id", "trade_date"]].merge(
            delist_df, on="instrument_id", how="inner"
        )
        return merged[merged["trade_date"] == merged["delist_date"]]

    price_on = _rows_on_delist(df)
    universe_on = _rows_on_delist(universe)
    alpha_on = _rows_on_delist(alpha_df)

    # targets whose signal date equals the instrument's raw delist_date
    target_occurrences = 0
    for sig, t in targets.items():
        for p in t.positions:
            if delist_map.get(p.instrument_id) == sig:
                target_occurrences += 1

    price_stocks = set(price_on["instrument_id"])
    first_date = min(delist_map[i] for i in price_stocks) if price_stocks else None
    first_stock = (
        min(price_stocks, key=lambda i: delist_map[i]) if price_stocks else None
    )

    return {
        "price_rows_on_raw_delist_date": int(len(price_on)),
        "universe_rows_on_raw_delist_date": int(len(universe_on)),
        "alpha_rows_on_raw_delist_date": int(len(alpha_on)),
        "target_occurrences_on_raw_delist_date": target_occurrences,
        "unique_stocks_with_price_on_raw_delist_date": len(price_stocks),
        "first_affected_date": first_date.isoformat() if first_date else None,
        "first_affected_stock": first_stock,
        "note": (
            "counts are per instrument ON its raw delist_date only; "
            "universe/alpha/target layers are subsets of the price layer. "
            "A stock in the universe on other dates is not delist-date impact."
        ),
    }


def _canonical_hash(obj) -> str:
    h = hashlib.sha256()

    def feed(x) -> None:
        if isinstance(x, pd.DataFrame):
            for col in sorted(x.columns):
                feed(col)
                arr = x[col].to_numpy()
                if arr.dtype == object:
                    for v in arr:
                        feed(v)
                else:
                    h.update(arr.tobytes())
        elif isinstance(x, (list, tuple)):
            for v in x:
                feed(v)
        elif isinstance(x, dict):
            for k in sorted(x, key=str):
                feed(k)
                feed(x[k])
        elif isinstance(x, bool):
            h.update(b"1" if x else b"0")
        else:
            h.update(str(x).encode("utf-8"))
            h.update(b"\x00")

    feed(obj)
    return h.hexdigest()


def fingerprint_frame(frame: pd.DataFrame) -> str:
    """Deterministic content fingerprint of a market-data frame."""
    return _canonical_hash(frame)


def fingerprint_security_master(
    securities: list,
    code_changes: list,
    mode: str,
) -> str:
    """Deterministic fingerprint of a LifecycleMonitor construction.

    Hashes exactly the fields ``LifecycleMonitor`` reads (instrument id,
    delist dates, code-change effective dates) plus the boundary mode, so a
    strategy/control symmetry audit can prove both portfolios ran on the
    same lifecycle inputs and semantics — not just the same mode string.
    """
    payload = {
        "mode": mode,
        "delist_dates": sorted(
            (s.instrument_id, s.delist_date.isoformat())
            for s in securities
            if s.delist_date is not None
        ),
        "code_changes": sorted(
            (c.old_instrument_id, c.new_instrument_id, c.effective_date.isoformat())
            for c in code_changes
        ),
    }
    return _canonical_hash(payload)


def code_change_lineage_audit(
    code_changes: list,
    securities: list,
    signal_dates: list,
    mode: str,
    price_frame: pd.DataFrame,
    universe_predicate=None,
) -> list[dict]:
    """Per-lineage PIT identity evidence for every formal signal date.

    For each code-change fact this reports:

    - old/new instrument id and the effective date;
    - on how many formal signal dates each identity was PIT-eligible;
    - ``future_successor_violation_count``: signal dates where the successor
      id was eligible STRICTLY BEFORE its effective date (vendor-backfilled
      successor history leaking into the past — must be 0);
    - ``old_new_overlap_count``: sessions carrying both identities of one
      lineage (double counting — must be 0);
    - ``eligible_but_unpriced_predecessor_count``: signal dates where the
      predecessor was eligible but had no price row (correct state: stays in
      the denominator, engine leaves the unfilled weight in cash).

    Eligibility comes from :func:`pit_eligible_instrument_ids` — the same
    function the control portfolio consumes — so the audit proves the actual
    production wiring, not a parallel re-implementation.
    """
    from quantlab.backtest.lifecycle import pit_eligible_instrument_ids

    eligible_by_date = {
        d: set(
            pit_eligible_instrument_ids(
                securities, code_changes, d, mode, universe_predicate
            )
        )
        for d in signal_dates
    }
    priced: set[tuple] = set()
    if price_frame is not None and not price_frame.empty:
        priced_frame = price_frame[["instrument_id", "trade_date"]].copy()
        priced_frame["trade_date"] = pd.to_datetime(priced_frame["trade_date"]).dt.date
        priced = set(
            zip(priced_frame["instrument_id"], priced_frame["trade_date"], strict=True)
        )
    master_ids = {s.instrument_id for s in securities}

    rows: list[dict] = []
    for change in sorted(code_changes, key=lambda c: c.effective_date):
        old_eligible = [
            d
            for d in signal_dates
            if change.old_instrument_id in eligible_by_date[d]
        ]
        new_eligible = [
            d
            for d in signal_dates
            if change.new_instrument_id in eligible_by_date[d]
        ]
        future_violations = [d for d in new_eligible if d < change.effective_date]
        overlaps = [
            d
            for d in signal_dates
            if change.old_instrument_id in eligible_by_date[d]
            and change.new_instrument_id in eligible_by_date[d]
        ]
        unpriced_predecessors = [
            d
            for d in old_eligible
            if (change.old_instrument_id, d) not in priced
        ]
        rows.append(
            {
                "old_instrument_id": change.old_instrument_id,
                "new_instrument_id": change.new_instrument_id,
                "effective_date": change.effective_date.isoformat(),
                "old_id_in_security_master": (
                    change.old_instrument_id in master_ids
                ),
                "old_eligible_signal_date_count": len(old_eligible),
                "new_eligible_signal_date_count": len(new_eligible),
                "future_successor_violation_count": len(future_violations),
                "old_new_overlap_count": len(overlaps),
                "eligible_but_unpriced_predecessor_count": (
                    len(unpriced_predecessors)
                ),
                "note": (
                    "successor id visible only from the effective date; the "
                    "backfilled original list_date is not a visibility fact; "
                    "old id absent from the master stays "
                    "eligible-but-unpriced until the day before the "
                    "effective date"
                ),
            }
        )
    return rows


def fingerprint_targets(targets: dict) -> str:
    """Deterministic content fingerprint of a target sequence."""
    canon: dict = {}
    for sig in sorted(targets):
        t = targets[sig]
        positions = sorted(
            (p.instrument_id, p.target_weight) for p in t.positions
        )
        canon[sig.isoformat()] = {
            "positions": positions,
            "cash_weight": t.cash_weight,
        }
    return _canonical_hash(canon)


def symmetry_audit(legacy_side: dict, v1_side: dict) -> dict:
    """Compare two lifecycle-mode run-input snapshots for symmetry.

    Each side carries ``data_fingerprint``, ``target_fingerprint``, ``config``
    (any comparable snapshot) and ``lifecycle_mode``. The result flags
    ``different_lifecycle_mode_only = True`` only when data, targets and config
    all match and only the mode differs.
    """
    same_data = legacy_side["data_fingerprint"] == v1_side["data_fingerprint"]
    same_target = legacy_side["target_fingerprint"] == v1_side["target_fingerprint"]
    same_config = legacy_side["config"] == v1_side["config"]
    diff_mode = legacy_side["lifecycle_mode"] != v1_side["lifecycle_mode"]
    return {
        "same_data_fingerprint": (
            legacy_side["data_fingerprint"] if same_data else "MISMATCH"
        ),
        "same_target_fingerprint": (
            legacy_side["target_fingerprint"] if same_target else "MISMATCH"
        ),
        "same_strategy_config": legacy_side["config"] if same_config else "MISMATCH",
        "different_lifecycle_mode_only": (
            same_data and same_target and same_config and diff_mode
        ),
    }
