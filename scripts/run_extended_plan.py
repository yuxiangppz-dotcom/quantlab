"""Run a capped zero-fit calendar/population plan."""

import os

for key in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "ARROW_NUM_THREADS",
):
    os.environ[key] = "2"

import json  # noqa: E402

import pyarrow as pa  # noqa: E402

from quantlab.daily.service import PROJECT_ROOT  # noqa: E402
from quantlab.research.extended_plan_run import run  # noqa: E402

pa.set_cpu_count(2)
pa.set_io_thread_count(2)
if __name__ == "__main__":
    result = run(PROJECT_ROOT)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "weekly_rows",
                    "future_max_new_fit_attempts",
                    "new_fit_attempts",
                    "resources",
                )
            }
        )
    )
