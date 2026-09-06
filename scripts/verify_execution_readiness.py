#!/usr/bin/env python3
"""Independent formal verifier for an execution-readiness artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantlab.execution.artifacts import (  # noqa: E402
    EXECUTION_READINESS_SCHEMA,
    verify_execution_readiness_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify execution-readiness evidence.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-run-id", default=None)
    parser.add_argument("--expected-schema", default=EXECUTION_READINESS_SCHEMA)
    args = parser.parse_args()
    try:
        result = verify_execution_readiness_artifact(
            args.run_dir,
            expected_run_id=args.expected_run_id,
            expected_head=args.expected_head,
            expected_schema=args.expected_schema,
        )
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("VERIFIED (execution readiness):", json.dumps(result, indent=2))
    print(f"run_dir: {args.run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
