#!/usr/bin/env python3
"""Run the retrospective exact-count Daily product strategy audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from quantlab.research.product_strategy_audit import run_product_strategy_audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit-config",
        type=Path,
        default=Path("config/product_strategy_audit_v1.json"),
    )
    parser.add_argument(
        "--daily-config",
        type=Path,
        default=Path("config/daily_mvp_v1.json"),
    )
    args = parser.parse_args()
    output = run_product_strategy_audit(args.audit_config, args.daily_config)
    print(f"output: {output}")


if __name__ == "__main__":
    main()
