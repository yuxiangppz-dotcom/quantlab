#!/usr/bin/env python3
"""Publish a formal 2020-2024 execution-readiness audit without trading claims."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantlab.artifacts import ArtifactPublisher, atomic_write_json, stream_csv  # noqa: E402
from quantlab.execution import (  # noqa: E402
    EXCHANGE_TIMEZONE,
    PITIdentityBook,
    TargetHandoffConfig,
    TradingCalendar,
    build_rebalance_instruction,
    default_a_share_rule_book,
)
from quantlab.execution.artifacts import (  # noqa: E402
    EXECUTION_READINESS_SCHEMA,
    READINESS_CHECK_COLUMNS,
    execution_readiness_artifact_contract,
    verify_execution_readiness_artifact,
)
from quantlab.execution.readiness import (  # noqa: E402
    build_execution_readiness_report,
    inspect_execution_inputs,
)
from quantlab.portfolio import TargetPortfolio  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2024, 12, 31)


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _git_clean() -> bool:
    return not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _calendar_for_handoff(canonical_root: Path) -> TradingCalendar:
    path = canonical_root / "calendar" / "calendar.parquet"
    frame = pd.read_parquet(path)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    sessions = tuple(sorted(set(frame.loc[frame["is_open"], "trade_date"])))
    from quantlab.artifacts import sha256_file

    return TradingCalendar(
        sessions=sessions,
        coverage_start=min(frame["trade_date"]),
        coverage_end=max(frame["trade_date"]),
        source_id="canonical/calendar/calendar.parquet",
        source_sha256=sha256_file(path),
    )


def _jsonable_handoff(result) -> dict:
    payload = asdict(result)
    payload["is_order_submission"] = False
    payload["is_fill_evidence"] = False
    payload["price_role"] = "planning_only"
    payload["financial_calculation_scope"] = "integer_share_rounding_only"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Formal execution-readiness audit for 2020-01-01 through 2024-12-31."
    )
    parser.add_argument(
        "--canonical-root", type=Path, default=PROJECT_ROOT / "data" / "canonical"
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "experiments" / EXECUTION_READINESS_SCHEMA,
    )
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    if not _git_clean():
        raise RuntimeError("formal execution readiness requires a clean git workspace")
    head_before = _git_head()
    run_id = args.run_id or datetime.now(EXCHANGE_TIMEZONE).strftime("%Y%m%dT%H%M%S")
    code_changes_path = PROJECT_ROOT / "config" / "security_code_changes.csv"
    evidence, inventory_before = inspect_execution_inputs(
        args.canonical_root, code_changes_path, PERIOD_START, PERIOD_END
    )

    calendar = _calendar_for_handoff(args.canonical_root)
    execution_date = calendar.next_session(PERIOD_END)
    if execution_date is None:
        raise RuntimeError("canonical calendar lacks the next session after audit end")
    smoke_target = TargetPortfolio(as_of=PERIOD_END, positions=(), cash_weight=1.0)
    smoke = build_rebalance_instruction(
        smoke_target,
        TargetHandoffConfig(
            portfolio_id="execution-readiness-all-cash-smoke",
            signal_as_of=datetime.combine(PERIOD_END, time(15, 30), EXCHANGE_TIMEZONE),
            execution_date=execution_date,
            planning_nav_fen=1_000_000,
            minimum_cash_fen=1_000_000,
        ),
        {},
        calendar=calendar,
        identities=PITIdentityBook(()),
        rules=default_a_share_rule_book(),
    )
    report, rule_inventory = build_execution_readiness_report(
        evidence,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        handoff_smoke_valid=smoke.instruction is not None,
    )

    publisher = ArtifactPublisher(
        args.out_root,
        run_id,
        expected_registry=execution_readiness_artifact_contract(),
        head=head_before,
        schema=EXECUTION_READINESS_SCHEMA,
    )
    try:
        evidence_after, inventory_after = inspect_execution_inputs(
            args.canonical_root, code_changes_path, PERIOD_START, PERIOD_END
        )
        stable = inventory_before == inventory_after and evidence == evidence_after
        head_after = _git_head()
        clean_after = _git_clean()
        if not stable or head_after != head_before or not clean_after:
            raise RuntimeError("code or canonical inputs changed during readiness audit")

        input_inventory = {
            "stable_during_audit": stable,
            "before": inventory_before,
            "after": inventory_after,
        }
        summary = {
            "analysis_type": "execution_readiness",
            "experiment_schema": EXECUTION_READINESS_SCHEMA,
            "run_id": run_id,
            "code_version": head_before,
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
            "check_count": len(report.checks),
            "status_counts": report.status_counts,
            "gates": report.gates,
            "claims": {
                "performance_claim": False,
                "fill_claim": False,
                "order_submission": False,
                "canonical_data_written": False,
                "external_provider_called": False,
            },
            "interpretation": (
                "Framework contracts are valid; historical, paper, and live execution "
                "remain fail-closed until their independent blockers are resolved."
            ),
        }
        atomic_write_json(publisher.staging / "summary.json", summary)
        stream_csv(
            publisher.staging / "readiness_checks.csv",
            list(READINESS_CHECK_COLUMNS),
            iter({
                "check_id": check.check_id,
                "category": check.category,
                "status": check.status.value,
                "finding": check.finding,
                "evidence_json": json.dumps(
                    check.evidence, sort_keys=True, ensure_ascii=False, default=str
                ),
                "limitation": check.limitation,
                "critical_for": "|".join(check.critical_for),
            } for check in report.checks),
        )
        atomic_write_json(publisher.staging / "rule_inventory.json", rule_inventory)
        atomic_write_json(publisher.staging / "handoff_smoke.json", _jsonable_handoff(smoke))
        atomic_write_json(publisher.staging / "input_inventory.json", input_inventory)
        final = publisher.publish(summary)
        verified = verify_execution_readiness_artifact(
            final,
            expected_run_id=run_id,
            expected_head=head_before,
            expected_schema=EXECUTION_READINESS_SCHEMA,
        )
    except BaseException as exc:
        if publisher.staging.exists():
            publisher.mark_incomplete(exc)
        raise

    print(json.dumps({
        "run_dir": str(final),
        "gates": report.gates,
        "verified_files": verified["verified_files"],
        "artifact_manifest_sha256": verified["artifact_manifest_sha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
