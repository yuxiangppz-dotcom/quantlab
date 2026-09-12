"""Source-bound local event-input staging with no provider or accounting authority."""

from __future__ import annotations

import csv
import json
import subprocess
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.data.dividend_acquisition import OUTPUT as RAW_OUTPUT
from quantlab.data.dividend_raw import DATES, MEASURES
from quantlab.data.dividend_review import read_acquisition, read_verification
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.cost_input_review import read_report as read_cost
from quantlab.research.dividend_events import EVENT_DATES, link_window, observations
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs

CONFIG = "config/dividend_event_inputs_v1.json"
OUTPUT = "data/products/dividend_event_inputs/events_20260912"
WINDOW_KEYS = [
    "model",
    "trade_date",
    "instrument_id",
    "horizon",
    "entry_date",
    "exit_date",
    "entry_beyond_cutoff",
    "exit_beyond_cutoff",
]
OBS_STRINGS = [
    "instrument_id",
    "response_fingerprint",
    "response_sha256",
    "request_fingerprint",
    "observation_id",
    "content_id",
    "observed_at",
    "raw_payload",
    "raw_status",
    "normalized_status",
    "candidate_group_id",
    "quality_flags",
    *DATES,
]
OBS_BOOLS = [
    "historical_pit_certified",
    "cashflow_eligible",
    "status_normalized",
    "implementation_candidate",
    "has_event_date",
    "candidate_conflict",
]
OBS_SCHEMA = pa.schema(
    [(key, pa.string()) for key in OBS_STRINGS]
    + [(key, pa.bool_()) for key in OBS_BOOLS]
    + [(key, pa.float64()) for key in MEASURES]
    + [("row_index", pa.int64()), ("duplicate_occurrences", pa.int64())]
)
WINDOW_STRINGS = ["model", "trade_date", "instrument_id", "entry_date", "exit_date", "window_id"]
WINDOW_BOOLS = [
    "entry_beyond_cutoff",
    "exit_beyond_cutoff",
    "unknown_event_history",
    "cashflow_eligible",
    "execution_authority",
    "overlap_evaluated",
    "overlap_through_intended_exit",
    "post_exit_comparison_known",
]
WINDOW_COUNTS = [
    "window_source_row",
    "horizon",
    "candidate_observations",
    "candidate_unique_contents",
    "implementation_candidates",
    "conflicting_candidates",
    "observed_candidates_at_signal_close",
    "payments_after_intended_exit",
    "listings_after_intended_exit",
    "undated_observations_for_code",
]
WINDOW_SCHEMA = pa.schema(
    [(key, pa.string()) for key in WINDOW_STRINGS]
    + [(key, pa.bool_()) for key in WINDOW_BOOLS]
    + [(key, pa.int64()) for key in WINDOW_COUNTS]
    + [(key, pa.null()) for key in ("entitlement_amount", "dividend_tax_fen", "complete_cost_fen")]
)
LINK_STRINGS = ["window_id", "observation_id", "content_id", "candidate_group_id", "quality_flags"]
LINK_SCHEMA = pa.schema(
    [(key, pa.string()) for key in LINK_STRINGS]
    + [
        (key, pa.bool_())
        for key in (
            "implementation_candidate",
            "record_date_in_window",
            "payment_after_intended_exit",
            "listing_after_intended_exit",
            "locally_observed_by_signal_close",
            "candidate_conflict",
        )
    ]
)


def write_rows(path, rows, schema):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DataValidationError("event-input artifact already exists")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")


def saved_windows(path):
    rows = pq.read_table(path, columns=WINDOW_KEYS, use_threads=False).to_pylist()
    for index, row in enumerate(rows):
        for key in ("trade_date", "entry_date", "exit_date"):
            row[key] = row[key].date().isoformat() if row[key] is not None else None
        row["window_source_row"] = index
    identities = [(r["model"], r["trade_date"], r["instrument_id"], r["horizon"]) for r in rows]
    if len(set(identities)) != len(rows):
        raise DataValidationError("saved target window identity duplicated")
    return rows


