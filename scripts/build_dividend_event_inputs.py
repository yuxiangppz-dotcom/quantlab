"""Build the authorized local observation/window companion once."""

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

import pyarrow as pa  # noqa: E402

pa.set_cpu_count(2)
pa.set_io_thread_count(2)

from quantlab.research.dividend_event_run import run  # noqa: E402

if __name__ == "__main__":
    report = run(Path(__file__).resolve().parents[1])
    print(
        json.dumps({key: report[key] for key in ("fingerprint", "totals", "resources")}), flush=True
    )
