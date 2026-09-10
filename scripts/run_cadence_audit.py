#!/usr/bin/env python3
"""Run the bounded Daily v1.1 cadence audit."""

from pathlib import Path

from quantlab.research.cadence_audit import run_cadence_audit

if __name__ == "__main__":
    output = run_cadence_audit(Path("config/research_daily_v1.json"))
    print(f"output: {output}")
