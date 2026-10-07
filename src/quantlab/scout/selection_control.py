"""Precommitted same-pool discovery-score control; price observations, never fills."""

from random import Random
from statistics import mean

from quantlab.scout.models import fingerprint, finite

VERSION = "same_deep_pool_score_top_k_h5_v1"


def freeze_control(rows, universe, candidates, coverage):
    by_code = {c["instrument_id"]: c for c in candidates}
    snapshot = []
    global_gaps = sorted(
        {
            c["source"]
            for c in coverage
            if c.get("status")
            in {"failed", "not_configured", "unavailable", "unsupported", "error"}
        }
    )
    for row in rows:
        code = row["instrument_id"]
        original = universe[code]
        score = original.score
        gaps = (by_code[code].get("source_summary") or {}).get("gaps", [])
        snapshot.append(
            {
                "instrument_id": code,
                "discovery_score": score if finite(score) else None,
                "ai_rank": row["rank"],
                "ai_status": row["final_status"],
                "primary_type": row["primary_type"],
                "recent_extension": "extended_5d_gt20pct"
                if finite(original.metrics.get("return_5d")) and original.metrics["return_5d"] > 0.2
                else "not_extended"
                if finite(original.metrics.get("return_5d"))
                else "unknown",
                "source_coverage": "missing"
                if gaps or global_gaps
                else "no_recorded_gap_not_exhaustive",
            }
        )
    valid = all(r["discovery_score"] is not None for r in snapshot)
    ordered = (
        sorted(snapshot, key=lambda r: (-r["discovery_score"], r["instrument_id"])) if valid else []
    )
    ranks = {r["instrument_id"]: i for i, r in enumerate(ordered, 1)}
    k = sum(r["ai_status"] in {"focus", "watch"} for r in snapshot)
    for row in snapshot:
        row["score_rank"] = ranks.get(row["instrument_id"])
        row["score_selected"] = valid and row["score_rank"] <= k
    value = {
        "version": VERSION,
        "status": "frozen" if valid else "discovery_score_incomplete",
        "k": k,
        "abstention": k == 0,
        "rows": sorted(snapshot, key=lambda r: r["instrument_id"]),
        "score_order": [r["instrument_id"] for r in ordered],
        "ai_order": [
            r["instrument_id"]
            for r in sorted(snapshot, key=lambda r: (r["ai_rank"] or 10**9, r["instrument_id"]))
        ],
        "common_benchmark": "equal_weight_same_deep_pool_adjusted_d_open_h5_close",
        "score_definition": (
            "existing mean cross-sectional percentile of return_1d/return_5d/"
            "amount_ratio_5d/breakout_20d"
        ),
        "tie_policy": "score descending, instrument_id ascending; ties are not distinct alpha",
        "coverage_gaps_at_freeze": global_gaps,
        "missing_policy": "complete frozen denominator; no replacement, no imputed zero",
        "observation_definition": "all_deep_rank_d_open_h_close_adjusted_v1",
        "claim_boundary": "price_observation_not_fill_or_net_return",
    }
    return {**value, "sha256": fingerprint(value)}


