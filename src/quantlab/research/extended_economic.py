"""Bounded 60-path execution; no training, providers, canonical writes or order adapters."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa

from quantlab.backtest.delisting_facts import load_validated_facts
from quantlab.backtest.lifecycle import LifecycleMonitor
from quantlab.backtest.models import BacktestConfig
from quantlab.backtest.run_spec import (
    BacktestRunSpec,
    fingerprint_lifecycle_monitor,
    fingerprint_risk_facts,
)
from quantlab.data.models import DataValidationError
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_economic_contract import (
    CONTRACT,
    CONTROL,
    END,
    OUT,
    RUNTIME,
    START,
    scenarios,
)
from quantlab.research.extended_economic_protocol import (
    Ledger,
    frame_hash,
    load_targets,
    monitor,
    preflight,
    prepare,
    verify_code,
    verify_locks,
)
from quantlab.research.extended_economic_results import write_result
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.weekly_pilot_protocol import HEAVY, inherited_locks, now


def worker(root, scenario_id, descriptors):
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    verify_locks(root, descriptors)
    out = root / OUT
    plan = sealed_read(out / "plan.json")
    verify_code(root, plan)
    verify_entries(root, plan["sources"]["inputs"])
    ledger = Ledger(out, plan["fingerprint"])
    if ledger.read(scenario_id)["status"] != "interrupted":
        raise DataValidationError("economic worker has no unused active reservation")
    scenario = next(s for s in scenarios() if s["id"] == scenario_id)
    folder = ledger.base / scenario_id
    atomic_seal(
        folder / "invoked.json",
        {"scenario": scenario_id, "plan": plan["fingerprint"], "at": now(), "pid": os.getpid()},
    )
    targets = load_targets(out, plan, f"{scenario['policy']}_h{scenario['horizon']}")
    verify_entries(out, {"marks.parquet": plan["artifacts"]["marks.parquet"]})
    marks = pd.read_parquet(out / "marks.parquet", use_threads=False)
    if frame_hash(marks) != plan["sources"]["marks_hash"]:
        raise DataValidationError("prepared adjusted marks changed")
    storage = ParquetStorage(root / "data/canonical")
    lifecycle = LifecycleMonitor(
        storage.load_securities(),
        load_security_code_changes(root / "config/security_code_changes.csv"),
        CONTRACT["lifecycle_mode"],
    )
    calendar = storage.load_trading_calendar()
    facts, _, errors = load_validated_facts(
        root / CONTRACT["risk_facts_path"], [(c.trade_date, c.is_open) for c in calendar]
    )
    if errors or fingerprint_lifecycle_monitor(lifecycle) != plan["sources"]["lifecycle_hash"]:
        raise DataValidationError("economic lifecycle evidence changed")
    if fingerprint_risk_facts(facts) != plan["sources"]["risk_facts_hash"]:
        raise DataValidationError("economic PIT risk evidence changed")
    spec = BacktestRunSpec(
        label=scenario_id,
        price_frame=marks[["instrument_id", "trade_date", "adj_close"]],
        open_dates=tuple(date.fromisoformat(d) for d in plan["sources"]["sessions"]),
        targets=targets,
        config=BacktestConfig(1.0, scenario["friction_bps"]),
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=lifecycle,
        requested_period_start=date.fromisoformat(START),
        requested_period_end=date.fromisoformat(END),
        risk_facts=facts,
        risk_policy=CONTRACT["risk_policy"],
    )
    symmetry = json.loads(json.dumps(spec.symmetry_fields(), default=str))
    if scenario["policy"] != CONTROL:
        control_id = f"{CONTROL}_h{scenario['horizon']}_bps{scenario['friction_bps']}"
        control_receipt = ledger.read(control_id)
        if control_receipt is None or control_receipt["status"] != "finished":
            raise DataValidationError("matching actual control invocation must be finished first")
        control_invocation = sealed_read(ledger.base / control_id / "invocation.json")
        if symmetry != control_invocation["symmetry"]:
            raise DataValidationError("actual strategy/control execution inputs are asymmetric")
    started = time.monotonic()
    result = spec.run()
    # The frozen spec, engine and all actual invocation inputs are the audit authority.
    spec.verify_bindings()
    atomic_seal(
        folder / "invocation.json",
        {
            "scenario": scenario,
            "symmetry": symmetry,
            "targets_fingerprint": spec.targets_fingerprint,
        },
    )
    write_result(folder, result, scenario, plan, elapsed_seconds=time.monotonic() - started)


def publish(root, plan):
    out = root / OUT
    ledger = Ledger(out, plan["fingerprint"])
    receipts = [ledger.read(s["id"]) for s in scenarios()]
    statuses = []
    for s, r in zip(scenarios(), receipts, strict=True):
        item = {"scenario": s, "status": "not_started" if r is None else r["status"]}
        if r is not None and r["status"] == "finished":
            item["summary"] = sealed_read(ledger.base / s["id"] / "summary.json")
        statuses.append(item)
    completed = sum(s["status"] == "finished" for s in statuses)
    failed = any(s["status"] in ("failed", "interrupted") for s in statuses)
    report = {
        "plan": plan["fingerprint"],
        "source_head": plan["code_head"],
        "at": now(),
        "status": "failed" if failed else "complete" if completed == 60 else "in_progress",
        "finished_paths": completed,
        "consumed_paths": sum(r is not None for r in receipts),
        "scenarios": statuses,
        "new_fits": 0,
        "provider_calls": 0,
        "execution_authority": False,
        "complete_user_fee_accounting": False,
        "historical_market_coverage_complete": False,
        "independent_verification_complete": False,
    }
    if report["status"] in ("complete", "failed"):
        target = out / "report.json"
        if target.exists():
            previous = sealed_read(target)
            if {k: v for k, v in previous.items() if k not in ("fingerprint", "at")} != {
                k: v for k, v in report.items() if k != "at"
            }:
                raise DataValidationError("terminal economic report changed")
            return previous
    else:
        target = out / "checkpoints" / f"{completed:03d}.json"
        if target.exists():
            return sealed_read(target)
    return atomic_seal(target, report)


def run(root):
    out = root / OUT
    with inherited_locks([root / HEAVY, out]) as descriptors:
        plan = prepare(root)
        ledger = Ledger(out, plan["fingerprint"])
        if (out / "report.json").exists():
            return publish(root, plan)
        started = time.monotonic()
        runtime = root / RUNTIME
        runtime.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                k: "2"
                for k in (
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                    "ARROW_NUM_THREADS",
                )
            }
        )
        for s in scenarios():
            previous = ledger.read(s["id"])
            if previous is not None:
                if previous["status"] != "finished":
                    return publish(root, plan)
                continue
            if time.monotonic() - started >= 2280:
                break
            preflight(root)
            verify_code(root, plan)
            folder = ledger.start(s["id"])
            print(f"economic scenario {scenarios().index(s) + 1}/60: {s['id']}", flush=True)
            try:
                command = [
                    sys.executable,
                    "-m",
                    "quantlab.research.extended_economic",
                    "--worker",
                    s["id"],
                ]
                for fd in descriptors:
                    command += ["--lock-fd", str(fd)]
                with (folder / "worker.log").open("xb") as log:
                    process = subprocess.Popen(
                        command,
                        cwd=root,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        pass_fds=tuple(descriptors),
                        start_new_session=True,
                    )
                    observed = monitor(process, root, started)
                atomic_seal(folder / "monitor.json", observed)
                if observed["returncode"] != 0 or observed["violation"] is not None:
                    raise DataValidationError(f"economic worker failed: {observed}")
                summary = sealed_read(folder / "summary.json")
                if summary["plan"] != plan["fingerprint"] or summary["scenario"] != s:
                    raise DataValidationError("economic summary identity changed")
                verify_code(root, plan)
            except Exception as exc:
                ledger.finish(s["id"], "failed", error=f"{type(exc).__name__}: {exc}")
                return publish(root, plan)
            ledger.finish(s["id"], "finished", engine_status=summary["engine_status"])
            publish(root, plan)
        return publish(root, plan)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--lock-fd", type=int, action="append", default=[])
    args = parser.parse_args()
    if args.worker:
        worker(Path.cwd(), args.worker, args.lock_fd)
    else:
        r = run(Path.cwd())
        print(json.dumps({k: r[k] for k in ("status", "finished_paths", "consumed_paths")}))


if __name__ == "__main__":
    main()
