"""Read only the reviewed ETF evidence; readiness is not strategy admission."""

from __future__ import annotations

import json
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read

CONFIG = "config/etf_event_progress_v1.json"
AUTHORITY_FLAGS = (
    "historical_pit_certified",
    "performance_evidence",
    "execution_authority",
    "candidate_promotion_eligible",
    "canonical_writes",
)
PROOF_CHECKS = (
    "bound_source_hashes_verified",
    "cash_equivalence_independently_reconciled",
    "annual_unit_conversion_verified",
    "coverage_independently_reconciled",
)


def _within(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise DataValidationError("ETF evidence path leaves its bound directory")
    return path


def _verify(root, entries):
    for name, entry in entries.items():
        path = _within(root, name)
        if _sha(path) != entry["sha256"] or (
            "bytes" in entry and path.stat().st_size != entry["bytes"]
        ):
            raise DataValidationError("ETF evidence artifact changed")


def read_etf_progress(root: Path):
    """No provider, worker, return calculation or account write is reachable here."""
    path = root / CONFIG
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    if config["schema"] != "etf_event_progress_v1" or any(
        config[key] != 0
        for key in (
            "provider_calls_authorized",
            "economic_paths_authorized",
            "model_fits_authorized",
        )
    ):
        raise DataValidationError("unreviewed ETF progress contract")
    out = _within(root, config["output"])
    if not (out / "report.json").exists() or not (out / "independent_proof.json").exists():
        return None
    _verify(root, config["inputs"])
    report = sealed_read(out / "report.json")
    proof = sealed_read(out / "independent_proof.json")
    intent = sealed_read(out / "started.json")
    parent = sealed_read(_within(root, config["parent_report_path"]))
    parent_proof = sealed_read(_within(root, config["parent_proof_path"]))
    if (
        report.get("schema") != "etf_event_audit_v1"
        or report["fingerprint"] != config["report_fingerprint"]
        or proof["fingerprint"] != config["proof_fingerprint"]
        or proof["report_fingerprint"] != report["fingerprint"]
        or proof["intent_fingerprint"] != intent["fingerprint"]
        or report["intent_fingerprint"] != intent["fingerprint"]
        or report["parent_etf_report_fingerprint"] != parent["fingerprint"]
        or parent_proof["report_fingerprint"] != parent["fingerprint"]
        or any(
            payload.get(key) is not False for payload in (report, proof) for key in AUTHORITY_FLAGS
        )
        or any(proof.get(key) is not True for key in PROOF_CHECKS)
        or any(
            report.get(key) != 0
            for key in ("provider_calls_used", "economic_paths_used", "model_fits_used")
        )
        or report.get("new_funding_feature_ids") != []
        or any(
            row.get(key) is not False
            for row in report["instruments"]
            for key in ("s1_ab_eligible", "f8_eligible", "f8_is_required_for_s1_ab")
        )
    ):
        raise DataValidationError("unverified ETF evidence or unsupported authority")
    _verify(root, intent["inputs"])
    _verify(out, report["artifacts"])
    _verify(out, proof["artifacts"])
    return report
