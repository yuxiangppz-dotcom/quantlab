#!/usr/bin/env python3
"""Independent verifier for a formal QuantLab run directory (v0.1.3).

Formal mode (default) requires an explicit external ``--expected-head`` and
cross-binds: directory basename, run id, HEAD, schema, summary, artifact
manifest, completion marker and the canonical registry. It fails hard on
staged ``.incomplete`` directories, missing/invalid completion markers,
``INCOMPLETE.json`` files, temp/partial files, inventory or hash mismatches,
and any metadata disagreement between marker/manifest/summary/directory.

``--mode diagnostic`` is the explicitly-named NON-formal mode: it checks
embedded metadata and payload integrity only (no external HEAD binding, no
completion-marker requirement) and must never be used for publication.

Exit codes: 0 = verified, 1 = verification failure. This verifier never
trusts recorded booleans.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantlab.backtest.artifacts import (  # noqa: E402
    STANDARD_GROUP_FAMILY,
    backtest_artifact_contract,
    verify_formal_artifact,
)

SCHEMA = "performance_baseline_benchmark_correctness_v0_1_3"

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
    parser = argparse.ArgumentParser(
        description="Formal (default) or diagnostic-only artifact verification.",
    )
    parser.add_argument("run_dir", type=Path, help="formal run directory")
    parser.add_argument(
        "--mode",
        choices=("formal", "diagnostic"),
        default="formal",
        help="formal (default; requires --expected-head) or the explicitly "
        "named non-formal diagnostic mode",
    )
    parser.add_argument(
        "--expected-head",
        default=None,
        help="git HEAD the run was launched from (REQUIRED in formal mode)",
    )
    parser.add_argument(
        "--expected-schema", default=SCHEMA, help="expected experiment schema"
    )
    parser.add_argument(
        "--expected-run-id", default=None, help="expected run id (basename)"
    )
    args = parser.parse_args()

    formal = args.mode == "formal"
    if formal and not args.expected_head:
        parser.error(
            "formal mode requires an explicit --expected-head <full SHA>; "
            "use --mode diagnostic for the non-formal embedded-metadata check"
        )

    run_dir = args.run_dir
    if not run_dir.is_dir():
        print(f"FAIL: {run_dir} is not a directory")
        return 1

    contract = backtest_artifact_contract(
        schema=args.expected_schema,
        groups={g: list(STANDARD_GROUP_FAMILY) for g in EXPORT_GROUPS},
        top_level=TOP_LEVEL,
    )

    try:
        result = verify_formal_artifact(
            run_dir,
            contract,
            expected_run_id=args.expected_run_id,
            expected_head=args.expected_head,
            expected_schema=args.expected_schema,
            formal=formal,
        )
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1

    print(f"VERIFIED ({args.mode}):", json.dumps(result, indent=2))
    print(f"run_dir: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
