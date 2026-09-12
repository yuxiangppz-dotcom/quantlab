import json
import os
from pathlib import Path

for key in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "ARROW_NUM_THREADS",
):
    os.environ[key] = "2"
from quantlab.research.extended_recovery import run  # noqa: E402

if __name__ == "__main__":
    r = run(Path.cwd())
    print(
        json.dumps(
            {
                k: r[k]
                for k in (
                    "status",
                    "completed_recovery_jobs",
                    "prediction_rows",
                    "additional_fit_attempts",
                    "combined_model_references",
                )
            }
        )
    )
