"""Run one authorized, checkpointed raw dividend acquisition segment."""

import os

for name in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "ARROW_NUM_THREADS",
):
    os.environ[name] = "2"

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pyarrow as pa  # noqa: E402

pa.set_cpu_count(2)
pa.set_io_thread_count(2)

from quantlab.data.dividend_acquisition import run  # noqa: E402

if __name__ == "__main__":
    try:
        report = run(Path(__file__).resolve().parents[1])
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "fingerprint",
                        "attempts",
                        "retries",
                        "status_counts",
                        "returned_rows",
                        "stop_reason",
                        "resources",
                    )
                }
            ),
            flush=True,
        )
    except Exception as exc:
        # No exception text: provider credentials must not leak through tracebacks.
        print(json.dumps({"failed": True, "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
