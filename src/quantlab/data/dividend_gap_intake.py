"""One finite raw dividend gap batch; existing raw history is never refetched."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime

from quantlab.data.dividend_raw import FIELDS, WireClient, inspect_response, strict_json
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.program_intake import Journal, acquire, now
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/dividend_gap_intake_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/dividend_gap_intake"
CODES = (
    "000301.SZ",
    "000333.SZ",
    "000548.SZ",
    "000753.SZ",
    "000760.SZ",
    "000786.SZ",
    "002366.SZ",
    "002462.SZ",
    "002474.SZ",
    "002770.SZ",
    "300016.SZ",
    "300105.SZ",
    "300261.SZ",
    "300409.SZ",
    "300471.SZ",
    "600075.SH",
    "600125.SH",
    "600240.SH",
    "600262.SH",
    "600298.SH",
    "600305.SH",
    "600308.SH",
    "600371.SH",
    "600497.SH",
    "601009.SH",
    "601126.SH",
    "601139.SH",
    "601318.SH",
    "601579.SH",
    "601668.SH",
    "603181.SH",
)
RESOURCES = {
    "max_generated_bytes": 128 * 1024**2,
    "reserve_host_D_bytes": 8 * 1024**3,
    "max_rss_bytes": 2 * 1024**3,
    "max_wakeup_seconds": 600,
    "next_partition_time_reserve_seconds": 60,
}


def requests_for():
    return [
        {
            "id": f"dividend_{code}",
            "row_cap": 2000,
            "parameters": {
                "api_name": "dividend",
                "params": {"ts_code": code},
                "fields": ",".join(FIELDS),
            },
        }
        for code in CODES
    ]


def profile(raw, request):
    result = inspect_response(
        raw, request["parameters"]["params"]["ts_code"], row_cap=request["row_cap"]
    )
    result.pop("server_message", None)
    if "profile" not in result:
        return result
    statistics = result["profile"]
    # The old helper's relevance window belongs to another task, not this one.
    statistics.pop("rows_with_date_in_required_window", None)
    data = strict_json(raw)["data"]
    relevant = 0
    for values in data["items"]:
        row = dict(zip(data["fields"], values, strict=True))
        event_days = []
        for key in ("record_date", "ex_date", "pay_date", "div_listdate"):
            value = row.get(key)
            if isinstance(value, str) and len(value) == 8 and value.isdigit():
                try:
                    event_days.append(datetime.strptime(value, "%Y%m%d").date().isoformat())
                except ValueError:
                    pass
        relevant += any("2020-01-01" <= day <= "2024-12-31" for day in event_days)
    statistics["rows_with_event_date_2020_2024"] = relevant
    statistics["relevance_window"] = ["2020-01-01", "2024-12-31"]
    if result["status"] in {"empty", "nonempty"} and any(statistics["malformed_counts"].values()):
        result["status"] = "schema_error"
    result["cashflow_eligible"] = False
    result["raw_units"] = "cash and stock distributions per share;base_share10000shares"
    return result


def validate_population(selection, parent, coverage):
    cohort = selection["instrument_ids"]
    existing = {x["instrument_id"] for x in parent["per_code"]}
    if (
        len(cohort) != 256
        or len(set(cohort)) != 256
        or tuple(sorted(set(cohort) - existing)) != CODES
        or tuple(coverage["missing_codes"]) != CODES
        or coverage["report_fingerprint"] != parent["fingerprint"]
        or len(set(cohort) & existing) != 225
    ):
        raise DataValidationError("fixed cohort/dividend gap changed; no refetch or reselection")


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "dividend_gap_intake_v1",
        "output": OUTPUT,
        "endpoint": "https://api.tushare.pro",
        "codes": list(CODES),
        "max_requests": 31,
        "max_retries": 0,
        "requests_per_minute": 100,
        "per_response_bytes": 2 * 1024**2,
        "max_body_bytes": 64 * 1024**2,
        "resources": RESOURCES,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
        "canonical_writes_authorized": False,
        "execution_authority": False,
        "historical_pit_certified": False,
    }
    if any(config.get(k) != v or type(config.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed dividend gap contract")
    if any(type(config["resources"].get(k)) is not int for k in RESOURCES):
        raise DataValidationError("dividend gap resource limits must be exact integers")
    paths = (
        "selection_path",
        "parent_report_path",
        "coverage_path",
        "audit_report_path",
        "audit_proof_path",
        "source_receipt_path",
        "task_card_path",
    )
    if any(config.get(key) not in config["inputs"] for key in paths):
        raise DataValidationError("required dividend gap evidence is not bound")
    verify_entries(root, config["inputs"])
    parent = sealed_read(root / config["parent_report_path"])
    coverage = sealed_read(root / config["coverage_path"])
    selection = sealed_read(root / config["selection_path"])
    audit = sealed_read(root / config["audit_report_path"])
    proof = sealed_read(root / config["audit_proof_path"])
    if proof["report_fingerprint"] != audit["fingerprint"]:
        raise DataValidationError("dividend gap prerequisite proof mismatch")
    validate_population(selection, parent, coverage)
    return config


def run(root, *, client_factory=WireClient):
    config = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(
            ["git", "rev-parse", "@{upstream}"],
            cwd=root,
            text=True,
        ).strip()
    ):
        raise DataValidationError("dividend gap intake requires clean pushed source")
    identity = canonical_payload_fingerprint(
        {"source_head": head, "config_sha256": _sha(root / CONFIG)}
    )
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("dividend gap segment already consumed; no restart")
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise DataValidationError("Tushare credential unavailable")
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=16 * 1024**2, projected_memory=128 * 1024**2)
        journal = Journal(out, config, requests_for(), identity, inspector=profile)
        atomic_seal(
            out / "started.json",
            {
                "at": now(),
                "identity": identity,
                "source_head": head,
                "code_inputs": binding.entries,
                "actual_segment_attempt": 1,
            },
        )
        client = client_factory(token, config["endpoint"])
        try:
            with budget.watchdog():
                stop = acquire(
                    journal, budget, client, gap_seconds=60.1 / config["requests_per_minute"]
                )
        except Exception as exc:
            stop = "runtime_or_budget_error"
            atomic_seal(out / "failed.json", {"at": now(), "error_type": type(exc).__name__})
        finally:
            client.close()
        binding.check()
        load_contract(root)
        return atomic_seal(
            out / "report.json",
            {
                "at": now(),
                "source_head": head,
                "identity": identity,
                "config_sha256": _sha(root / CONFIG),
                "stop_reason": stop,
                **journal.summary(),
                "actual_segment_attempts": 1,
                "complete_history": False,
                "strategy_input_eligible": False,
                "performance_evidence": False,
                "cashflow_eligible": False,
                "canonical_writes": False,
                "new_formula_identities_used": 0,
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "generated_bytes": budget.check(),
                },
            },
        )
