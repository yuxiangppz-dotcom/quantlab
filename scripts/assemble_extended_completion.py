import json
import os
from pathlib import Path

for name in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "ARROW_NUM_THREADS",
):
    os.environ[name] = "2"

from quantlab.research.extended_completion_assembly import assemble  # noqa: E402

if __name__ == "__main__":
    r = assemble(Path.cwd())
    print(
        json.dumps(
            {
                k: r[k]
                for k in (
                    "fingerprint",
                    "status",
                    "prediction_rows",
                    "diagnostic_counts",
                    "parent_status",
                    "independent_verification_complete",
                )
            }
        )
    )
