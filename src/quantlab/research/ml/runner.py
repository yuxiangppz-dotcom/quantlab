"""One reproducible entry for panel admission, model fitting, diagnostics and replay."""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd

from quantlab.research.ml.config import load_config
from quantlab.research.ml.data import calendar_index, execution_labels, validate_features
from quantlab.research.ml.evaluation import scenario_metrics, signal_diagnostics
from quantlab.research.ml.io import research_output, sha256, verify_bundle, write_json
from quantlab.research.ml.replay import replay_scores
from quantlab.research.ml.training import walk_forward


def code_identity(root):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    if dirty:
        raise ValueError("research run requires clean committed code")
    packages = {}
    for name in ("numpy", "pandas", "pyarrow", "lightgbm"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"git_head": head, "python": platform.python_version(), "packages": packages}


def run_training(bundle: Path, config_path: Path, output: Path, start, end, *, root: Path):
    research_output(output)
    config = load_config(config_path)
    manifest = verify_bundle(bundle)
    identity = code_identity(root)
    # Unique output directory is both the experiment reservation and overwrite guard.
    output.mkdir(parents=True, exist_ok=False)
    intent = {
        "schema": "quantlab_ml_run_v2",
        "created_at": datetime.now(UTC).isoformat(),
        "config": config.payload(),
        "config_fingerprint": config.fingerprint,
        "inputs": manifest,
        "input_manifest_sha256": sha256(bundle / "manifest.json"),
        "code": identity,
        "start": str(start),
        "end": str(end),
        "knowledge_basis": "retrospective_walk_forward_not_untouched_holdout",
        "performance_eligible": False,
    }
    write_json(output / "intent.json", intent)
    try:
        sessions = calendar_index(json.loads((bundle / "calendar.json").read_text()))
        names = json.loads((bundle / "feature_names.json").read_text())
        # Refuse over-budget full-panel materialization before reading Parquet.
        import pyarrow.parquet as pq

        rows = pq.ParquetFile(bundle / "features.parquet").metadata.num_rows
        if rows * (len(names) + 12) * 8 * 6 > config.max_matrix_bytes:
            raise MemoryError(
                "input panel exceeds configured memory budget; stage a smaller universe"
            )
        features = validate_features(
            pd.read_parquet(bundle / "features.parquet"), names, sessions, config
        )
        labels = execution_labels(pd.read_parquet(bundle / "prices.parquet"), sessions, config)
        frame = features.merge(
            labels, on=["trade_date", "instrument_id"], how="left", validate="one_to_one"
        )
        scores, fits = walk_forward(frame, names, sessions, start, end, config, output / "models")
        daily, summary = signal_diagnostics(scores, config.min_cross_section)
        scores.to_parquet(output / "scores.parquet", index=False)
        daily.to_parquet(output / "signal_daily.parquet", index=False)
        write_json(output / "fits.json", fits)
        write_json(output / "signal_summary.json", summary)
        # Detect input changes during execution before publishing a complete run.
        if verify_bundle(bundle) != manifest:
            raise ValueError("input manifest changed during run")
        if code_identity(root) != identity:
            raise ValueError("code or runtime changed during run")
        artifacts = {
            p.relative_to(output).as_posix(): sha256(p)
            for p in sorted(output.rglob("*"))
            if p.is_file()
        }
        write_json(
            output / "completed.json",
            {"artifacts": artifacts, "fits": len(fits), "performance_eligible": False},
        )
    except Exception as exc:
        write_json(output / "failed.json", {"type": type(exc).__name__, "reason": str(exc)})
        raise
    return summary


def run_scenarios(
    scores,
    universe,
    calendar,
    market_days,
    initial_marks,
    *,
    start,
    end,
    capitals_fen,
    config,
    output,
):
    """Each capital/model gets its own share ledger; never scale a normalized NAV."""
    research_output(output)
    if not capitals_fen or len(set(capitals_fen)) != len(capitals_fen):
        raise ValueError("capital scenarios must be nonempty and unique")
    if any(type(c) is not int or c <= 0 for c in capitals_fen):
        raise ValueError("capital scenarios must be positive integer fen")
    if set(scores.model) - set(config.models):
        raise ValueError("score models differ from the frozen configuration")
    output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for capital in capitals_fen:
        for model, part in scores.groupby("model", sort=True):
            result = replay_scores(
                part,
                universe,
                calendar,
                market_days,
                initial_marks,
                start=start,
                end=end,
                initial_cash_fen=capital,
                config=config,
            )
            folder = output / f"{model}-{capital}fen"
            folder.mkdir()
            schedule = result.schedule
            ledger, orders = [], []
            previous = capital
            for record in schedule.records:
                trades = sum(a.transition.simulated_notional_fen for a in record.attempts)
                ledger.append(
                    {
                        "session": str(record.session),
                        "cash_fen": record.book.cash_fen,
                        "position_value_fen": record.position_value_fen,
                        "equity_fen": record.marked_equity_fen,
                        "daily_return": record.marked_equity_fen / previous - 1,
                        "one_way_turnover": trades / previous / 2,
                        "fees_fen": record.modeled_fees_fen,
                    }
                )
                previous = record.marked_equity_fen
                for attempt in record.attempts:
                    t = attempt.transition
                    orders.append(
                        {
                            "session": str(record.session),
                            "instrument_id": attempt.order.instrument_id,
                            "side": attempt.order.side,
                            "desired": attempt.order.desired_quantity,
                            "simulated": t.simulated_quantity,
                            "status": t.status,
                            "reason": t.reason,
                            "notional_fen": t.simulated_notional_fen,
                            "commission_fen": t.commission_fen,
                            "stamp_fen": t.stamp_fen,
                            "additional_fee_fen": t.additional_fee_fen,
                        }
                    )
            pd.DataFrame(ledger).to_parquet(folder / "ledger.parquet", index=False)
            pd.DataFrame(orders).to_parquet(folder / "attempts.parquet", index=False)
            write_json(folder / "decisions.json", list(result.decisions))
            summary = {
                "model": model,
                "capital_fen": capital,
                "status": schedule.status,
                "stopped_on": str(schedule.stopped_on) if schedule.stopped_on else None,
                "stop_reason": schedule.stop_reason,
                "valid_through": str(schedule.valid_through),
                "terminal_liquidation": False,
                **scenario_metrics(schedule.records, capital),
            }
            write_json(folder / "summary.json", summary)
            summaries.append(summary)
    write_json(output / "summary.json", summaries)
    return summaries
