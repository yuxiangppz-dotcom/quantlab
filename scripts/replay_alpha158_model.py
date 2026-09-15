"""Commissioned saved-model scenario. No training, canonical writes or live orders."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import uuid
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from quantlab.research.model_replay_accounting import (
    ReplayEvidenceError,
    ValuationMark,
    capture_claims,
    corporate_nav,
    open_corporate_day,
    settle_disposals,
    valuation_mark,
)
from quantlab.research.model_replay_data import ReplayData
from quantlab.research.model_replay_intake import nav_metrics
from quantlab.research.quantity_kernel import ResearchBook, ResearchOrder, simulate_research_order


def write_json(path, value):
    text = json.dumps(value, default=str, ensure_ascii=False, indent=2)
    pending = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    with pending.open("x") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.link(pending, path)  # Atomic, exclusive publish. Existing records cannot be replaced.
    pending.unlink()


def run(root, output, delisted_valuation="stop", exclude_st=False, fractional_policy="stop"):
    if delisted_valuation not in {"stop", "zero", "last"}:
        raise ValueError("unknown delisted valuation policy")
    if output.exists() or output.is_relative_to(root / "data/canonical"):
        raise ValueError("output must be a new directory outside canonical")
    output.mkdir(parents=True)
    write_json(
        output / "started.json",
        {
            "scope": "alpha158_commissioned_replay",
            "execution_authority": False,
            "delisted_valuation": delisted_valuation,
            "exclude_st": exclude_st,
            "fractional_policy": fractional_policy,
        },
    )
    data = ReplayData(root)
    code_root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/replay_alpha158_model.py",
        "src/quantlab/research/model_replay_accounting.py",
        "src/quantlab/research/model_replay_data.py",
        "src/quantlab/research/model_replay_intake.py",
        "src/quantlab/research/quantity_kernel.py",
    ):
        data.bind(code_root / relative)
    targets = data.targets(exclude_st=exclude_st)
    start, end = targets.trade_date.min().date(), targets.trade_date.max().date()
    data.load_events(set(targets.instrument_id), start, end)
    groups = {
        day.date(): list(group.sort_values("rank").itertuples())
        for day, group in targets.groupby("trade_date")
    }
    dates = [day for day in data.calendar if start <= day <= end]
    book = ResearchBook(start, 20000000)
    claims, marks, pending = [], {}, []
    records, all_attempts, all_corporate, all_marks = [], [], [], []
    stop = None
    (output / "days").mkdir()
    for index, day in enumerate(dates):
        prior_book, prior_claims, prior_marks = book, copy.deepcopy(claims), dict(marks)
        attempts, corporate, stale = [], [], []
        try:
            if index:
                book, corporate = open_corporate_day(book, claims, day, fractional_policy)
                for change in data.changes:
                    if str(day) == change["effective_date"] and any(
                        lot.instrument_id == change["old_instrument_id"] for lot in book.lots
                    ):
                        raise ReplayEvidenceError(f"held_code_change_requires_transfer:{change}")
                desired = {code: quantity for code, quantity, rank in pending}
                held = Counter()
                for lot in book.lots:
                    held[lot.instrument_id] += lot.quantity
                orders = []
                ranks = {code: rank for code, quantity, rank in pending}
                for code in sorted(set(held) | set(desired)):
                    difference = desired.get(code, 0) - held[code]
                    if difference:
                        orders.append((code, "buy" if difference > 0 else "sell", abs(difference)))
                orders.sort(key=lambda x: (x[1] == "buy", ranks.get(x[0], 999), x[0]))
                for code, side, quantity in orders:
                    security = data.securities.get(code)
                    if (
                        security
                        and security.delist_date
                        and day >= security.delist_date
                        and delisted_valuation != "stop"
                    ):
                        attempts.append(
                            {
                                "date": str(day),
                                "instrument_id": code,
                                "side": side,
                                "desired_quantity": quantity,
                                "quantity": 0,
                                "price_fen": None,
                                "notional_fen": 0,
                                "commission_fen": 0,
                                "stamp_fen": 0,
                                "dividend_tax_fen": 0,
                                "status": "blocked",
                                "reason": "delisted_no_execution",
                            }
                        )
                        continue
                    context = data.context(code, day, dates[index - 1])
                    order = ResearchOrder(
                        f"{day}:{side}:{code}", code, side, quantity, dates[index - 1]
                    )
                    before = book
                    result = simulate_research_order(book, order, context)
                    book = result.book
                    tax = 0
                    if side == "sell" and result.simulated_quantity:
                        book, tax = settle_disposals(before, book, claims, day)
                    attempts.append(
                        {
                            "date": str(day),
                            "instrument_id": code,
                            "side": side,
                            "desired_quantity": quantity,
                            "quantity": result.simulated_quantity,
                            "price_fen": result.modeled_price_fen,
                            "notional_fen": result.simulated_notional_fen,
                            "commission_fen": result.commission_fen,
                            "stamp_fen": result.stamp_fen,
                            "dividend_tax_fen": tax,
                            "status": result.status,
                            "reason": result.reason,
                        }
                    )
                book = replace(book, asof_date=day)
            held_codes = {lot.instrument_id for lot in book.lots}
            for code in held_codes:
                security = data.securities.get(code)
                if not (
                    security
                    and security.delist_date
                    and day >= security.delist_date
                    and delisted_valuation != "stop"
                ):
                    data.security(code, day)
                for row in data.unplaced.get(code, []):
                    if row["ex_date"] == str(day).replace("-", ""):
                        raise ReplayEvidenceError(
                            f"unknown_record_date_for_held_distribution:{code}:{day}"
                        )
            required = held_codes | {row.instrument_id for row in groups[day]}
            today = data.partition("daily", day)
            for code in sorted(required):
                security = data.securities.get(code)
                if (
                    security
                    and security.delist_date
                    and day >= security.delist_date
                    and delisted_valuation != "stop"
                ):
                    previous = marks.get(code)
                    if previous is None:
                        raise ReplayEvidenceError(f"no_pre_delist_mark:{code}")
                    mark = ValuationMark(
                        code,
                        day,
                        previous.observed_date,
                        0 if delisted_valuation == "zero" else previous.price_fen,
                        f"delisted_{delisted_valuation}_scenario_unknown_recovery",
                    )
                    stale.append(asdict(mark))
                    marks[code] = mark
                    continue
                raw = today.get(code)
                suspended = data.suspended(code, day)
                mark = valuation_mark(
                    code, day, None if raw is None else raw["close"], marks.get(code), suspended
                )
                if mark.method != "observed_raw_close":
                    if any(c.event.instrument_id == code and c.event.ex == day for c in claims):
                        raise ReplayEvidenceError(f"stale_price_across_ex_date:{code}:{day}")
                    stale.append(asdict(mark))
                marks[code] = mark
            position = sum(lot.quantity * marks[lot.instrument_id].price_fen for lot in book.lots)
            receivable, tax_reserve = corporate_nav(claims, day)
            equity = book.cash_fen + position + receivable - tax_reserve
            if equity <= 0:
                raise ReplayEvidenceError("nonpositive_equity")
            # Record-date eligibility is actual after-trade inventory, not target membership.
            events = [
                event for code in sorted(held_codes) for event in data.distributions(code, day)
            ]
            claims.extend(capture_claims(book, events))
            next_pending = []
            for row in groups[day]:
                # Integer floor of 5% of decision NAV / actual decision mark.
                if marks[row.instrument_id].price_fen <= 0:
                    raise ReplayEvidenceError("target_after_delisting")
                quantity = equity // (20 * marks[row.instrument_id].price_fen)
                next_pending.append((row.instrument_id, quantity, row.rank))
            record = {
                "date": str(day),
                "cash_fen": book.cash_fen,
                "position_fen": position,
                "dividend_receivable_fen": receivable,
                "dividend_tax_reserve_fen": tax_reserve,
                "equity_fen": equity,
                "gross_exposure": position / equity,
                "stale_positions": sum(m["instrument_id"] in held_codes for m in stale),
                "commission_fen": sum(a["commission_fen"] for a in attempts),
                "stamp_fen": sum(a["stamp_fen"] for a in attempts),
                "dividend_tax_fen": sum(a["dividend_tax_fen"] for a in attempts),
                "turnover_fen": sum(a["notional_fen"] for a in attempts),
            }
            write_json(
                output / "days" / f"{day}.json",
                {
                    "record": record,
                    "book": asdict(book),
                    "claims": [asdict(c) for c in claims],
                    "next_pending": next_pending,
                    "attempts": attempts,
                    "corporate": corporate,
                    "stale_marks": stale,
                },
            )
            pending = next_pending
            records.append(record)
            all_attempts.extend(attempts)
            all_corporate.extend({"date": str(day), **row} for row in corporate)
            all_marks.extend(stale)
            if index % 50 == 0:
                print(f"committed {day}, {len(records)} days", flush=True)
        except (ReplayEvidenceError, ValueError) as exc:
            book, claims, marks = prior_book, prior_claims, prior_marks
            stop = {"date": str(day), "reason": str(exc), "exception": type(exc).__name__}
            write_json(output / "stopped.json", stop)
            print(json.dumps(stop), flush=True)
            break
    frame = pd.DataFrame(records)
    frame.to_csv(output / "ledger.csv", index=False)
    pd.DataFrame(all_attempts).to_csv(output / "attempts.csv", index=False)
    pd.DataFrame(all_corporate).to_csv(output / "corporate.csv", index=False)
    pd.DataFrame(all_marks).to_csv(output / "stale_marks.csv", index=False)
    for path in list(data.manifest):
        data.bind(Path(path))
    write_json(output / "source_manifest.json", data.manifest)
    complete = stop is None and len(records) == len(dates)
    metrics = None
    if complete:
        metrics = nav_metrics(pd.Series(frame.equity_fen.values, index=pd.to_datetime(frame.date)))
    result = {
        "status": "completed_scenario" if complete else "stopped_incomplete_scenario",
        "requested_start": str(start),
        "requested_end": str(end),
        "stop": stop,
        "committed_days": len(records),
        "model": "Alpha158 rolling LightGBM",
        "initial_cash_cny": 200000,
        "metrics": metrics,
        "delisted_valuation": delisted_valuation,
        "exclude_st": exclude_st,
        "fractional_policy": fractional_policy,
        "delisted_recovery_is_unknown": any(m["method"].startswith("delisted_") for m in all_marks),
        "execution_authority": False,
        "actual_broker_performance": False,
        "source_files": len(data.manifest),
        "model_fits": 0,
        "ledger_sha256": hashlib.sha256((output / "ledger.csv").read_bytes()).hexdigest(),
    }
    write_json(output / "result.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--delisted-valuation", choices=["stop", "zero", "last"], default="stop")
    parser.add_argument("--exclude-st", action="store_true")
    parser.add_argument("--fractional-policy", choices=["stop", "floor"], default="stop")
    args = parser.parse_args()
    run(
        args.source_root.resolve(),
        args.output.resolve(),
        args.delisted_valuation,
        args.exclude_st,
        args.fractional_policy,
    )
