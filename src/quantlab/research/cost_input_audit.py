"""Input readiness over saved predictions; prices and events cannot choose targets."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_protocol import OUTPUT as ROLLING
from quantlab.research.cost_input_corporate import prepare_events, window_events
from quantlab.research.costs import estimate_research_order_components
from quantlab.research.signal_portfolio_audit import score_targets

KEYS = ["instrument_id", "trade_date"]


def identities(frame, sessions, instruments):
    if frame[KEYS].isna().any().any() or frame.duplicated(KEYS).any():
        raise DataValidationError("missing or duplicate audit keys")
    dates = pd.to_datetime(frame.trade_date)
    if dates.isna().any() or not dates.eq(dates.dt.normalize()).all():
        raise DataValidationError("invalid score/metadata session")
    rows = dates.map(sessions)
    cols = frame.instrument_id.map(instruments)
    if rows.isna().any() or cols.isna().any():
        raise DataValidationError("score/metadata outside the pinned population")
    return rows.to_numpy(dtype=int), cols.to_numpy(dtype=int)


def metadata_grid(root, metadata, config, sessions, instruments):
    grid = np.zeros((len(sessions), len(instruments)), dtype=np.uint8)
    date_index = {pd.Timestamp(d): i for i, d in enumerate(sessions)}
    code_index = {code: i for i, code in enumerate(instruments)}
    for name in metadata["artifacts"]:
        frame = pd.read_parquet(
            root / ROLLING / name, columns=config["metadata_columns"], use_threads=False
        )
        frame = frame.loc[frame.trade_date.between(config["start"], config["end"])]
        rows, cols = identities(frame, date_index, code_index)
        if (
            grid[rows, cols].any()
            or frame.complete_features.isna().any()
            or not frame.complete_features.isin([True, False]).all()
        ):
            raise DataValidationError("duplicate metadata or unknown completeness")
        grid[rows, cols] = 1 + frame.complete_features.to_numpy(dtype=np.uint8)
    return grid


def validate_score_frame(frame, grid, seen, date_index, code_index):
    rows, cols = identities(frame, date_index, code_index)
    if (
        seen[rows, cols].any()
        or not np.isfinite(frame.score).all()
        or not frame.complete_features.eq(True).all()
        or not (grid[rows, cols] == 2).all()
    ):
        raise DataValidationError("score duplication, invalid value or feature membership mismatch")
    seen[rows, cols] = True


def collect_targets(root, config, manifest, grid, instruments):
    sessions = manifest["sessions"]
    date_index = {pd.Timestamp(d): i for i, d in enumerate(sessions)}
    code_index = {code: i for i, code in enumerate(instruments)}
    targets, daily = [], []
    for model in config["models"]:
        seen = np.zeros_like(grid, dtype=bool)
        total = 0
        for source in (s for s in manifest["scores"] if s["model"] == model):
            # Label fields are physically excluded, including the incomplete future tail.
            frame = pd.read_parquet(
                root / source["path"], columns=config["score_columns"], use_threads=False
            )
            validate_score_frame(frame, grid, seen, date_index, code_index)
            total += len(frame)
            for stamp, cross in frame.groupby("trade_date", sort=True):
                target = score_targets(cross, "score", config)
                daily.append(
                    {
                        "model": model,
                        "trade_date": stamp,
                        "score_rows": len(cross),
                        "targets": len(target.positions),
                        "cash_weight": target.cash_weight,
                    }
                )
                for p in target.positions:
                    targets.append(
                        {
                            "model": model,
                            "trade_date": stamp,
                            "instrument_id": p.instrument_id,
                            "target_weight": p.target_weight,
                            "cash_weight": target.cash_weight,
                        }
                    )
        if total != config["score_rows_per_model"] or not np.array_equal(seen, grid == 2):
            raise DataValidationError("saved predictions do not exactly cover complete metadata")
    result = pd.DataFrame(targets).sort_values(["model", "trade_date", "instrument_id"])
    days = pd.DataFrame(daily).sort_values(["model", "trade_date"])
    if days.duplicated(["model", "trade_date"]).any() or len(days) != len(sessions) * len(
        config["models"]
    ):
        raise DataValidationError("model calendar coverage differs from contract")
    return result.reset_index(drop=True), days.reset_index(drop=True)


def shifted(sessions, position, offset):
    index = position + offset
    return sessions[index] if index < len(sessions) else None


def market_partition(frame, ids, day, kind):
    """Invalid or ambiguous rows become unavailable, without altering target selection."""
    index = pd.Index(sorted(ids), name="instrument_id")
    result = pd.DataFrame(index=index)
    result["present"] = False
    result["valid"] = False
    result["value"] = np.nan
    stats = {"rows": 0, "duplicate_key_rows": 0, "invalid_key_rows": 0, "invalid_value_rows": 0}
    if frame is None:
        return result, {**stats, "partition_present": False}
    required = [*KEYS, *(["open", "high", "low", "close"] if kind == "daily" else ["adj_factor"])]
    if not set(required).issubset(frame):
        return result, {
            **stats,
            "partition_present": True,
            "missing_columns": sorted(set(required) - set(frame)),
        }
    duplicates = frame.duplicated(KEYS, keep=False)
    invalid_keys = frame[KEYS].isna().any(axis=1) | ~pd.to_datetime(
        frame.trade_date, errors="coerce"
    ).eq(day)
    good = frame.loc[~duplicates & ~invalid_keys].set_index("instrument_id")
    numeric = good[required[2:]].apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(numeric).all(axis=1) & numeric.gt(0).all(axis=1)
    if kind == "daily":
        valid &= numeric.high.ge(numeric[["open", "low", "close"]].max(axis=1))
        valid &= numeric.low.le(numeric[["open", "high", "close"]].min(axis=1))
    values = numeric["close" if kind == "daily" else "adj_factor"]
    result["present"] = index.isin(frame.instrument_id.dropna())
    result["valid"] = valid.reindex(index, fill_value=False)
    result["value"] = values.where(valid).reindex(index)
    return result, {
        "partition_present": True,
        "rows": len(frame),
        "duplicate_key_rows": int(duplicates.sum()),
        "invalid_key_rows": int(invalid_keys.sum()),
        "invalid_value_rows": int((~valid).sum()),
    }


def inspect_market(root, targets, config, manifest, progress=print):
    sessions = list(pd.to_datetime(manifest["sessions"]))
    positions = {d: i for i, d in enumerate(sessions)}
    needs = defaultdict(set)
    for row in targets.itertuples():
        start = positions[row.trade_date]
        for day in sessions[start : min(len(sessions), start + max(config["horizons"]) + 2)]:
            needs[day].add(row.instrument_id)
    collected, profiles = [], []
    for number, day in enumerate(sessions):
        ids = needs[day]
        combined = pd.DataFrame(index=pd.Index(sorted(ids), name="instrument_id"))
        for kind in ("daily", "adj_factor"):
            path = manifest["groups"][kind][number]
            frame = (
                pd.read_parquet(root / path, use_threads=False)
                if path in manifest["files"]
                else None
            )
            result, stats = market_partition(frame, ids, day, kind)
            for column in result:
                combined[kind + "_" + column] = result[column]
            profiles.append({"dataset": kind, "trade_date": str(day.date()), "path": path, **stats})
        combined["trade_date"] = day
        collected.append(combined.reset_index())
        if (number + 1) % 200 == 0:
            progress(f"market input partitions: {number + 1}/{len(sessions)}", flush=True)
    return pd.concat(collected, ignore_index=True), profiles


def audit_windows(targets, market, events, config, sessions):
    sessions = list(pd.to_datetime(sessions))
    positions = {d: i for i, d in enumerate(sessions)}
    lookup = {(r.trade_date, r.instrument_id): r for r in market.itertuples()}
    by_code = {key: data for key, data in events.groupby("instrument_id")}
    empty = events.iloc[:0]
    rows = []
    for row in targets.itertuples():
        pos = positions[row.trade_date]
        entry = shifted(sessions, pos, 1)
        for horizon in config["horizons"]:
            exit_day = shifted(sessions, pos, horizon + 1)
            days = sessions[pos + 1 : min(len(sessions), pos + horizon + 2)]
            observations = [lookup[(d, row.instrument_id)] for d in days]
            valid_prices = sum(x.daily_valid for x in observations)
            valid_adjustments = sum(x.adj_factor_valid for x in observations)
            baseline = lookup[(row.trade_date, row.instrument_id)]
            adj = [baseline.adj_factor_value, *[x.adj_factor_value for x in observations]]
            changes = sum(
                np.isfinite(a) and np.isfinite(b) and a != b
                for a, b in zip(adj[:-1], adj[1:], strict=True)
            )
            first = observations[0] if observations else None
            last = observations[-1] if exit_day is not None else None
            corporate = window_events(
                by_code.get(row.instrument_id, empty),
                row.instrument_id,
                row.trade_date,
                entry,
                exit_day,
                sessions[-1],
            )
            rows.append(
                {
                    "model": row.model,
                    "trade_date": row.trade_date,
                    "instrument_id": row.instrument_id,
                    "horizon": horizon,
                    "entry_date": entry,
                    "exit_date": exit_day,
                    "entry_beyond_cutoff": entry is None,
                    "exit_beyond_cutoff": exit_day is None,
                    "expected_window_sessions": horizon + 1,
                    "observed_window_sessions": len(days),
                    "valid_price_sessions": valid_prices,
                    "valid_adjustment_sessions": valid_adjustments,
                    "entry_price_valid": bool(first and first.daily_valid),
                    "exit_price_valid": bool(last and last.daily_valid),
                    "adjacent_adjustment_changes": changes,
                    "complete_market_window": exit_day is not None
                    and valid_prices == horizon + 1
                    and valid_adjustments == horizon + 1,
                    **corporate,
                    "complete_cost_fen": None,
                    "execution_eligible": False,
                }
            )
    return pd.DataFrame(rows)


def fee_matrix(root, config, sessions):
    rows = []
    for day in sessions:
        for capital in config["hypothetical_capital_cny"]:
            notional = int(
                Decimal(capital * 100)
                * Decimal(str(config["target_contract"]["gross_exposure"]))
                / config["target_contract"]["max_names"]
            )
            for side in ("buy", "sell"):
                result = estimate_research_order_components(
                    [notional],
                    asset_type="stock",
                    side=side,
                    trade_date=pd.Timestamp(day).date(),
                    profile_path=root / config["cost_profile"],
                )
                rows.append(
                    {
                        "hypothetical_capital_cny": capital,
                        "hypothetical_notional_fen": notional,
                        **{
                            k: result[k]
                            for k in (
                                "trade_date",
                                "side",
                                "commission_fen",
                                "stamp_duty_fen",
                                "known_components_subtotal_fen",
                                "complete_trading_cost_fen",
                            )
                        },
                        "share_quantity": None,
                        "execution_authority": False,
                    }
                )
    return pd.DataFrame(rows)


def summarize(windows):
    output = []
    for (model, horizon), frame in windows.groupby(["model", "horizon"]):
        output.append(
            {
                "model": model,
                "horizon": int(horizon),
                "target_windows": len(frame),
                **{
                    key: int(frame[key].sum())
                    for key in (
                        "entry_beyond_cutoff",
                        "exit_beyond_cutoff",
                        "complete_market_window",
                        "candidate_event_rows",
                        "implemented_candidate_rows",
                        "candidate_rows_observed_by_signal_close",
                        "candidate_rows_with_quality_gaps",
                        "candidate_payments_after_intended_exit",
                    )
                },
                "windows_with_adjustment_change": int(
                    frame.adjacent_adjustment_changes.gt(0).sum()
                ),
                "target_rows_without_any_local_event": int((~frame.code_has_any_local_event).sum()),
                "windows_with_event_history_unknown": len(frame),
                "net_cost_ready": False,
            }
        )
    return output


def run_audit(root, out, config, manifest, inventory, rolling):
    instruments = sorted(inventory["instruments"])
    grid = metadata_grid(root, rolling["metadata"], config, manifest["sessions"], instruments)
    targets, daily = collect_targets(root, config, manifest, grid, instruments)
    targets.to_parquet(out / "targets.parquet", index=False)
    daily.to_parquet(out / "score_daily.parquet", index=False)
    # Selection is now complete. No subsequent price/event result is fed back into targets.
    print(
        f"frozen intentions: {len(targets)} from {int(daily.score_rows.sum())} scores", flush=True
    )
    frames = [
        pd.read_parquet(root / name, use_threads=False) for name in manifest["groups"]["dividend"]
    ]
    events, corporate = prepare_events(
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    )
    events.to_parquet(out / "corporate_observations.parquet", index=False)
    market, market_profile = inspect_market(root, targets, config, manifest)
    market.to_parquet(out / "market_observations.parquet", index=False)
    windows = audit_windows(targets, market, events, config, manifest["sessions"])
    windows.to_parquet(out / "target_windows.parquet", index=False)
    fees = fee_matrix(root, config, manifest["sessions"])
    fees.to_parquet(out / "fee_components.parquet", index=False)
    seen = grid != 0
    daily_meta = pd.DataFrame(
        {
            "trade_date": manifest["sessions"],
            "metadata_rows": seen.sum(axis=1),
            "complete_features": (grid == 2).sum(axis=1),
            "incomplete_features": (grid == 1).sum(axis=1),
            "not_in_metadata_grid": (grid == 0).sum(axis=1),
        }
    )
    daily_meta.to_parquet(out / "metadata_daily.parquet", index=False)
    codes = sorted(targets.instrument_id.unique())
    missing_codes = sorted(set(codes) - set(corporate["instrument_ids"]))
    acquisition = {
        "instrument_ids": codes,
        "instrument_count": len(codes),
        "codes_without_any_local_event": missing_codes,
        "required_window_start": str(windows.entry_date.min().date()),
        "required_window_end": config["end"],
        "date_tail_not_acquired_in_this_task": True,
        "needed_fields": [
            "ann_date",
            "imp_ann_date",
            "div_proc",
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
            "stk_div",
            "stk_bo_rate",
            "stk_co_rate",
            "cash_div",
            "cash_div_tax",
            "base_date",
            "base_share",
            "raw_payload",
            "request_parameters",
            "observed_at",
        ],
        "reason": "No complete event-history certificate exists, even for codes with some rows.",
        "execution_authority": False,
    }
    return {
        "score_rows": int(daily.score_rows.sum()),
        "target_rows": len(targets),
        "metadata_rows": int(seen.sum()),
        "metadata_complete": int((grid == 2).sum()),
        "metadata_incomplete": int((grid == 1).sum()),
        "summary": summarize(windows),
        "corporate_profile": corporate,
        "market_partition_profile": market_profile,
        "fee_matrix_rows": len(fees),
        "acquisition_request": acquisition,
        "examples": {
            name: frame.head(config["example_limit_per_failure"])
            .astype(object)
            .where(frame.head(config["example_limit_per_failure"]).notna(), None)
            .to_dict("records")
            for name, frame in {
                "unknown_tail": windows.loc[
                    windows.exit_beyond_cutoff, ["model", "instrument_id", "horizon"]
                ],
                "no_event_history": windows.loc[
                    ~windows.code_has_any_local_event, ["model", "instrument_id", "horizon"]
                ],
            }.items()
        },
        "new_fit_attempts": 0,
        "complete_cost": None,
        "net_cost_ready": False,
        "performance_evidence": False,
        "execution_authority": False,
        "fresh_forward_evidence": False,
    }
