"""Compact saved ledgers for explicitly hypothetical adjusted-price value transfers."""

import hashlib
from dataclasses import asdict

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_economic_contract import CAPITALS


def write_positions(path, books):
    schema = pa.schema(
        [
            ("trade_date", pa.date32()),
            ("instrument_id", pa.string()),
            ("value", pa.float64()),
            ("weight", pa.float64()),
            ("last_price", pa.float64()),
            ("last_mark_date", pa.date32()),
            ("missing_price", pa.bool_()),
        ]
    )
    count = 0
    with pq.ParquetWriter(path, schema, compression="zstd") as writer:
        for snapshot in books:
            rows = [{"trade_date": snapshot.trade_date, **asdict(p)} for p in snapshot.positions]
            if rows:
                writer.write_table(pa.Table.from_pylist(rows, schema=schema))
                count += len(rows)
    return count


def gross_identity(result):
    """Lossless streaming identity, including all zero-friction position states."""
    import json

    digest = hashlib.sha256()
    for snap in result.books:
        if snap.book == "gross":
            digest.update(
                json.dumps(
                    asdict(snap), sort_keys=True, default=str, separators=(",", ":")
                ).encode()
            )
    return digest.hexdigest()


def write_result(folder, result, scenario, plan, *, elapsed_seconds):
    if result.run_mode != "strict" or result.settlement_events:
        raise DataValidationError("economic path changed strict/no-settlement semantics")
    books = [b for b in result.books if b.book == "net"]
    daily = pd.DataFrame(
        [{k: v for k, v in asdict(s).items() if k not in ("positions", "book")} for s in books]
    )
    daily = daily.rename(
        columns={"nav": "value_after_declared_friction", "fee": "declared_friction_charge"}
    )
    if not daily.empty:
        daily["drawdown"] = (
            daily.value_after_declared_friction / daily.value_after_declared_friction.cummax() - 1
        )
    daily.to_parquet(folder / "daily.parquet", index=False)
    positions = write_positions(folder / "positions.parquet", books)
    transfers = pd.DataFrame([asdict(t) for t in result.trades if t.book == "net"])
    if not transfers.empty:
        transfers = transfers.drop(columns="book").rename(
            columns={
                "signed_trade_value": "hypothetical_value_transfer",
                "execution_price": "adjusted_mark",
                "execution_date": "value_date",
            }
        )
    transfers.to_parquet(folder / "value_transfers.parquet", index=False)
    # Small session-level engine records remain explicit audit evidence, not broker reports.
    audit = {
        key: [asdict(x) for x in getattr(result, key)]
        for key in (
            "rebalances",
            "skipped_executions",
            "lifecycle_events",
            "accounting_checks",
            "risk_policy_audit",
            "settlement_events",
            "failed_attempts",
        )
    }
    # Dataclass dates are converted explicitly before the canonical JSON seal.
    import json

    atomic_seal(folder / "accounting.json", json.loads(json.dumps(audit, default=str)))
    completed = result.status == "completed"
    final = books[-1] if books else None
    sessions = plan.get("sources", {}).get("sessions", [])
    last_rebalance = result.rebalances[-1] if result.rebalances else None
    elapsed_sessions = None
    if last_rebalance and sessions and result.valid_through:
        elapsed_sessions = sessions.index(str(result.valid_through)) - sessions.index(
            str(last_rebalance.execution_date)
        )
    summary = {
        "scenario": scenario,
        "plan": plan["fingerprint"],
        "source_head": plan["code_head"],
        "engine_status": result.status,
        "full_period_completed": completed,
        "valid_through": str(result.valid_through) if result.valid_through else None,
        "first_blocking_event": json.loads(
            json.dumps(asdict(result.first_blocking_event), default=str)
        )
        if result.first_blocking_event
        else None,
        "accounting_error": result.accounting_error,
        "valid_sessions": len(books),
        "position_rows": positions,
        "final_value_after_declared_friction": final.nav if final else None,
        "return_after_declared_friction_on_valid_prefix": final.nav - 1 if final else None,
        "drawdown_on_valid_prefix": float(daily.drawdown.min()) if final else None,
        "declared_friction_charged": float(daily.declared_friction_charge.sum()) if final else None,
        "final_cash": final.cash if final else None,
        "terminal_open_positions": final.holdings_count if final else None,
        "no_terminal_liquidation": True,
        "last_rebalance_signal": str(last_rebalance.signal_date) if last_rebalance else None,
        "last_rebalance_value_date": str(last_rebalance.execution_date) if last_rebalance else None,
        "observed_sessions_since_last_rebalance": elapsed_sessions,
        "terminal_planned_holding_interval_complete": elapsed_sessions >= scenario["horizon"]
        if elapsed_sessions is not None
        else None,
        "capital_scalings": {
            str(c): {"final_value": final.nav * c, "cash": final.cash * c} for c in CAPITALS
        }
        if final
        else {},
        "gross_ledger_identity": gross_identity(result),
        "gross_evidence_scenario": f"{scenario['policy']}_h{scenario['horizon']}_bps0",
        "solver_root_residual": result.solver_root_residual,
        "elapsed_seconds": elapsed_seconds,
        "history_already_observed": True,
        "historical_market_coverage_complete": False,
        "complete_user_fee_accounting": False,
        "execution_authority": False,
        "hypothetical_adjusted_price_proxy": True,
        "new_fits": 0,
        "provider_calls": 0,
    }
    if final and (not np.isfinite(final.nav) or final.cash < -1e-9):
        raise DataValidationError("economic output has invalid final cash/value")
    return atomic_seal(folder / "summary.json", summary)
