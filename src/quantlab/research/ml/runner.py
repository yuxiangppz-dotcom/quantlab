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
from quantlab.research.ml.data import calendar_index
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


def run_training(
    bundle: Path, config_path: Path, output: Path, start, end, *, root: Path, resume=False
):
    from uuid import uuid4

    from quantlab.research.alpha158_store import exclusive_job
    from quantlab.research.ml.artifacts import complete, fingerprint, verify_completed, write_frame
    from quantlab.research.ml.data import monthly_folds
    from quantlab.research.ml.panel import fold_panel

    research_output(output)
    config = load_config(config_path)
    manifest = verify_bundle(bundle)
    identity = code_identity(root)
    if output.exists() and not resume:
        raise FileExistsError(output)
    binding = {
        "schema": "quantlab_ml_run_v2",
        "config": config.payload(),
        "config_fingerprint": config.fingerprint,
        "inputs": manifest,
        "input_manifest_sha256": sha256(bundle / "manifest.json"),
        "code": identity,
        "start": str(pd.Timestamp(start).date()),
        "end": str(pd.Timestamp(end).date()),
        "knowledge_basis": "retrospective_walk_forward_not_untouched_holdout",
        "performance_eligible": False,
    }
    # JSON roundtrip normalizes tuple/list differences in config.
    binding = json.loads(json.dumps(binding))
    with exclusive_job(output):
        if (output / "invalidated.json").exists():
            raise ValueError("run invalidated by input/code change; use a new output directory")
        intent_path = output / "intent.json"
        if intent_path.exists():
            intent = json.loads(intent_path.read_text())
            if {k: v for k, v in intent.items() if k != "created_at"} != binding:
                raise ValueError("resume code/config/input binding mismatch")
        else:
            write_json(intent_path, {**binding, "created_at": datetime.now(UTC).isoformat()})
        if (output / "completed.json").exists():
            verify_completed(output)
            return json.loads((output / "signal_summary.json").read_text())
        try:
            sessions = calendar_index(json.loads((bundle / "calendar.json").read_text()))
            names = json.loads((bundle / "feature_names.json").read_text())
            all_scores, all_fits = [], []
            for fold in monthly_folds(sessions, start, end, config):
                sealed = output / "models" / fold.name
                if sealed.exists():
                    verify_completed(sealed)
                    if json.loads((sealed / "fold_binding.json").read_text())[
                        "run_binding"
                    ] != fingerprint(binding):
                        raise ValueError("sealed fold belongs to a different run")
                else:
                    if verify_bundle(bundle) != manifest or code_identity(root) != identity:
                        raise ValueError("input/code/runtime changed before fold")
                    frame = fold_panel(bundle, fold, names, sessions, config, label_cutoff=end)
                    if verify_bundle(bundle) != manifest:
                        raise ValueError("input changed while reading fold")
                    # An interrupted work directory is kept for diagnosis, never reused as a model.
                    work = output / ".work" / uuid4().hex
                    scores, fits = walk_forward(
                        frame, names, sessions, fold.test_start, fold.test_end, config, work
                    )
                    if verify_bundle(bundle) != manifest or code_identity(root) != identity:
                        raise ValueError("input/code/runtime changed during fold")
                    candidate = work / fold.name
                    write_frame(candidate / "scores.parquet", scores)
                    write_json(candidate / "fits.json", fits)
                    write_json(
                        candidate / "fold_binding.json", {"run_binding": fingerprint(binding)}
                    )
                    complete(candidate)
                    sealed.parent.mkdir(parents=True, exist_ok=True)
                    candidate.rename(sealed)
                    del frame
                all_scores.append(pd.read_parquet(sealed / "scores.parquet"))
                all_fits.extend(json.loads((sealed / "fits.json").read_text()))
            scores = pd.concat(all_scores, ignore_index=True)
            daily, summary = signal_diagnostics(scores, config.min_cross_section)
            write_frame(output / "scores.parquet", scores)
            write_frame(output / "signal_daily.parquet", daily)
            # Aggregates can already exist after an interrupted final publication.
            for name, payload in (("fits.json", all_fits), ("signal_summary.json", summary)):
                path = output / name
                if path.exists():
                    if json.loads(path.read_text()) != json.loads(json.dumps(payload)):
                        raise ValueError("existing aggregate differs from sealed folds")
                else:
                    write_json(path, payload)
            if verify_bundle(bundle) != manifest or code_identity(root) != identity:
                raise ValueError("input/code/runtime changed during run")
            complete(output)
            return summary
        except Exception as exc:
            try:
                stable = verify_bundle(bundle) == manifest and code_identity(root) == identity
            except Exception:
                stable = False
            if not stable:
                write_json(output / "invalidated.json", {"reason": "input/code/runtime changed"})
            write_json(
                output / "failures" / f"{uuid4().hex}.json",
                {"type": type(exc).__name__, "reason": str(exc)},
            )
            raise


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
    corporate_actions=(),
    resume=False,
    binding_extra=None,
    final_check=None,
    strategy_mode="backtest",
):
    """Independently accounted scenarios with input-bound daily checkpoints."""
    from uuid import uuid4

    from quantlab.research.alpha158_store import exclusive_job
    from quantlab.research.ml.artifacts import (
        checkpoint_write,
        complete,
        fingerprint,
        frame_fingerprint,
        verify_completed,
        write_frame,
    )

    research_output(output)
    if not capitals_fen or len(set(capitals_fen)) != len(capitals_fen):
        raise ValueError("capital scenarios must be nonempty and unique")
    if any(type(c) is not int or c <= 0 for c in capitals_fen):
        raise ValueError("capital scenarios must be positive integer fen")
    if strategy_mode not in {
        "backtest",
        "archived_forward_signals",
        "eligible_equal_weight",
        "simple_factor",
    }:
        raise ValueError("unknown scenario strategy mode")
    expected_models = (
        {"shadow"}
        if strategy_mode == "archived_forward_signals"
        else {"eligible_equal_weight"}
        if strategy_mode == "eligible_equal_weight"
        else {"momentum_20d"}
        if strategy_mode == "simple_factor"
        else set(config.models)
    )
    if scores.empty or set(scores.model) != expected_models:
        raise ValueError("score models differ from the frozen configuration")
    inputs = {
        "strategy_mode": strategy_mode,
        "scores": frame_fingerprint(scores),
        "universe": frame_fingerprint(universe),
        "calendar": fingerprint(calendar),
        "initial_marks": fingerprint(initial_marks),
        "market": (
            market_days.fingerprint
            if hasattr(market_days, "fingerprint")
            else fingerprint(market_days)
        ),
        "corporate": fingerprint(corporate_actions),
        "start": str(start),
        "end": str(end),
        "capitals_fen": capitals_fen,
        "config": config.payload(),
        "extra": binding_extra,
    }
    binding = fingerprint(inputs)
    if output.exists() and not resume:
        raise FileExistsError(output)
    with exclusive_job(output):
        if (output / "invalidated.json").exists():
            raise ValueError("replay invalidated by input/code change; use a new output directory")
        intent = output / "intent.json"
        if intent.exists():
            if json.loads(intent.read_text())["binding"] != binding:
                raise ValueError("replay resume input/code/config mismatch")
        else:
            write_json(
                intent,
                {
                    "binding": binding,
                    "inputs": json.loads(json.dumps(inputs)),
                    "performance_eligible": False,
                },
            )
        if (output / "completed.json").exists():
            verify_completed(output)
            return json.loads((output / "summary.json").read_text())
        try:
            summaries = []
            for capital in capitals_fen:
                for model, part in scores.groupby("model", sort=True):
                    folder = output / f"{model}-{capital}fen"
                    folder.mkdir(exist_ok=True)
                    if (folder / "completed.json").exists():
                        verify_completed(folder)
                        summaries.append(json.loads((folder / "summary.json").read_text()))
                        continue
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
                        corporate_actions=corporate_actions,
                        checkpoint_dir=folder / "sessions",
                        binding=binding,
                        policy_name=(
                            "eligible_equal_weight"
                            if strategy_mode == "eligible_equal_weight"
                            else "buffered_rank"
                        ),                    )
                    schedule = result.schedule
                    ledger, orders, positions = [], [], []
                    previous = capital
                    for record, decision in zip(schedule.records, result.decisions, strict=True):
                        trades = sum(a.transition.simulated_notional_fen for a in record.attempts)
                        slippage = 0
                        marks = (
                            {
                                m.instrument_id: m.price_fen
                                for m in market_days.get(record.session).marks
                            }
                            if hasattr(market_days, "get")
                            else {
                                m.instrument_id: m.price_fen
                                for day in market_days
                                if day.session == record.session
                                for m in day.marks
                            }
                        )
                        for attempt in record.attempts:
                            t = attempt.transition
                            slip = (
                                abs(t.modeled_price_fen - marks[attempt.order.instrument_id])
                                * t.simulated_quantity
                                if t.modeled_price_fen
                                else 0
                            )
                            slippage += slip
                            orders.append(
                                {
                                    "session": str(record.session),
                                    "order_id": attempt.order.order_id,
                                    "instrument_id": attempt.order.instrument_id,
                                    "side": attempt.order.side,
                                    "desired": attempt.order.desired_quantity,
                                    "simulated": t.simulated_quantity,
                                    "status": t.status,
                                    "reason": t.reason,
                                    "notional_fen": t.simulated_notional_fen,
                                    "modeled_price_fen": t.modeled_price_fen,
                                    "slippage_fen": slip,
                                    "commission_fen": t.commission_fen,
                                    "stamp_fen": t.stamp_fen,
                                    "additional_fee_fen": t.additional_fee_fen,
                                }
                            )
                        ledger.append(
                            {
                                "session": str(record.session),
                                "cash_fen": record.book.cash_fen,
                                "position_value_fen": record.position_value_fen,
                                "receivable_fen": decision["receivable_fen"],
                                "equity_fen": record.marked_equity_fen,
                                "daily_return": record.marked_equity_fen / previous - 1,
                                "one_way_turnover": trades / previous / 2,
                                "fees_fen": record.modeled_fees_fen,
                                "slippage_fen": slippage,
                                "gross_exposure": decision["realized_exposure"]["gross_exposure"],
                                "risk_breaches": len(decision["realized_exposure"]["breaches"]),
                            }
                        )
                        previous = record.marked_equity_fen
                        for lot in record.book.lots:
                            positions.append(
                                {
                                    "session": str(record.session),
                                    "lot_id": lot.lot_id,
                                    "instrument_id": lot.instrument_id,
                                    "quantity": lot.quantity,
                                    "acquired_on": str(lot.acquired_on),
                                    "sellable_on": str(lot.sellable_on),
                                    "raw_mark_fen": marks[lot.instrument_id],
                                }
                            )
                    write_frame(folder / "ledger.parquet", pd.DataFrame(ledger))
                    write_frame(folder / "attempts.parquet", pd.DataFrame(orders))
                    write_frame(folder / "positions.parquet", pd.DataFrame(positions))
                    _same_or_write(folder / "decisions.json", list(result.decisions))
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
                    _same_or_write(folder / "summary.json", summary)
                    terminal = folder / "terminal.json"
                    if not terminal.exists():
                        checkpoint_write(
                            terminal,
                            {"book": schedule.book, "corporate_state": result.corporate_state},
                        )
                    complete(folder)
                    summaries.append(summary)
            _same_or_write(output / "summary.json", summaries)
            if final_check is not None:
                try:
                    final_check()
                except Exception:
                    write_json(output / "invalidated.json", {"reason": "final input check failed"})
                    raise
            complete(output)
            return summaries
        except Exception as exc:
            if final_check is not None and not (output / "invalidated.json").exists():
                try:
                    final_check()
                except Exception:
                    write_json(output / "invalidated.json", {"reason": "input check after failure"})
            write_json(
                output / "failures" / f"{uuid4().hex}.json",
                {"type": type(exc).__name__, "reason": str(exc)},
            )
            raise


def _same_or_write(path, payload):
    normalized = json.loads(json.dumps(payload, allow_nan=False))
    if path.exists():
        if json.loads(path.read_text()) != normalized:
            raise ValueError(f"immutable aggregate differs:{path.name}")
    else:
        write_json(path, normalized)
