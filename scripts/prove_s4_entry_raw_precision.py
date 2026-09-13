"""Independent direct raw-number/old-source reconciliation for the one S4 intake."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from quantlab.data.entry_raw_precision import OUTPUT, load_contract
from quantlab.data.program_intake import now
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result
        result[key] = value
    return result


def scalar_units(value, scale, positive):
    if value is None:
        return None, "missing"
    assert type(value) in (int, Decimal)
    d = Decimal(value)
    assert d.is_finite()
    sign, digits, exponent = d.as_tuple()
    if abs(exponent) > 100 or d > Decimal(10**15) / scale:
        return None, "out_of_range"
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    numerator = (-1 if sign else 1) * coefficient * scale
    denominator = 1
    if exponent >= 0:
        numerator *= 10**exponent
    else:
        denominator = 10 ** (-exponent)
    if numerator < 0 or (positive and numerator == 0):
        return None, "invalid_sign"
    if numerator > 10**15 * denominator:
        return None, "out_of_range"
    if numerator % denominator:
        return None, "off_integer_grid"
    return numerator // denominator, None


def check(root, c, saved, report, projection, proposals):
    out = root / OUTPUT
    observed_names = {p.name for p in (out / "attempts").iterdir()}
    expected_names = {
        f"daily_{k['instrument_id']}_{k['trade_date'].replace('-', '')}" for k in c["keys"]
    }
    assert observed_names == expected_names and len(observed_names) == 13
    assert len(projection["rows"]) == 13
    sources = {}
    for day, path in c["canonical_paths"].items():
        f = pd.read_parquet(root / path, use_threads=False)
        assert f.instrument_id.is_unique and f.instrument_id.notna().all()
        assert pd.to_datetime(f.trade_date).eq(pd.Timestamp(day)).all()
        sources[day] = {r.instrument_id: r._asdict() for r in f.itertuples(index=False)}
    prior = datetime.fromisoformat(sealed_read(out / "started.json")["at"])
    counts = Counter()
    rawbytes = totalrows = resolved_volume = 0
    for key, r in zip(c["keys"], projection["rows"], strict=True):
        code, day = key["instrument_id"], key["trade_date"]
        compact = day.replace("-", "")
        name = f"daily_{code}_{compact}"
        p = out / "attempts" / name
        intent, result = sealed_read(p / "intent.json"), sealed_read(p / "result.json")
        assert intent["request"] == {
            "id": name,
            "row_cap": 6000,
            "parameters": {
                "api_name": "daily",
                "params": {"ts_code": code, "start_date": compact, "end_date": compact},
                "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
            },
        }
        assert intent["identity"] == result["identity"] == report["identity"]
        assert result["intent_fingerprint"] == intent["fingerprint"]
        sent, received = datetime.fromisoformat(intent["at"]), datetime.fromisoformat(result["at"])
        assert prior <= sent <= received
        prior = received
        raw = (p / "response.body").read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        assert result["artifacts"]["response.body"] == {"sha256": sha, "bytes": len(raw)}
        assert result["wire_sha256"] == sha and result["received_bytes"] == len(raw)
        assert result["raw_redacted"] is False and result["transport_status"] == "received"
        obj = json.loads(raw, parse_float=Decimal, object_pairs_hook=unique_object)
        assert type(obj["code"]) is int and obj["code"] == 0
        data = obj["data"]
        fields = data["fields"]
        assert len(fields) == 8 and set(fields) == {
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "vol",
            "amount",
        }
        assert len(data["items"]) <= 1
        for container in (obj, data):
            assert not container.get("has_more") and not container.get("truncated")
            assert container.get("total") in (None, len(data["items"]))
        row = dict(zip(fields, data["items"][0], strict=True)) if data["items"] else None
        if row:
            assert row["ts_code"] == code and row["trade_date"] == compact
        status = "nonempty" if row else "empty"
        assert result["status"] == status == r["source_status"]
        assert result["rows"] == len(data["items"])
        for field, value in key.items():
            assert r[field] == value
        original = sources[day].get(code)
        assert r["canonical_row_present"] == (original is not None)
        for field, scale in [
            ("open", 100),
            ("high", 100),
            ("low", 100),
            ("close", 100),
            ("vol", 100),
            ("amount", 100000),
        ]:
            value = None if row is None else row[field]
            normalized, reason = scalar_units(value, scale, field not in ("vol", "amount"))
            assert r["raw_number_text"][field] == (None if value is None else str(value))
            assert r["raw_normalized"][field] == normalized and r["unknown_units"][field] == reason
            old = None if original is None else original["volume" if field == "vol" else field]
            genuine = (
                isinstance(old, (int, float)) and not isinstance(old, bool) and math.isfinite(old)
            )
            expected_exact = (
                None
                if not genuine or normalized is None
                else Decimal(str(old)) * (1 if field == "vol" else 100) == normalized
            )
            expected_float = (
                None
                if not genuine or value is None
                else float(value) * (100 if field == "vol" else 1000 if field == "amount" else 1)
                == old
            )
            assert r["comparisons"][field] == {
                "saved_canonical_text": str(old) if genuine else None,
                "exact_value_equal": expected_exact,
                "float_normalization_reproduces_saved": expected_float,
            }
        v = r["raw_normalized"]
        complete = all(v[k] is not None for k in ("open", "high", "low", "close"))
        assert r["raw_ohlc_consistent"] == (
            v["low"] <= v["open"] <= v["high"] and v["low"] <= v["close"] <= v["high"]
            if complete
            else None
        )
        assert r["automatically_admitted"] is False and r["historical_pit_certified"] is False
        resolved_volume += key["purpose"] == "execution_volume_precision" and v["vol"] is not None
        counts[status] += 1
        totalrows += len(data["items"])
        rawbytes += len(raw)
    assert report["status_counts"] == dict(counts)
    assert report["charged_body_bytes"] == rawbytes and report["returned_rows"] == totalrows
    assert len(proposals["rows"]) == 20
    originals = [r for r in saved["plan_plan"]["rows"] if r["selected_raw_proposal"]]
    for original, new in zip(originals, proposals["rows"], strict=True):
        assert new == {
            "instrument_id": original["instrument_id"],
            "proposal_rank": original["proposal_rank"],
            "nominal_quantity": original["nominal_quantity"],
            "original_required_unknowns": original["required_unknowns"],
            "new_source_keys": [
                k for k in c["keys"] if k["instrument_id"] == original["instrument_id"]
            ],
            "automatically_admitted": False,
        }
    return {
        "raw_rows_checked": totalrows,
        "raw_bytes_checked": rawbytes,
        "status_counts": dict(counts),
        "exact_new_volume_rows": resolved_volume,
    }


def prove(root):
    binding = InputBinding(root)
    head = code_binding(root, binding)
    assert (
        head
        == subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    )
    c, saved = load_contract(root)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, c["resources"])
        budget.check()
        intent = atomic_seal(
            out / "proof_intent.json", {"at": now(), "source_head": head, "attempt": 1}
        )
        try:
            with budget.watchdog():
                report = sealed_read(out / "report.json")
                projection = sealed_read(out / "reconciliation.json")
                proposals = sealed_read(out / "proposals.json")
                assert report["source_head"] == projection["source_head"] == head
                assert (
                    report["stop_reason"] == "request_scope_exhausted"
                    and report["requests_attempted"] == 13
                )
                assert report["reconciliation_fingerprint"] == projection["fingerprint"]
                assert report["proposals_fingerprint"] == proposals["fingerprint"]
                assert proposals["parent_plan_fingerprint"] == saved["plan_plan"]["fingerprint"]
                assert (
                    report["historical_pit_certified"]
                    is report["execution_authority"]
                    is report["performance_evidence"]
                    is False
                )
                summary = check(root, c, saved, report, projection, proposals)
                binding.check()
                verify_entries(root, c["inputs"])
                return atomic_seal(
                    out / "proof.json",
                    {
                        "at": now(),
                        "source_head": head,
                        "report_fingerprint": report["fingerprint"],
                        "intent_fingerprint": intent["fingerprint"],
                        "requests_checked": 13,
                        "proposals_checked": 20,
                        **summary,
                        "method": (
                            "independent raw Decimal digit/exponent integer division "
                            "and old-source comparisons; same agent"
                        ),
                        "historical_pit_certified": False,
                        "performance_evidence": False,
                        "execution_authority": False,
                        "seconds": time.monotonic() - budget.started,
                        "peak_rss_bytes": peak_rss_bytes(),
                        "generated_bytes": budget.check(),
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "proof_failed.json",
                {
                    "at": now(),
                    "error_type": type(exc).__name__,
                    "intent_fingerprint": intent["fingerprint"],
                },
            )
            raise


if __name__ == "__main__":
    print(prove(Path.cwd())["fingerprint"])
