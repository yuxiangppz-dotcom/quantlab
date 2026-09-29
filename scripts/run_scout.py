#!/usr/bin/env python3
"""Compatibility wrapper for the Scout console command."""

from quantlab.scout.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
