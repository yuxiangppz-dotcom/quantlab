"""One separate scalar verification of a price-only weekend observation, no labels."""

import json
import math
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from quantlab.research.alpha158_store import atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.round2_dataset import sealed_read, verify_entries

MEASURES = ("open", "high", "low", "close", "volume", "amount", "adj_factor")


def same(left, right):
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left) and pd.isna(right))
    return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-14)


def scalar_prices(records):
    values = []
    for r in records:
        numbers = [r[k] for k in MEASURES]
        valid = all(not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in numbers)
        valid = valid and r["low"] <= r["open"] <= r["high"]
        valid = valid and r["low"] <= r["close"] <= r["high"]
        p = r["close"] * r["adj_factor"] if valid else math.nan
        values.append(p if math.isfinite(p) and p > 0 else math.nan)
    return values


def source_rows(root, c):
    """Independent units/key mapping; no production join or scoring helpers."""
    observed = {}
    for p in c["canonical_paths"]:
        for r in pd.read_parquet(root / p).to_dict("records"):
            key = (r["instrument_id"], pd.Timestamp(r["trade_date"]).strftime("%Y-%m-%d"))
            observed.setdefault(key, {}).update({k: r[k] for k in MEASURES if k in r})
    out = root / c["output"]
    for api in ("daily", "adj_factor"):
        folder = out / "attempts" / api
        receipt = sealed_read(folder / "result.json")
        verify_entries(folder, receipt["artifacts"])
        assert receipt["status"] == "nonempty" and receipt["raw_redacted"] is False
        raw = json.loads((folder / "response.body").read_bytes())["data"]
        for row in raw["items"]:
            r = dict(zip(raw["fields"], row, strict=True))
            assert r["trade_date"] == "20260911"
            key = (r["ts_code"], "2026-09-11")
            target = observed.setdefault(key, {})
            for source, dest, scale in (
                ("open", "open", 1),
                ("high", "high", 1),
                ("low", "low", 1),
                ("close", "close", 1),
                ("vol", "volume", 100),
                ("amount", "amount", 1000),
                ("adj_factor", "adj_factor", 1),
            ):
                if source in r:
                    target[dest] = math.nan if r[source] is None else float(r[source]) * scale
    return observed


def main(root):
    c = json.loads((root / "config/s4_prospective_observation_v1.json").read_text())
    out = root / c["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        if any((out / name).exists() for name in ("proof_started.json", "independent_proof.json")):
            raise ValueError("prospective proof attempt already consumed")
        report = sealed_read(out / "observation.json")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        assert head == report["source_head"]
        started = time.monotonic()
        atomic_seal(out / "proof_started.json", {"at": datetime.now(UTC).isoformat(), "head": head})
        verify_entries(root, c["inputs"])
        verify_entries(out, report["files"])
        intent = sealed_read(out / "started.json")
        verify_entries(root, intent["code_inputs"])
        assert intent["fingerprint"] == report["intent_fingerprint"]
        intake = sealed_read(out / "intake_report.json")
        assert intake["fingerprint"] == report["intake_fingerprint"]
        assert intake["requests_attempted"] == 2 and intake["status_counts"] == {"nonempty": 2}
        codes = sealed_read(root / c["selection_path"])["instrument_ids"]
        inputs = pd.read_parquet(out / "bound_price_inputs.parquet")
        scores = pd.read_parquet(out / "scores.parquet")
        assert list(scores.instrument_id) == codes and len(codes) == 256
        assert scores.price_as_of.eq("2026-09-11").all()
        assert (
            len(inputs) == 21 * 256 and not inputs.duplicated(["instrument_id", "trade_date"]).any()
        )
        raw = source_rows(root, c)
        returns = {}
        for code in codes:
            part = inputs[inputs.instrument_id.eq(code)]
            assert (
                list(pd.to_datetime(part.trade_date).dt.strftime("%Y-%m-%d")) == c["window_dates"]
            )
            rows = part.to_dict("records")
            for day, row in zip(c["window_dates"], rows, strict=True):
                source = raw.get((code, day), {})
                assert all(same(row[k], source.get(k, math.nan)) for k in MEASURES)
            p = scalar_prices(rows)
            returns[code] = {}
            for n in (3, 20):
                value = (
                    p[-1] / p[-n - 1] - 1
                    if all(math.isfinite(v) for v in p[-n - 1 :])
                    else math.nan
                )
                returns[code][n] = value if math.isfinite(value) else math.nan
        valid = [v[3] for v in returns.values() if math.isfinite(v[3])]
        middle = statistics.median(valid) if valid else math.nan
        for row in scores.to_dict("records"):
            changes = returns[row["instrument_id"]]
            assert same(row["change3"], changes[3]) and same(row["change20"], changes[20])
            assert same(row["S4-A"], middle - changes[3])
            assert same(row["reference_reversal20"], -changes[20])
            assert row["S4_A_known"] == math.isfinite(changes[3])
            assert row["reference20_known"] == math.isfinite(changes[20])
        assert report["known_S4_A"] == len(valid)
        assert report["known_reference20"] == sum(math.isfinite(v[20]) for v in returns.values())
        timing = report["timing"]
        created, available, cutoff = [
            datetime.fromisoformat(timing[k])
            for k in (
                "created_at",
                "source_available_at",
                "publication_cutoff",
            )
        ]
        assert all(t.utcoffset() is not None for t in (created, available, cutoff))
        assert available <= created < cutoff and cutoff.isoformat() == "2026-09-14T09:30:00+08:00"
        assert report["label_dates"] == c["label_dates"]
        assert report["future_diagnostic_pending"] is True
        assert timing["old_same_day_forward_shadow_eligible"] is False
        assert all(
            report[k] is False
            for k in (
                "broker_order",
                "performance_evidence",
                "execution_authority",
                "candidate_promoted",
            )
        )
        assert report["economic_paths"] == report["model_fits"] == 0
        verify_entries(root, c["inputs"])
        proof = atomic_seal(
            out / "independent_proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "checked_codes": 256,
                "checked_grid_rows": 5376,
                "raw_unit_mapping_reconciled": True,
                "all_scores_and_masks_reconciled": True,
                "method": "independent source mapping, scalar price windows and median; same agent",
                "seconds": time.monotonic() - started,
                "peak_rss_bytes": peak_rss_bytes(),
                "performance_evidence": False,
                "execution_authority": False,
            },
        )
        print(json.dumps({"fingerprint": proof["fingerprint"], "checked_codes": 256}))


if __name__ == "__main__":
    main(Path.cwd())
