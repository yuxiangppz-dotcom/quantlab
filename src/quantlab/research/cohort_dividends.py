"""Finite retrospective event-field checks; no entitlement or signal authority."""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.data.dividend_gap_intake import CODES
from quantlab.data.dividend_raw import FIELDS
from quantlab.data.models import DataValidationError
from quantlab.data.models import canonical_payload_fingerprint as fingerprint
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.dividend_event_run import OBS_SCHEMA, write_rows
from quantlab.research.dividend_events import EVENT_DATES, observations
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries

CONFIG = "config/cohort_dividend_readiness_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/cohort_dividend_readiness"
WINDOW = ("2020-01-01", "2024-12-31")
RESOURCES = {
    "max_generated_bytes": 256 * 1024**2,
    "reserve_host_D_bytes": 8 * 1024**3,
    "max_rss_bytes": 2 * 1024**3,
    "max_wakeup_seconds": 600,
    "next_partition_time_reserve_seconds": 60,
}
TERMS = (
    "cash_div",
    "cash_div_tax",
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "pay_date",
    "div_listdate",
)
STRUCTURAL_FLAGS = {
    "unknown_status",
    "record_after_ex",
    "pay_before_record",
    "listing_before_record",
    "share_components_unknown",
    "share_components_disagree",
    "cash_before_tax_unknown",
    "implemented_record_or_ex_unknown",
    "positive_cash_pay_date_unknown",
    "positive_shares_listing_date_unknown",
    "candidate_version_conflict",
}
EXTRA_STRINGS = (
    "availability_bound_source",
    "source_observed_at",
    "source_receipt_at",
    "observed_date_scope",
    "possible_event_group_id",
    "structural_blockers",
)
SCHEMA = pa.schema(
    list(OBS_SCHEMA)
    + [(k, pa.string()) for k in EXTRA_STRINGS]
    + [
        ("possible_event_conflict", pa.bool_()),
        ("structurally_ready", pa.bool_()),
    ]
)


def utc_stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError
        return stamp
    except (ValueError, TypeError) as exc:
        raise DataValidationError("missing or invalid UTC receipt time") from exc


def adapt_response(raw, receipt, intent, code, batch):
    """Pass a derived timestamp view to the existing lossless parser, not a new receipt."""
    if receipt["intent_fingerprint"] != intent["fingerprint"]:
        raise DataValidationError("dividend request/response identity changed")
    if batch == "legacy":
        parameters = intent["parameters"]
        bound = receipt.get("observed_at")
        bound_source = "wire_observed_at"
    elif batch == "gap":
        parameters = intent["request"]["parameters"]
        if (
            receipt.get("raw_redacted") is not False
            or receipt.get("body_count_complete") is not True
        ):
            raise DataValidationError("gap response lacks complete original bytes")
        bound = receipt.get("at")
        bound_source = "receipt_recorded_at"
        if utc_stamp(bound) < utc_stamp(intent.get("at")):
            raise DataValidationError("response recording precedes request")
    else:
        raise DataValidationError("unknown source batch")
    utc_stamp(bound)
    if parameters != {
        "api_name": "dividend",
        "params": {"ts_code": code},
        "fields": ",".join(FIELDS),
    }:
        raise DataValidationError("dividend request parameters changed")
    # Original sealed fingerprint continues to identify each response-row occurrence.
    view = {**receipt, "observed_at": bound}
    rows = observations(raw, view, code)
    for row in rows:
        row.update(
            availability_bound_source=bound_source,
            source_observed_at=receipt.get("observed_at"),
            source_receipt_at=receipt.get("at"),
        )
    return rows


