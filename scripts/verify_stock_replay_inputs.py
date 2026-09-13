"""Independent Fraction arithmetic over the frozen first-session input materialization."""

import json
import math
import time
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pandas as pd

from quantlab.research.alpha158_store import atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.round2_dataset import sealed_read, verify_entries


def scaled(value, multiplier, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    number = Fraction(str(value)) * multiplier
    return int(number) if number.denominator == 1 and minimum <= number <= 10**15 else None


def main(root):
    c = json.loads((root / "config/stock_replay_inputs_v1.json").read_text())
    out = root / c["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        if (out / "independent_proof.json").exists() or (out / "proof_started.json").exists():
            raise ValueError("proof attempt already consumed")
        started = time.monotonic()
        atomic_seal(out / "proof_started.json", {"at": datetime.now(UTC).isoformat()})
        verify_entries(root, c["inputs"])
        report = sealed_read(out / "report.json")
        table = sealed_read(out / "observations.json")
        assert table["fingerprint"] == report["observations_fingerprint"]
        codes = sealed_read(root / c["selection"])["instrument_ids"]
        assert [row["instrument_id"] for row in table["rows"]] == codes
        history = {}
        for day, path in zip(c["prior20_dates"], c["prior20_paths"], strict=True):
            data = pd.read_parquet(root / path, columns=["instrument_id", "amount"])
            history[day] = dict(zip(data.instrument_id, data.amount, strict=True))
        frames = {k: pd.read_parquet(root / p) for k, p in c["context_paths"].items()}
        raw = frames["daily"].set_index("instrument_id").to_dict("index")
        limits = frames["limits"].set_index("instrument_id").to_dict("index")
        scores = pd.read_parquet(
            root / c["signals"],
            columns=["instrument_id", "trade_date", "S4-A"],
            filters=[("trade_date", "=", pd.Timestamp(c["decision_date"]))],
        )
        scores = scores.set_index("instrument_id")["S4-A"].to_dict()
        count = dict.fromkeys(report["counts"], 0)
        for record in table["rows"]:
            code, s = record["instrument_id"], record["session"]
            amounts = {d: scaled(history[d].get(code), 100) for d in c["prior20_dates"]}
            missing = [d for d, x in amounts.items() if x is None]
            mean = None if missing else int(sum(Fraction(x) for x in amounts.values()) // 20)
            assert s["prior20_amount_fen"] == mean
            assert record["prior20_unknown_dates"] == missing
            assert s["prior20_sessions"] == 20 - len(missing)
            assert s["prior20_asof"] == c["decision_date"]
            assert s["execution_date"] == s["evidence_date"] == c["execution_date"]
            assert s["next_session"] == c["next_session"]
            assert s["calendar_verified"] is True
            source = raw.get(code, {})
            p = {
                name: scaled(source.get(name), 100, 1) for name in ("open", "close", "low", "high")
            }
            valid = all(x is not None for x in p.values())
            if valid:
                valid = p["low"] <= p["open"] <= p["high"] and p["low"] <= p["close"] <= p["high"]
            for key in p:
                assert record["raw_bar"][key + "_fen"] == (p[key] if valid else None)
            for key in ("close", "low", "high"):
                target = "raw_close_fen" if key == "close" else key + "_fen"
                assert s[target] == (p[key] if valid else None)
            assert s["session_amount_fen"] == scaled(source.get("amount"), 100)
            assert s["session_volume_shares"] == scaled(source.get("volume"), 1)
            for name in ("up_limit", "down_limit"):
                assert s[name + "_fen"] == scaled(limits.get(code, {}).get(name), 100, 1)
            relation = bool(
                valid
                and s["up_limit_fen"] is not None
                and s["down_limit_fen"] is not None
                and s["down_limit_fen"] <= p["low"] <= p["high"] <= s["up_limit_fen"]
            )
            assert record["bar_limit_relation_valid"] == relation
            score = scores[code]
            expected_score = score if math.isfinite(score) else None
            assert record["copied_S4_A"] == expected_score
            for name in ("st", "suspensions"):
                f = frames[name][frames[name].instrument_id.eq(code)].astype(object)
                expected = json.loads(
                    json.dumps(f.where(pd.notna(f), None).to_dict("records"), default=str)
                )
                assert record[name + "_source_records"] == expected
            assert all(
                s[k] is None
                for k in (
                    "market_open",
                    "corporate_actions_processed",
                    "rules",
                    "fees",
                    "participation",
                )
            )
            assert (
                record["execution_authority"] is record["historical_performance_eligible"] is False
            )
            count["grid"] += 1
            count["copied_signal_known"] += expected_score is not None
            count["valid_raw_ohlc"] += valid
            count["complete_adv20"] += mean is not None
            count["consistent_bar_limits"] += relation
            count["st_codes"] += bool(record["st_source_records"])
            count["suspension_codes"] += bool(record["suspensions_source_records"])
        assert count == report["counts"]
        verify_entries(root, c["inputs"])
        proof = atomic_seal(
            out / "independent_proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "checked_rows": len(codes),
                "counts_reconciled": True,
                "method": "independent Fraction conversions and scalar sum; same agent",
                "seconds": time.monotonic() - started,
                "peak_rss_bytes": peak_rss_bytes(),
                "performance_evidence": False,
                "execution_authority": False,
            },
        )
        print(json.dumps({"fingerprint": proof["fingerprint"], "checked_rows": len(codes)}))


if __name__ == "__main__":
    main(Path.cwd())
