"""Independent saved-row/interval proof; never calls a catalogue resolver."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.alpha158_store import atomic_seal, exclusive_job
from quantlab.research.closing_quantity import (
    CONFIG,
    MANIFEST,
    OUTPUT,
    VERSION,
    fixed_sessions,
    load_catalogue,
)
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.rule_evidence import SCOPES


def main():
    root = Path(__file__).resolve().parents[1]
    c = load_catalogue(root)
    report = sealed_read(root / OUTPUT / "report.json")
    if (report["config_sha256"], report["manifest_sha256"]) != (
        _sha(root / CONFIG),
        _sha(root / MANIFEST),
    ):
        raise DataValidationError("report source identity changed")
    days = fixed_sessions(root, c)
    expected_keys = {(str(day), exchange, board) for day in days for exchange, board in SCOPES}
    keys = {(r["date"], r["exchange"], r["board"]) for r in report["rows"]}
    assert keys == expected_keys and len(report["rows"]) == 2904
    prior = sealed_read(root / "data/products/rule_evidence/rule_catalogue_v3_20260913/report.json")
    prior_rows = {(r["session"], r["exchange"], r["board"]): r for r in prior["rows"]}
    rules = (*c.parent.parent._book.rules, *c.parent.earlier._book.rules)
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        atomic_seal(
            root / OUTPUT / "proof_started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "proof_attempt": 1,
            },
        )
        for row in report["rows"]:
            matches = [
                r
                for r in rules
                if r.exchange == row["exchange"]
                and r.board == row["board"]
                and str(r.effective_from) <= row["date"] <= str(r.effective_to)
            ]
            assert len(matches) == 1
            original = json.loads(json.dumps(asdict(matches[0]), default=str))
            old = prior_rows[(row["date"], row["exchange"], row["board"])]
            assert row["parent_id"] == original["rule_id"] == old["catalogue_rule"]
            assert row["values"] == old["catalogue_values"]
            assert row["values"] == {
                "price_tick": "0.01",
                "buy_min_quantity": 200 if row["board"] == "STAR" else 100,
                "buy_quantity_step": 1 if row["board"] == "STAR" else 100,
                "sell_min_quantity": 200 if row["board"] == "STAR" else 100,
                "sell_quantity_step": 1 if row["board"] == "STAR" else 100,
                "max_limit_quantity": {"STAR": 100000, "CHINEXT": 300000}.get(
                    row["board"], 1000000
                ),
                "allow_full_odd_lot_exit": True,
                "sellability_lag_sessions": 1,
            }
            assert row["parent_payload"] == canonical_payload_fingerprint(original)
            assert row["effective_from"] == original["effective_from"]
            assert row["effective_to"] == original["effective_to"]
            original["rule_id"] += ":closing-subset-v1"
            original["version"] += ":" + VERSION
            original["supported_sessions"] = ["closing_auction"]
            original["limitations"] += [
                *c.contract["limitations"],
                *[
                    f"{r['source_id']}: {r['clause']}"
                    for r in c.contract["provisions"][row["parent_id"]]
                ],
            ]
            assert row["closing_id"] == original["rule_id"]
            assert row["closing_payload"] == canonical_payload_fingerprint(original)
        assert report["execution_authority"] is False and report["performance_evidence"] is False
        result = atomic_seal(
            root / OUTPUT / "proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "report_fingerprint": report["fingerprint"],
                "all_checks_passed": True,
                "checked_rows": len(keys),
                "method": "independent interval selection and saved old report; no resolver",
                "execution_authority": False,
                "performance_evidence": False,
            },
        )
        print(json.dumps(result))


if __name__ == "__main__":
    main()