def classify(rows):
    """Return all occurrences; possible identities never deduplicate economic events."""
    result = [dict(row) for row in rows]
    groups = defaultdict(list)
    for row in result:
        dates = [row[key] for key in EVENT_DATES if row[key]]
        row["observed_date_scope"] = (
            "in_window"
            if any(WINDOW[0] <= day <= WINDOW[1] for day in dates)
            else "no_observed_date_in_window"
            if dates
            else "no_valid_event_dates"
        )
        group = None
        if row["implementation_candidate"] and row["record_date"] and row["ex_date"]:
            group = fingerprint(
                [row[k] for k in ("instrument_id", "end_date", "record_date", "ex_date")]
            )
            groups[group].append(row)
        row["possible_event_group_id"] = group
    collisions = []
    conflict_ids = set()
    for group, members in sorted(groups.items()):
        variants = {fingerprint([row[k] for k in TERMS]) for row in members}
        if len(variants) > 1:
            conflict_ids.add(group)
            collisions.append(
                {
                    "possible_event_group_id": group,
                    "instrument_id": members[0]["instrument_id"],
                    "observation_ids": [row["observation_id"] for row in members],
                    "distinct_term_variants": len(variants),
                    "any_observed_date_in_window": any(
                        row["observed_date_scope"] == "in_window" for row in members
                    ),
                    "resolved": False,
                }
            )
    for row in result:
        flags = json.loads(row["quality_flags"])
        blockers = {f for f in flags if f in STRUCTURAL_FLAGS or f.startswith("malformed_")}
        if not row["implementation_candidate"]:
            blockers.add("not_implementation")
        row["possible_event_conflict"] = row["possible_event_group_id"] in conflict_ids
        if row["possible_event_conflict"]:
            blockers.add("possible_event_terms_conflict")
        row["structural_blockers"] = json.dumps(sorted(blockers))
        row["structurally_ready"] = not blockers
        if row["historical_pit_certified"] is not False or row["cashflow_eligible"] is not False:
            raise DataValidationError("observation authority cannot be upgraded")
    return result, collisions