def run(root):
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    verify_entries(root, config["inputs"])
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("event inputs require pushed source")
    kind, raw_report = read_acquisition(root)
    verification = read_verification(root, raw_report)
    cost = read_cost(root)
    if (
        kind != "report"
        or raw_report["fingerprint"] != config["acquisition_fingerprint"]
        or verification["fingerprint"] != config["verification_fingerprint"]
        or cost["fingerprint"] != config["cost_fingerprint"]
    ):
        raise DataValidationError("event input source identity changed")
    codes = [row["instrument_id"] for row in raw_report["per_code"]]
    if len(codes) != config["instrument_count"] or raw_report["status_counts"] != {
        "nonempty": len(codes)
    }:
        raise DataValidationError("incomplete fixed raw response population")
    windows = saved_windows(root / config["windows_path"])
    by_code = defaultdict(list)
    for window in windows:
        by_code[window["instrument_id"]].append(window)
    if set(by_code) != set(codes) or len(windows) != config["window_count"]:
        raise DataValidationError("saved window population changed")
    source_inputs = {
        **config["inputs"],
        **{RAW_OUTPUT + "/" + name: entry for name, entry in raw_report["artifacts"].items()},
    }
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        budget = Budget(out, config["resources"])
        plan = atomic_seal(
            out / "plan.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "code_head": head,
                "code_files": binding.entries,
                "config_sha256": _sha(root / CONFIG),
                "source_inputs": source_inputs,
                "acquisition_fingerprint": raw_report["fingerprint"],
                "new_fit_attempts": 0,
            },
        )
        totals, quality, statuses = Counter(), Counter(), Counter()
        conflicts, relevant_conflicts, contents, seen_observations = set(), set(), set(), set()
        review_cases = []
        groups = defaultdict(Counter)
        batches = {"observations": [], "windows": [], "links": []}
        artifacts = {}
        try:
            with budget.watchdog():
                for position, record in enumerate(raw_report["per_code"]):
                    if position % config["codes_per_partition"] == 0:
                        if not budget.can_start():
                            raise DataValidationError("event-input checkpoint required")
                        budget.check(projected_bytes=64 * 1024**2, projected_memory=256 * 1024**2)
                    code = record["instrument_id"]
                    folder = root / RAW_OUTPUT / "attempts" / code / f"{record['attempts']:02d}"
                    receipt = sealed_read(folder / "result.json")
                    rows = observations((folder / "response.body").read_bytes(), receipt, code)
                    for row in rows:
                        if row["observation_id"] in seen_observations:
                            raise DataValidationError("observation identity collision")
                        seen_observations.add(row["observation_id"])
                        contents.add(row["content_id"])
                        statuses[row["normalized_status"] or "<unknown>"] += 1
                        quality.update(json.loads(row["quality_flags"]))
                        if row["candidate_conflict"]:
                            conflicts.add(row["candidate_group_id"])
                        relevant = any(
                            row[key] and config["start"] <= row[key] <= config["end"]
                            for key in EVENT_DATES
                        )
                        totals["potentially_date_relevant"] += relevant
                        if relevant and row["candidate_conflict"]:
                            relevant_conflicts.add(row["candidate_group_id"])
                        if row["candidate_conflict"] or row["status_normalized"]:
                            review_cases.append(
                                {
                                    **{
                                        key: row[key]
                                        for key in (
                                            "instrument_id",
                                            "observation_id",
                                            "response_fingerprint",
                                            "row_index",
                                            "candidate_group_id",
                                            "raw_status",
                                            "normalized_status",
                                            *EVENT_DATES,
                                            "quality_flags",
                                        )
                                    },
                                    "date_window_relevant": relevant,
                                }
                            )
                    totals["observations"] += len(rows)
                    batches["observations"].extend(rows)
                    for window in by_code[code]:
                        summary, links = link_window(window, rows, config["end"])
                        batches["windows"].append(summary)
                        batches["links"].extend(links)
                        counts = groups[(window["model"], window["horizon"])]
                        counts["windows"] += 1
                        counts["windows_with_candidates"] += bool(links)
                        counts["windows_without_known_entry"] += not summary["overlap_evaluated"]
                        counts["windows_without_known_exit"] += not summary[
                            "post_exit_comparison_known"
                        ]
                        for key in (
                            "candidate_observations",
                            "implementation_candidates",
                            "conflicting_candidates",
                            "observed_candidates_at_signal_close",
                            "payments_after_intended_exit",
                            "listings_after_intended_exit",
                        ):
                            if summary[key] is not None:
                                counts[key] += summary[key]
                    if (position + 1) % config["codes_per_partition"] == 0 or position == len(
                        codes
                    ) - 1:
                        partition = position // config["codes_per_partition"]
                        for name, schema in (
                            ("observations", OBS_SCHEMA),
                            ("windows", WINDOW_SCHEMA),
                            ("links", LINK_SCHEMA),
                        ):
                            path = out / name / f"{partition:04d}.parquet"
                            write_rows(path, batches[name], schema)
                            artifacts[path.relative_to(out).as_posix()] = {
                                "sha256": _sha(path),
                                "bytes": path.stat().st_size,
                            }
                            totals[name + "_written"] += len(batches[name])
                            batches[name].clear()
                        budget.check()
                        print(
                            json.dumps(
                                {
                                    "codes": position + 1,
                                    "observations": totals["observations"],
                                    "windows": totals["windows_written"],
                                }
                            ),
                            flush=True,
                        )
        except BaseException as exc:
            atomic_seal(
                out / "failed.json",
                {
                    "error_type": type(exc).__name__,
                    "plan": plan["fingerprint"],
                    "totals": dict(totals),
                },
            )
            raise
        binding.check()
        verify_entries(root, source_inputs)
        if (
            totals["observations"] != config["observation_count"]
            or totals["windows_written"] != config["window_count"]
        ):
            raise DataValidationError("event output population incomplete")
        review_path = out / "review_cases.csv"
        fields = [
            "instrument_id",
            "observation_id",
            "response_fingerprint",
            "row_index",
            "candidate_group_id",
            "raw_status",
            "normalized_status",
            *EVENT_DATES,
            "quality_flags",
            "date_window_relevant",
        ]
        with review_path.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(review_cases)
        artifacts["review_cases.csv"] = {
            "sha256": _sha(review_path),
            "bytes": review_path.stat().st_size,
        }
        return atomic_seal(
            out / "report.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "plan_fingerprint": plan["fingerprint"],
                "config_sha256": plan["config_sha256"],
                "artifacts": artifacts,
                "instrument_count": len(codes),
                "totals": dict(totals),
                "quality_counts": dict(quality),
                "status_counts": dict(statuses),
                "unique_contents": len(contents),
                "exact_duplicate_rows": totals["observations"] - len(contents),
                "candidate_conflict_groups": len(conflicts),
                "candidate_conflict_groups_date_window": len(relevant_conflicts),
                "review_case_rows": len(review_cases),
                "window_summary": [
                    {"model": key[0], "horizon": key[1], **dict(value)}
                    for key, value in sorted(groups.items())
                ],
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "bytes": budget.check(),
                },
                "new_fit_attempts": 0,
                "provider_calls": 0,
                "cashflow_eligible": False,
                "historical_pit_certified": False,
                "complete_event_history_certified": False,
                "performance_evidence": False,
                "execution_authority": False,
            },
        )


