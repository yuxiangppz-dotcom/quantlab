"""Saved raw snapshot and rational ranks; never calls the production scorer."""

import json
import math
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from quantlab.research.alpha158_store import atomic_seal, exclusive_job
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.s2_prospective import CONFIG, OUTPUT


def rank(value, values):
    return sum(x < value for x in values) + Fraction(sum(x == value for x in values) + 1, 2)


def valid(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def main():
    root = Path(__file__).resolve().parents[1]
    c = json.loads((root / CONFIG).read_text())
    verify_entries(root, c["inputs"])
    report = sealed_read(root / OUTPUT / "observation.json")
    selection = sealed_read(root / c["selection_path"])
    assert report["config_sha256"] == _sha(root / CONFIG)
    assert report["selection_fingerprint"] == selection["fingerprint"]
    received = datetime.fromisoformat(c["source_received_at"])
    created = datetime.fromisoformat(report["created_at"])
    assert received <= created < datetime(2026, 9, 14, 1, 30, tzinfo=UTC)
    assert report["source_received_at"] == c["source_received_at"]
    data = json.loads((root / c["raw_path"]).read_bytes())["data"]
    raw = {row[0]: dict(zip(data["fields"], row, strict=True)) for row in data["items"]}
    codes = selection["instrument_ids"]
    assert [r["instrument_id"] for r in report["records"]] == codes
    current = {code: raw.get(code, {}) for code in codes}
    sizes = [row["circ_mv"] for row in current.values() if valid(row.get("circ_mv"))]
    groups = {
        code: min(4, int((rank(row["circ_mv"], sizes) - 1) * 5 / len(sizes)))
        for code, row in current.items()
        if len(sizes) >= 20 and valid(row.get("circ_mv"))
    }
    grouped = {
        g: [
            code
            for code in codes
            if groups.get(code) == g
            and valid(current[code].get("pe_ttm"))
            and valid(current[code].get("pb"))
        ]
        for g in range(5)
    }
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        atomic_seal(
            root / OUTPUT / "proof_started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "observation_fingerprint": report["fingerprint"],
                "proof_attempt": 1,
            },
        )
        known, reasons = 0, {}
        for record in report["records"]:
            code = record["instrument_id"]
            source = current[code]
            for field in ("pe_ttm", "pb", "circ_mv"):
                assert record[field] == source.get(field)
            group = groups.get(code)
            assert record["size_group"] == group
            members = grouped.get(group, [])
            if not valid(source.get("circ_mv")):
                reason = "size_unknown_or_nonpositive"
            elif len(sizes) < 20:
                reason = "fewer_than20_size_observations"
            elif not valid(source.get("pe_ttm")) or not valid(source.get("pb")):
                reason = "valuation_unknown_or_nonpositive"
            elif len(members) < 20:
                reason = "fewer_than20_valid_values_in_size_group"
            else:
                reason = "observed_value_score"
            assert record["reason"] == reason
            expected_known = reason == "observed_value_score"
            assert record["score_known"] is expected_known
            reasons[reason] = reasons.get(reason, 0) + 1
            if not expected_known:
                assert record["score"] is None
                continue
            exact = sum(
                rank(-source[field], [-current[x][field] for x in members])
                for field in ("pe_ttm", "pb")
            ) / (2 * len(members))
            assert abs(record["score"] - float(exact)) < 1e-14
            known += 1
        assert known == report["scores_known"] and reasons == report["reasons"]
        assert report["correlation"] is None and report["future_exit_date"] is None
        for flag in (
            "future_labels_evaluated",
            "historical_pit_certified",
            "performance_evidence",
            "historical_performance_eligible",
            "execution_authority",
            "future_calendar_complete",
        ):
            assert report[flag] is False
        result = atomic_seal(
            root / OUTPUT / "proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "observation_fingerprint": report["fingerprint"],
                "checked_codes": len(codes),
                "scores_known": known,
                "all_checks_passed": True,
                "method": "same-agent rational pairwise ranks from saved original; no scorer call",
                "future_labels_evaluated": False,
                "execution_authority": False,
            },
        )
        print(json.dumps(result))


if __name__ == "__main__":
    main()
