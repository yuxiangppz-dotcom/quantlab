"""Reconcile the one raw F7 source batch without calling its intake/profiler."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from quantlab.data.margin_intake import OUTPUT, load_contract
from quantlab.data.program_intake import now
from quantlab.research.alpha158_store import atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries


def prove(root):
    started = time.monotonic()
    binding = InputBinding(root)
    head = code_binding(root, binding)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        atomic_seal(out / "proof_intent.json", {"at": now(), "source_head": head, "attempt": 1})
        config = load_contract(root)
        report = sealed_read(out / "report.json")
        start = sealed_read(out / "started.json")
        assert report["identity"] == start["identity"]
        assert report["requests_attempted"] == report["requests_limit"] == 256
        assert report["stop_reason"] == "request_scope_exhausted"
        days = {d.replace("-", "") for d in config["source_dates"]}
        codes = config["codes"]
        assert {p.name for p in (out / "attempts").iterdir()} == {"margin_" + c for c in codes}
        total_rows, total_bytes, nulls, negatives, nonempty = 0, 0, 0, 0, 0
        statuses, coverage = Counter(), []
        prior_at = datetime.fromisoformat(start["at"])
        for code in codes:
            p = out / "attempts" / ("margin_" + code)
            intent, result = sealed_read(p / "intent.json"), sealed_read(p / "result.json")
            assert intent["request"] == {
                "id": "margin_" + code,
                "row_cap": 6000,
                "parameters": {
                    "api_name": "margin_detail",
                    "params": {"ts_code": code, "start_date": "20191224", "end_date": "20241230"},
                    "fields": "ts_code,trade_date,rzye",
                },
            }
            assert result["intent_fingerprint"] == intent["fingerprint"]
            assert result["identity"] == intent["identity"] == report["identity"]
            sent, received = (
                datetime.fromisoformat(intent["at"]),
                datetime.fromisoformat(result["at"]),
            )
            assert sent.utcoffset() is not None and received.utcoffset() is not None
            assert prior_at <= sent <= received
            prior_at = received
            raw = (p / "response.body").read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            assert result["artifacts"]["response.body"] == {"bytes": len(raw), "sha256": digest}
            assert result["wire_sha256"] == digest and result["received_bytes"] == len(raw)
            assert result["raw_redacted"] is False and result["transport_status"] == "received"
            obj = json.loads(raw)
            assert type(obj["code"]) is int and obj["code"] == 0
            data = obj["data"]
            fields = data["fields"]
            assert len(fields) == 3 and set(fields) == {"ts_code", "trade_date", "rzye"}
            rows = [dict(zip(fields, r, strict=True)) for r in data["items"]]
            observed, row_nulls, row_negatives = set(), 0, 0
            for row in rows:
                assert row["ts_code"] == code and row["trade_date"] in days
                assert row["trade_date"] not in observed
                observed.add(row["trade_date"])
                value = row["rzye"]
                if value is None:
                    row_nulls += 1
                else:
                    assert type(value) in (int, float) and math.isfinite(value)
                    row_negatives += value < 0
            for container in (obj, data):
                assert not container.get("has_more") and not container.get("truncated")
                assert container.get("total") in (None, len(rows))
            assert len(rows) < 6000
            status = "nonempty" if rows else "empty"
            assert result["status"] == status and result["rows"] == len(rows)
            assert result["null_rzye_rows"] == row_nulls
            assert result["negative_rzye_rows"] == row_negatives
            assert result["duplicate_key_rows"] == 0 and result["raw_unit"] == "CNY"
            assert result["date_range"] == ([min(observed), max(observed)] if rows else None)
            assert result["historical_pit_certified"] is result["complete_history"] is False
            statuses[status] += 1
            total_rows += len(rows)
            total_bytes += len(raw)
            nulls += row_nulls
            negatives += row_negatives
            nonempty += bool(rows)
            coverage.append(
                {
                    "instrument_id": code,
                    "observed_dates": len(observed),
                    "unobserved_source_dates": len(days) - len(observed),
                    "null_rzye_rows": row_nulls,
                    "negative_rzye_rows": row_negatives,
                }
            )
        assert report["status_counts"] == dict(statuses)
        assert report["returned_rows"] == total_rows
        assert report["charged_body_bytes"] == total_bytes
        binding.check()
        verify_entries(root, config["inputs"])
        return atomic_seal(
            out / "proof.json",
            {
                "at": now(),
                "source_head": head,
                "report_fingerprint": report["fingerprint"],
                "requests_checked": 256,
                "raw_rows_checked": total_rows,
                "raw_bytes_checked": total_bytes,
                "nonempty_codes": nonempty,
                "empty_codes": 256 - nonempty,
                "null_rzye_rows": nulls,
                "negative_rzye_rows": negatives,
                "coverage": coverage,
                "method": (
                    "independent raw-json rows, request/date ordering, hashes and scalar counts; "
                    "same agent"
                ),
                "historical_pit_certified": False,
                "margin_target_eligibility_certified": False,
                "performance_evidence": False,
                "execution_authority": False,
                "seconds": time.monotonic() - started,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        )


if __name__ == "__main__":
    print(prove(Path.cwd())["fingerprint"])