def summarize(rows):
    counts = Counter()
    statuses, flags, blockers, provenance = Counter(), Counter(), Counter(), Counter()
    for row in rows:
        counts["occurrences"] += 1
        counts["implementation_occurrences"] += row["implementation_candidate"]
        counts["structurally_ready_occurrences"] += row["structurally_ready"]
        counts[row["observed_date_scope"]] += 1
        counts["possible_event_conflict_occurrences"] += row["possible_event_conflict"]
        statuses[row["normalized_status"] or "unknown"] += 1
        flags.update(json.loads(row["quality_flags"]))
        blockers.update(json.loads(row["structural_blockers"]))
        provenance[row["availability_bound_source"]] += 1
    return {
        "counts": dict(counts),
        "statuses": dict(statuses),
        "flags": dict(flags),
        "blockers": dict(blockers),
        "availability_provenance": dict(provenance),
        "distinct_contents": len({row["content_id"] for row in rows}),
        "exact_duplicate_excess": len(rows) - len({row["content_id"] for row in rows}),
        "candidate_conflict_groups": len(
            {row["candidate_group_id"] for row in rows if row["candidate_conflict"]}
        ),
        "structurally_ready_contents": len(
            {row["content_id"] for row in rows if row["structurally_ready"]}
        ),
    }


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "cohort_dividend_readiness_v1",
        "output": OUTPUT,
        "window": list(WINDOW),
        "instrument_count": 256,
        "occurrence_count": 15190,
        "source_body_byte_cap": 64 * 1024**2,
        "resources": RESOURCES,
        "provider_requests_authorized": 0,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
        "execution_authority": False,
        "historical_pit_certified": False,
        "cashflow_eligible": False,
    }
    if any(config.get(k) != v or type(config.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed cohort dividend contract")
    for key in RESOURCES:
        if type(config["resources"][key]) is not int:
            raise DataValidationError("resource cap must be an integer")
    paths, sources, entries = config["paths"], config["sources"], config["inputs"]
    required = set(paths.values()) | {
        s["folder"] + "/" + f
        for s in sources
        for f in ("response.body", "intent.json", "result.json")
    }
    if len(paths) != 6 or set(entries) != required or len(entries) != 774:
        raise DataValidationError("source manifest is not the fixed finite population")
    verify_entries(root, entries)
    if any((root / p).stat().st_size != e["bytes"] for p, e in entries.items()):
        raise DataValidationError("source byte count changed")
    selection = sealed_read(root / paths["selection"])["instrument_ids"]
    if len(selection) != 256 or [s["instrument_id"] for s in sources] != selection:
        raise DataValidationError("fixed cohort source order changed")
    if len(set(selection)) != 256 or sorted(
        s["instrument_id"] for s in sources if s["batch"] == "gap"
    ) != list(CODES):
        raise DataValidationError("cohort gap membership changed")
    parent = sealed_read(root / paths["old_report"])
    old = {r["instrument_id"]: r for r in parent["per_code"]}
    for source in sources:
        code = source["instrument_id"]
        prefix = "data/products/corporate_action_staging/dividend_targets_20260911/attempts"
        expected = (
            f"{prefix}/{code}/{old[code]['attempts']:02d}"
            if code in old
            else f"data/products/research_program/launch_20260912/dividend_gap_intake/"
            f"attempts/dividend_{code}"
        )
        if source["folder"] != expected or source["batch"] != ("legacy" if code in old else "gap"):
            raise DataValidationError("raw source selection changed")
    for key in ("old_proof", "new_report", "new_proof"):
        sealed_read(root / paths[key])
    if sum(e["bytes"] for p, e in entries.items() if p.endswith("/response.body")) > 64 * 1024**2:
        raise DataValidationError("source body budget exceeded")
    return config


def pushed_head(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    head = git("rev-parse", "HEAD")
    if git("status", "--porcelain") or head != git("rev-parse", "@{upstream}"):
        raise DataValidationError("actual staging requires clean pushed source")
    return head


def run(root):
    config = load_contract(root)
    head, config_sha = pushed_head(root), _sha(root / CONFIG)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        out.mkdir(parents=True, exist_ok=False)
        budget = Budget(out, RESOURCES)
        atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "config_sha256": config_sha,
                "actual_attempt": 1,
            },
        )
        try:
            with budget.watchdog():
                budget.check()
                rows = []
                for source in config["sources"]:
                    folder = root / source["folder"]
                    rows.extend(
                        adapt_response(
                            (folder / "response.body").read_bytes(),
                            sealed_read(folder / "result.json"),
                            sealed_read(folder / "intent.json"),
                            source["instrument_id"],
                            source["batch"],
                        )
                    )
                    budget.check()
                if len(rows) != 15190:
                    raise DataValidationError("raw occurrence count changed")
                rows, collisions = classify(rows)
                write_rows(out / "occurrences.parquet", rows, SCHEMA)
                atomic_seal(out / "collisions.json", {"possible_event_collisions": collisions})
                per_code = []
                for source in config["sources"]:
                    code_rows = [r for r in rows if r["instrument_id"] == source["instrument_id"]]
                    per_code.append(
                        {
                            "instrument_id": source["instrument_id"],
                            "all": summarize(code_rows),
                            "in_window": summarize(
                                [r for r in code_rows if r["observed_date_scope"] == "in_window"]
                            ),
                        }
                    )
                yearly = {}
                for year in range(2020, 2025):
                    yearly[str(year)] = summarize(
                        [
                            r
                            for r in rows
                            if any(r[k] and r[k].startswith(str(year)) for k in EVENT_DATES)
                        ]
                    )
                atomic_seal(
                    out / "profiles.json",
                    {"per_code": per_code, "yearly": yearly, "year_groups_may_overlap": True},
                )
                load_contract(root)
                if pushed_head(root) != head or _sha(root / CONFIG) != config_sha:
                    raise DataValidationError("source changed during experiment")
                readback = pq.read_table(out / "occurrences.parquet", use_threads=False)
                if readback.num_rows != len(rows):
                    raise DataValidationError("saved occurrence count changed")
                artifacts = {
                    p.name: {"sha256": _sha(p), "bytes": p.stat().st_size}
                    for p in out.iterdir()
                    if p.is_file()
                }
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "config_sha256": config_sha,
                        "artifacts": artifacts,
                        "all": summarize(rows),
                        "in_window": summarize(
                            [r for r in rows if r["observed_date_scope"] == "in_window"]
                        ),
                        "possible_event_collision_groups": len(collisions),
                        "in_window_collision_groups": sum(
                            c["any_observed_date_in_window"] for c in collisions
                        ),
                        "provider_requests": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "execution_authority": False,
                        "cashflow_eligible": False,
                        "historical_pit_certified": False,
                        "complete_event_history": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "artifact_bytes_before_report": budget.check(),
                        },
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {"error_type": type(exc).__name__, "at": datetime.now(UTC).isoformat()},
            )
            raise
