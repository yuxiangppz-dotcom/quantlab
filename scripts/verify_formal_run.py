#!/usr/bin/env python3
"""Independent verifier for a formal QuantLab run directory.

Re-derives the complete expected artifact inventory from the declared
registry (14 groups x 13 standard files + top-level artifacts), then fails
hard on: missing files, sha256 mismatches, size mismatches, CSV header
mismatches, row-count mismatches, missing control recovery bounds, a missing
or invalid COMPLETED.json marker, a manifest that does not match the actual
directory, or a summary whose schema/HEAD disagree with the expectations.

Exit codes: 0 = verified, 1 = verification failure. This verifier never
trusts summary.json booleans.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantlab.backtest.artifacts import (  # noqa: E402
    STANDARD_GROUP_FAMILY,
    verify_formal_artifact,
)

EXPERIMENT_SCHEMA = "performance_baseline_benchmark_correctness_v0_1_2"

EXPORT_GROUPS = (
    "A_legacy_baseline",
    "A_legacy_baseline_diagnostic",
    "C_legacy_admission_v2",
    "C_legacy_admission_v2_diagnostic",
    "A_v1_baseline",
    "A_v1_baseline_diagnostic",
    "C_v1_admission_v2",
    "C_v1_admission_v2_diagnostic",
    "legacy_settlement_recovery_1",
    "legacy_settlement_recovery_0",
    "primary_strategy_recovery_assumption_1",
    "primary_strategy_recovery_assumption_0",
    "equal_weight_v1_control_recovery_assumption_1",
    "equal_weight_v1_control_recovery_assumption_0",
)

TOP_LEVEL = (
    "summary.json",
    "manifest.json",
    "delisting_facts.json",
    "delisting_audit.csv",
    "shadow_admission.csv",
    "code_lineage_audit.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="formal run directory")
    parser.add_argument(
        "--expected-head",
        default=None,
        help="git HEAD the run was launched from (enforced against summary)",
    )
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.is_dir():
        print(f"FAIL: {run_dir} is not a directory")
        return 1
    if run_dir.name.endswith(".incomplete"):
        print("FAIL: staged .incomplete directory is not a formal artifact")
        return 1

    registry = {
        "groups": {g: list(STANDARD_GROUP_FAMILY) for g in EXPORT_GROUPS},
        "top_level": list(TOP_LEVEL),
    }
    summary = json.loads((run_dir / "summary.json").read_text())
    expected_head = args.expected_head or summary.get("code_version")

    try:
        result = verify_formal_artifact(
            run_dir,
            registry,
            expected_head=expected_head,
            expected_schema=EXPERIMENT_SCHEMA,
        )
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1

    print("VERIFIED:", json.dumps(result, indent=2))
    print(f"run_dir: {run_dir}")
    print(f"schema: {summary.get('experiment_schema')}")
    print(f"head: {summary.get('code_version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