def observe_control(control, rows):
    """Same start, horizon, adjustment and benchmark, including abstention days."""
    if control.get("version") != VERSION:
        raise ValueError("Unknown same-pool control version")
    if control["sha256"] != fingerprint({k: v for k, v in control.items() if k != "sha256"}):
        raise ValueError("Same-pool control freeze hash mismatch")
    frozen = {r["instrument_id"]: r for r in control["rows"]}
    marks = {r["instrument_id"]: r for r in rows}
    if len(marks) != len(rows) or set(marks) != set(frozen):
        raise ValueError("Control observation does not match complete frozen pool")
    valid_pool = all(
        r.get("status") == "observed" and finite(r.get("adjusted_price_return")) for r in rows
    )
    benchmark = mean(r["adjusted_price_return"] for r in rows) if valid_pool and rows else None

    def group(ids):
        values = [marks[c] for c in ids]
        ready = bool(values) and all(
            r.get("status") == "observed" and finite(r.get("adjusted_price_return")) for r in values
        )
        average = mean(r["adjusted_price_return"] for r in values) if ready else None
        adverse_ready = ready and all(finite(r.get("adverse_daily_low_change")) for r in values)
        return {
            "ids": ids,
            "original_count": len(ids),
            "valid_count": sum(r.get("status") == "observed" for r in values),
            "mean_price_change": average,
            "relative_to_common_pool": average - benchmark
            if average is not None and benchmark is not None
            else None,
            "adverse_mean": mean(r["adverse_daily_low_change"] for r in values)
            if adverse_ready
            else None,
            "actual_execution": "unknown_daily_ohlc_is_not_fill",
        }

    groups = {
        "all_pool": group(sorted(frozen)),
        "ai_selected": group(
            sorted(c for c, r in frozen.items() if r["ai_status"] in {"focus", "watch"})
        ),
        "score_top_k": group(sorted(c for c, r in frozen.items() if r["score_selected"])),
    }
    for field, labels in (
        ("ai_status", ("focus", "watch", "unselected")),
        (
            "primary_type",
            ("event_update", "trend_continuation", "pullback_improvement", "insufficient_evidence"),
        ),
        ("recent_extension", ("extended_5d_gt20pct", "not_extended", "unknown")),
        ("source_coverage", ("missing", "no_recorded_gap_not_exhaustive")),
    ):
        for label in labels:
            groups[field + ":" + label] = group(
                sorted(c for c, r in frozen.items() if r[field] == label)
            )
    ai, score = groups["ai_selected"], groups["score_top_k"]
    paired = valid_pool and control["status"] == "frozen" and control["k"] > 0
    return {
        "version": VERSION,
        "k": control["k"],
        "abstention": control["abstention"],
        "pool_count": len(frozen),
        "common_pool_mean": benchmark,
        "groups": groups,
        "paired_complete": paired,
        "ai_minus_score_price_change": ai["mean_price_change"] - score["mean_price_change"]
        if paired
        else None,
        "ai_minus_score_adverse": ai["adverse_mean"] - score["adverse_mean"]
        if paired and ai["adverse_mean"] is not None and score["adverse_mean"] is not None
        else None,
        "state": "abstention"
        if control["abstention"]
        else "observed"
        if paired
        else "pending_or_incomplete",
    }


def summarize_control(dates):
    """Date-equal diagnostics within one frozen version; overlapping days form blocks."""
    ordered = sorted(dates, key=lambda d: d["target_day"])
    complete = [d for d in ordered if d["paired_complete"]]
    blocks = [ordered[i : i + 5] for i in range(0, len(ordered), 5)]
    # Incomplete dates remain in their blocks. Never pull winners across gaps.
    values = [
        [d["ai_minus_score_price_change"] for d in b]
        for b in blocks
        if len(b) == 5 and all(d["paired_complete"] for d in b)
    ]
    interval = None
    if len(values) >= 4:
        rng = Random(0)
        samples = sorted(mean(v for _ in values for v in rng.choice(values)) for _ in range(1000))
        interval = [samples[24], samples[974]]
    return {
        "signal_date_count": len(ordered),
        "abstention_dates": sum(d["abstention"] for d in ordered),
        "paired_complete_dates": len(complete),
        "pending_or_incomplete_dates": len(ordered) - len(complete),
        "mean_ai_minus_score_price_change": mean(d["ai_minus_score_price_change"] for d in complete)
        if complete
        else None,
        "h5_contiguous_signal_date_block_size": 5,
        "complete_block_count": len(values),
        "descriptive_block_bootstrap_interval95": interval,
        "uncertainty": "insufficient date blocks"
        if interval is None
        else "descriptive, dependent markets; not efficacy proof",
        "selection_accuracy": "pending_frozen_prospective_observations",
        "realizable_net_returns": "not_measured_no_execution_model",
    }
