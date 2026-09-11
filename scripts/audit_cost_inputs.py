"""One sealed cost/corporate-input audit of existing scores; never a model run."""

import os

for name in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[name] = "2"

import json  # noqa: E402
import subprocess  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import pyarrow as pa  # noqa: E402

pa.set_cpu_count(2)
pa.set_io_thread_count(2)

from quantlab.data.models import DataValidationError  # noqa: E402
from quantlab.research.alpha158_store import (  # noqa: E402
    Budget,
    atomic_seal,
    exclusive_job,
    peak_rss_bytes,
)
from quantlab.research.cost_input_audit import run_audit  # noqa: E402
from quantlab.research.cost_input_sources import CONFIG, MANIFEST, OUTPUT, load_inputs  # noqa: E402
from quantlab.research.input_audit import _sha  # noqa: E402
from quantlab.research.round2_dataset import InputBinding, code_binding  # noqa: E402


def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / CONFIG).read_text())
    out = root / OUTPUT
    with exclusive_job(out):
        if (out / "started.json").exists():
            raise DataValidationError("cost audit already started; preserve existing attempt")
        budget = Budget(out, config["resources"])
        with budget.watchdog():
            budget.check(projected_bytes=64 * 1024**2, projected_memory=512 * 1024**2)
            binding = InputBinding(root)
            head = code_binding(root, binding)
            if (
                head
                != subprocess.check_output(
                    ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
                ).strip()
            ):
                raise DataValidationError("cost audit implementation must be clean and pushed")
            config, manifest, inventory, rolling = load_inputs(root)
            atomic_seal(
                out / "started.json",
                {
                    "at": datetime.now(UTC).isoformat(),
                    "source_head": head,
                    "contract_sha256": _sha(root / CONFIG),
                    "source_manifest_sha256": _sha(root / MANIFEST),
                    "execution_authority": False,
                    "new_fit_attempts": 0,
                },
            )
            try:
                result = run_audit(root, out, config, manifest, inventory, rolling)
                binding.check()
                load_inputs(root)
                artifacts = {
                    p.relative_to(out).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size}
                    for p in sorted(out.rglob("*"))
                    if p.is_file() and p.name != "worker.lock"
                }
                import time

                report = atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "version": config["version"],
                        "source_head": head,
                        "contract_sha256": _sha(root / CONFIG),
                        "source_manifest_sha256": _sha(root / MANIFEST),
                        "artifacts": artifacts,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes_before_report": budget.check(projected_bytes=2 * 1024**2),
                            "arrow_cpu_threads": pa.cpu_count(),
                            "arrow_io_threads": pa.io_thread_count(),
                        },
                        **result,
                    },
                )
                print(
                    json.dumps(
                        {
                            k: report[k]
                            for k in (
                                "fingerprint",
                                "score_rows",
                                "target_rows",
                                "summary",
                                "resources",
                            )
                        }
                    ),
                    flush=True,
                )
            except BaseException as exc:
                atomic_seal(
                    out / "failed.json",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "source_head": head,
                        "new_fit_attempts": 0,
                    },
                )
                raise


if __name__ == "__main__":
    main()
