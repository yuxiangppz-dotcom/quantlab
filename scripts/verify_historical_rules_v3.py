"""Once-only saved-row proof using interval selection, not the catalogue resolver."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / "data/products/rule_evidence/rule_catalogue_v3_20260913"
    report = sealed_read(out / "report.json")
    config = json.loads((root / "config/historical_rule_catalogue_v3.json").read_text())
    manifest = json.loads((root / "config/historical_rule_sources_v3.json").read_text())
    budget = Budget(out, config["resources"])
    with exclusive_job(root / "data/runtime/research/heavy_job"), budget.watchdog():
        budget.check()
        atomic_seal(out / "proof_started.json", {"at": datetime.now(UTC).isoformat(), "attempt": 1})
        verify_entries(root, manifest["files"])
        assert _sha(root / "config/historical_rule_catalogue_v3.json") == report["catalogue_sha256"]
        assert (
            _sha(root / "config/historical_rule_sources_v3.json")
            == report["source_manifest_sha256"]
        )
        parent = json.loads((root / "config/historical_rule_catalogue_v2.json").read_text())
        old_report = sealed_read(
            root / "data/products/rule_evidence/rule_catalogue_v2_20260911/report.json"
        )
        originals = {r["id"]: r for r in parent["intervals"]}
        intervals = copy.deepcopy(parent["intervals"])
        for spec in config["additions"]:
            r = copy.deepcopy(originals[spec["parent_interval_id"]])
            r.update({k: v for k, v in spec.items() if k != "parent_interval_id"})
            intervals.append(r)
        inventory = sealed_read(root / config["calendar"]["path"])
        sessions = [s for s in inventory["sessions"] if "2020-01-01" <= s <= "2026-09-10"]
        assert len(sessions) == 1623
        expected_ids = {
            (e, b, s)
            for e, b in [("SSE", "MAIN"), ("SSE", "STAR"), ("SZSE", "MAIN"), ("SZSE", "CHINEXT")]
            for s in sessions
        }
        key = lambda r: (r["exchange"], r["board"], r["session"])  # noqa: E731
        rows = report["rows"]
        assert len(rows) == 6492 and {key(r) for r in rows} == expected_ids
        for row in rows:
            hits = [
                r
                for r in intervals
                if (r["exchange"], r["board"]) == key(row)[:2]
                and r["from"] <= row["session"] <= r["through"]
            ]
            assert len(hits) <= 1
            assert row["catalogue_rule"] == (hits[0]["id"] if hits else None)
            assert row["catalogue_values"] == (hits[0]["values"] if hits else None)
        assert {key(r): r for r in rows if r["session"] >= "2023-01-01"} == {
            key(r): r for r in old_report["rows"]
        }
        assert all(r["catalogue_rule"] is not None for r in rows if r["session"][:4] == "2022")
        budget.check()
        proof = atomic_seal(
            out / "proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "checked_rows": len(rows),
                "unchanged_parent_rows": len(old_report["rows"]),
                "all_checks_passed": True,
                "method": "same-agent saved-row interval selector; no resolver call or rerun",
                "execution_authority": False,
                "economic_paths": 0,
                "provider_requests": 0,
            },
        )
        print(json.dumps(proof))


if __name__ == "__main__":
    main()
