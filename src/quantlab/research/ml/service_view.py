"""Read-only paper-account monitoring and benchmark reports from committed records."""

from __future__ import annotations

import json

import pandas as pd

from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import (
    checkpoint_read,
    complete,
    verify_publication,
    write_frame,
)
from quantlab.research.ml.io import research_output, sha256, write_json
from quantlab.research.ml.reporting import comparison_metrics
from quantlab.research.ml.service import account_head, inspect_service, load_service


def account_frames(root):
    service = load_service(root)
    account_head(root, service)
    rows, holdings, orders, targets = [], [], [], []
    previous = service["capital_fen"]
    for folder in sorted((root / "sessions").glob("*")):
        if not folder.is_dir():
            continue
        saved = checkpoint_read(folder / "account.json")
        book = saved["state"]["book"]
        published = verify_publication(folder, book.asof_date, require_forward=False)
        record, settlement = saved["record"], saved["settlement"]
        if record is not None:
            trades = sum(a.transition.simulated_notional_fen for a in record.attempts)
            slippage = sum(
                abs(a.transition.modeled_price_fen - saved["state"]["marks"][a.order.instrument_id])
                * a.transition.simulated_quantity
                for a in record.attempts
                if a.transition.modeled_price_fen is not None
            )
            rows.append(
                {
                    "session": str(book.asof_date),
                    "equity_fen": record.marked_equity_fen,
                    "cash_fen": book.cash_fen,
                    "daily_return": record.marked_equity_fen / previous - 1,
                    "one_way_turnover": trades / (2 * previous),
                    "fees_fen": record.modeled_fees_fen,
                    "slippage_fen": slippage,
                    "risk_breaches": len(settlement["realized_exposure"]["breaches"]),
                    "missing_decision": bool(settlement.get("decision_missing")),
                    "decision_status": saved["status"],
                }
            )
            previous = record.marked_equity_fen
            for attempt in record.attempts:
                t = attempt.transition
                orders.append(
                    {
                        "session": str(book.asof_date),
                        "order_id": attempt.order.order_id,
                        "instrument_id": attempt.order.instrument_id,
                        "side": attempt.order.side,
                        "desired": attempt.order.desired_quantity,
                        "simulated": t.simulated_quantity,
                        "status": t.status,
                        "reason": t.reason,
                        "notional_fen": t.simulated_notional_fen,
                    }
                )
        for lot in book.lots:
            holdings.append(
                {
                    "session": str(book.asof_date),
                    "instrument_id": lot.instrument_id,
                    "quantity": lot.quantity,
                    "sellable_on": str(lot.sellable_on),
                    "raw_mark_fen": saved["state"]["marks"][lot.instrument_id],
                }
            )
        if saved["plan"]:
            for name, weight in saved["plan"]["target_weights"].items():
                targets.append(
                    {
                        "signal_date": str(book.asof_date),
                        "instrument_id": name,
                        "target_weight": weight,
                        "forward": published["forward_eligible"],
                    }
                )
    return {
        "ledger": pd.DataFrame(rows),
        "holdings": pd.DataFrame(holdings),
        "attempts": pd.DataFrame(orders),
        "targets": pd.DataFrame(targets),
    }


def build_service_report(root, benchmark_path, output):
    research_output(output)
    # Freeze a consistent prefix while a daily worker might otherwise append a session.
    with exclusive_job(root):
        frames = account_frames(root)
        ledger = frames["ledger"]
        if ledger.empty:
            raise ValueError("no settled sessions to report")
        before = sha256(benchmark_path)
        benchmark = pd.read_parquet(benchmark_path)
        if set(benchmark) != {"session", "benchmark_return"}:
            raise ValueError("benchmark requires exactly session and benchmark_return")
        benchmark["session"] = pd.to_datetime(benchmark.session).dt.strftime("%Y-%m-%d")
        if benchmark.session.duplicated().any():
            raise ValueError("duplicate benchmark session")
        if set(ledger.session) - set(benchmark.session):
            raise ValueError("benchmark missing settled sessions")
        aligned = ledger.merge(benchmark, on="session", how="left", validate="one_to_one")
        result = {
            "schema": "quantlab_paper_report_v1",
            "status": inspect_service(root),
            "metrics": comparison_metrics(aligned.daily_return, aligned.benchmark_return),
            "fees_fen": int(ledger.fees_fen.sum()),
            "slippage_fen": int(ledger.slippage_fen.sum()),
            "mean_one_way_turnover": float(ledger.one_way_turnover.mean()),
            "missing_decision_days": int(ledger.missing_decision.sum()),
            "risk_breach_days": int(ledger.risk_breaches.gt(0).sum()),
            "source_sessions": {
                p.parent.name: sha256(p)
                for p in sorted((root / "sessions").glob("*/completed.json"))
            },
            "benchmark_sha256": before,
            "performance_eligible": False,
        }
        if sha256(benchmark_path) != before:
            raise ValueError("benchmark changed during report")
        output.mkdir(parents=True, exist_ok=False)
        for name, frame in frames.items():
            write_frame(output / f"{name}.parquet", frame)
        write_frame(output / "comparison.parquet", aligned)
        write_json(output / "report.json", result)
        (output / "report.md").write_text(
            "# 每日模拟账户报告\n\n仅为已封存决策的日频模拟结果；非券商成交或策略有效性认证。\n\n"
            + "```json\n"
            + json.dumps(result, indent=2, ensure_ascii=False)
            + "\n```\n",
            encoding="utf-8",
        )
        complete(output)
        return result
