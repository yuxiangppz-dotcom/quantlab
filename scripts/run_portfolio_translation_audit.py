#!/usr/bin/env python3
"""Run the bounded Daily v1.1 portfolio translation audit."""

from pathlib import Path

from quantlab.research.portfolio_validation import run_portfolio_translation_audit

if __name__ == "__main__":
    output = run_portfolio_translation_audit(Path("config/portfolio_translation_audit_v1_1.json"))
    print(f"output: {output}")
