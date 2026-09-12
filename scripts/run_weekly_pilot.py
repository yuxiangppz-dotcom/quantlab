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

from quantlab.research.weekly_pilot import run  # noqa: E402

if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {key: result[key] for key in ("status", "cumulative_fit_attempts", "completed_fits")}
        )
    )
