"""Acquire the one fixed20-request ETF input pilot, without canonical writes."""

import os

for name in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "ARROW_NUM_THREADS",
):
    os.environ[name] = "2"
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from quantlab.data.etf_intake import run  # noqa: E402

if __name__ == "__main__":
    try:
        print(json.dumps(run(Path(__file__).resolve().parents[1])), flush=True)
    except Exception as exc:
        print(json.dumps({"failed": True, "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