def read_report(root):
    out = root / OUTPUT
    if not (out / "report.json").exists():
        return None
    report, plan = sealed_read(out / "report.json"), sealed_read(out / "plan.json")
    if (
        report["plan_fingerprint"] != plan["fingerprint"]
        or report["config_sha256"] != plan["config_sha256"]
        or report["config_sha256"] != _sha(root / CONFIG)
    ):
        raise DataValidationError("event-input source plan changed")
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    if (
        report.get("new_fit_attempts") != 0
        or report.get("provider_calls") != 0
        or report["instrument_count"] != config["instrument_count"]
        or report["totals"]["observations_written"] != config["observation_count"]
        or report["totals"]["windows_written"] != config["window_count"]
        or sum(row["windows"] for row in report["window_summary"]) != config["window_count"]
    ):
        raise DataValidationError("event-input population or action count changed")
    for flag in (
        "cashflow_eligible",
        "historical_pit_certified",
        "complete_event_history_certified",
        "performance_evidence",
        "execution_authority",
    ):
        if report.get(flag) is not False:
            raise DataValidationError("event candidates gained financial authority")
    verify_historical_inputs(root, {"code_head": plan["code_head"], "inputs": plan["code_files"]})
    verify_entries(root, plan["source_inputs"])
    verify_entries(out, report["artifacts"])
    return report
