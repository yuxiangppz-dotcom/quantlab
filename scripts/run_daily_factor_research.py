#!/usr/bin/env python3
"""Run the bounded Daily v1 factor and LightGBM experiment."""

from __future__ import annotations

import argparse
from pathlib import Path

from quantlab.research.factor_experiment import run_factor_experiment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    output = run_factor_experiment(args.config)
    print(f"output: {output}")


if __name__ == "__main__":
    main()
