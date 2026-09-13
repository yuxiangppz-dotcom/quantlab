"""Single independent rational-number proof of the liquidity-only projection."""

import json
import math
import subprocess
import time
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pandas as pd

from quantlab.research.alpha158_store import atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.round2_dataset import sealed_read, verify_entries


def main(root):
    c = json.loads((root / "config/liquidity_amount_lower_bound_v1.json").read_text())
    out = root / c["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        if any((out / n).exists() for n in ("proof_started.json", "independent_proof.json")):
            raise ValueError("liquidity proof already consumed")
        report = sealed_read(out / "report.json")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        assert head == report["source_head"]
        started = time.monotonic()
        atomic_seal(out / "proof_started.json", {"at": datetime.now(UTC).isoformat(), "head": head})
        verify_entries(root, c["inputs"])
        verify_entries(root, sealed_read(out / "started.json")["code_inputs"])
        table = sealed_read(out / "projection.json")
        assert table["fingerprint"] == report["projection_fingerprint"]
        old = sealed_read(root / c["old_output"] / "observations.json")["rows"]
        rows = table["rows"]
        assert [r["instrument_id"] for r in rows] == [r["instrument_id"] for r in old]
        raw = {
            day: pd.read_parquet(root / path).set_index("instrument_id").amount.to_dict()
            for day, path in zip(c["dates"], c["paths"], strict=True)
        }
        counts = dict(
            cohort=0,
            exact_adv20_known=0,
            lower_bound_adv20_known=0,
            fractional_fen_rows_floored=0,
            unknown_code_dates_retained=0,
        )
        for row, previous in zip(rows, old, strict=True):
            values, unknown, floored = {}, [], []
            for day in c["dates"]:
                v = raw[day].get(row["instrument_id"])
                valid = type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 10**13
                number = Fraction(str(v)) * 100 if valid else None
                if number is None or not 0 <= number <= 10**15:
                    values[day] = None
                    unknown.append(day)
                else:
                    values[day] = number.numerator // number.denominator
                    assert 0 <= number - values[day] < 1
                    if number.denominator != 1:
                        floored.append(day)
            mean = None if unknown else sum(values.values()) // 20
            assert row["daily_floor_fen"] == values and row["adv20_floor_fen"] == mean
            assert row["unknown_dates"] == unknown and row["floored_dates"] == floored
            assert row["copied_exact_adv20_fen"] == previous["session"]["prior20_amount_fen"]
            if row["copied_exact_adv20_fen"] is not None:
                assert mean == row["copied_exact_adv20_fen"]
            counts["cohort"] += 1
            counts["exact_adv20_known"] += row["copied_exact_adv20_fen"] is not None
            counts["lower_bound_adv20_known"] += mean is not None
            counts["fractional_fen_rows_floored"] += len(floored)
            counts["unknown_code_dates_retained"] += len(unknown)
        assert counts == report["counts"]
        assert all(
            report[k] is False
            for k in (
                "cashflow_eligible",
                "performance_evidence",
                "execution_authority",
                "controller_installed",
                "original_sources_repaired",
            )
        )
        verify_entries(root, c["inputs"])
        result = atomic_seal(
            out / "independent_proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "counts": counts,
                "all_daily_bounds_and_masks_reconciled": True,
                "method": "independent Fraction floors and integer mean; same agent",
                "seconds": time.monotonic() - started,
                "peak_rss_bytes": peak_rss_bytes(),
                "performance_evidence": False,
                "execution_authority": False,
            },
        )
        print(json.dumps({"fingerprint": result["fingerprint"], "counts": counts}))


if __name__ == "__main__":
    main(Path.cwd())
