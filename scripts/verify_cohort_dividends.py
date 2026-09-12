"""One offline cross-check from raw bodies; never calls the production event mapper."""

import hashlib
import json
import math
import resource
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.round2_dataset import sealed_read


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def aggregate(rows):
    names = [
        "occurrences",
        "implementation_occurrences",
        "structurally_ready_occurrences",
        "possible_event_conflict_occurrences",
    ]
    counts = {name: 0 for name in names} if rows else {}
    for row in rows:
        for name, value in zip(
            names,
            [
                1,
                row["implementation_candidate"],
                row["structurally_ready"],
                row["possible_event_conflict"],
            ],
            strict=True,
        ):
            counts[name] += value
        scope = row["observed_date_scope"]
        counts[scope] = counts.get(scope, 0) + 1

    def categories(key, multiple=False):
        return dict(
            Counter(
                value
                for row in rows
                for value in (json.loads(row[key]) if multiple else [row[key] or "unknown"])
            )
        )

    return {
        "counts": counts,
        "statuses": categories("normalized_status"),
        "flags": categories("quality_flags", True),
        "blockers": categories("structural_blockers", True),
        "availability_provenance": categories("availability_bound_source"),
        "distinct_contents": len({r["content_id"] for r in rows}),
        "exact_duplicate_excess": len(rows) - len({r["content_id"] for r in rows}),
        "candidate_conflict_groups": len(
            {r["candidate_group_id"] for r in rows if r["candidate_conflict"]}
        ),
        "structurally_ready_contents": len(
            {r["content_id"] for r in rows if r["structurally_ready"]}
        ),
    }


def check(root):
    started = time.monotonic()
    config = json.loads((root / "config/cohort_dividend_readiness_v1.json").read_text())
    out = root / config["output"]
    report = sealed_read(out / "report.json")
    for folder, entries in [(root, config["inputs"]), (out, report["artifacts"])]:
        for name, entry in entries.items():
            raw = (folder / name).read_bytes()
            assert len(raw) == entry["bytes"] and hashlib.sha256(raw).hexdigest() == entry["sha256"]
    rows = pq.read_table(out / "occurrences.parquet", use_threads=False).to_pylist()
    indexed = {(r["instrument_id"], r["row_index"]): r for r in rows}
    assert len(rows) == len(indexed) == 15190
    dates = [
        "end_date",
        "ann_date",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "imp_ann_date",
        "base_date",
    ]
    measures = ["stk_div", "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax", "base_share"]
    event_dates = ["record_date", "ex_date", "pay_date", "div_listdate"]
    known = {
        "实施",
        "预案",
        "股东大会通过",
        "股东大会未通过",
        "未通过",
        "停止实施",
        "股东提议",
        "预披露",
        "其他",
    }
    pending_flags, candidates, contents, events = {}, defaultdict(set), Counter(), defaultdict(list)
    seen = 0
    for source in config["sources"]:
        folder = root / source["folder"]
        receipt, intent = sealed_read(folder / "result.json"), sealed_read(folder / "intent.json")
        assert receipt["intent_fingerprint"] == intent["fingerprint"]
        assert receipt["status"] == "nonempty"
        raw = (folder / "response.body").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == receipt["wire_sha256"]
        body = json.loads(raw)
        fields, items = body["data"]["fields"], body["data"]["items"]
        assert body["code"] == 0 and len(items) == receipt["rows"]
        for i, values in enumerate(items):
            original = dict(zip(fields, values, strict=True))
            row = indexed[(source["instrument_id"], i)]
            assert json.loads(row["raw_payload"]) == original
            assert row["instrument_id"] == original["ts_code"]
            assert row["row_index"] == i
            assert row["content_id"] == digest(original)
            assert row["response_fingerprint"] == receipt["fingerprint"]
            assert row["response_sha256"] == receipt["wire_sha256"]
            assert row["request_fingerprint"] == intent["fingerprint"]
            assert row["observation_id"] == digest([receipt["fingerprint"], i])
            stamp_key = "observed_at" if source["batch"] == "legacy" else "at"
            assert row["observed_at"] == datetime.fromisoformat(receipt[stamp_key]).isoformat()
            assert row["source_observed_at"] == receipt.get("observed_at")
            assert row["source_receipt_at"] == receipt.get("at")
            assert row["availability_bound_source"] == (
                "wire_observed_at" if source["batch"] == "legacy" else "receipt_recorded_at"
            )
            issues = set()
            for key in dates:
                value = original[key]
                parsed = None
                if value not in (None, ""):
                    try:
                        assert isinstance(value, str) and len(value) == 8
                        assert value.isascii() and value.isdigit()
                        parsed = datetime.strptime(value, "%Y%m%d").date().isoformat()
                    except (ValueError, AssertionError):
                        issues.add("malformed_" + key)
                assert row[key] == parsed
            for key in measures:
                value = original[key]
                parsed = None
                if value not in (None, ""):
                    if type(value) in (float, int) and math.isfinite(value) and value >= 0:
                        parsed = float(value)
                    else:
                        issues.add("malformed_" + key)
                assert row[key] == parsed
            status = original["div_proc"].strip() if isinstance(original["div_proc"], str) else None
            assert row["normalized_status"] == status
            assert row["raw_status"] == (
                original["div_proc"] if isinstance(original["div_proc"], str) else None
            )
            assert row["status_normalized"] == (status != original["div_proc"])
            assert row["implementation_candidate"] == (status == "实施")
            if row["status_normalized"]:
                issues.add("status_normalized")
            if status not in known:
                issues.add("unknown_status")
            for right, flag in [
                ("ex_date", "record_after_ex"),
                ("pay_date", "pay_before_record"),
                ("div_listdate", "listing_before_record"),
            ]:
                if row["record_date"] and row[right] and row["record_date"] > row[right]:
                    issues.add(flag)
            if any(row[k] is None for k in measures[:3]):
                issues.add("share_components_unknown")
            elif abs(row["stk_div"] - row["stk_bo_rate"] - row["stk_co_rate"]) > 1e-8:
                issues.add("share_components_disagree")
            if row["cash_div_tax"] is None:
                issues.add("cash_before_tax_unknown")
            if row["base_date"] is None or row["base_share"] is None:
                issues.add("base_shares_unknown")
            if status == "实施":
                if not row["record_date"] or not row["ex_date"]:
                    issues.add("implemented_record_or_ex_unknown")
                if row["cash_div_tax"] and not row["pay_date"]:
                    issues.add("positive_cash_pay_date_unknown")
                if row["stk_div"] and not row["div_listdate"]:
                    issues.add("positive_shares_listing_date_unknown")
            days = [row[k] for k in event_dates if row[k]]
            assert row["has_event_date"] == bool(days)
            if not days:
                issues.add("event_date_relevance_unknown")
            scope = (
                "in_window"
                if any("2020-01-01" <= d <= "2024-12-31" for d in days)
                else ("no_observed_date_in_window" if days else "no_valid_event_dates")
            )
            assert row["observed_date_scope"] == scope
            candidate = digest(
                [
                    original[k]
                    for k in ["ts_code", "end_date", "ann_date", "div_proc", "imp_ann_date"]
                ]
            )
            assert row["candidate_group_id"] == candidate
            candidates[candidate].add(row["content_id"])
            contents[row["content_id"]] += 1
            pending_flags[row["observation_id"]] = issues
            event = None
            if status == "实施" and row["record_date"] and row["ex_date"]:
                event = digest(
                    [row[k] for k in ["instrument_id", "end_date", "record_date", "ex_date"]]
                )
                events[event].append(row)
            assert row["possible_event_group_id"] == event
            seen += 1
    assert seen == len(rows) and len(pending_flags) == len(rows)
    collisions = []
    for key, members in sorted(events.items()):
        variants = {
            tuple(r[k] for k in [*measures[:5], "pay_date", "div_listdate"]) for r in members
        }
        if len(variants) > 1:
            collisions.append(
                {
                    "possible_event_group_id": key,
                    "instrument_id": members[0]["instrument_id"],
                    "observation_ids": [r["observation_id"] for r in members],
                    "distinct_term_variants": len(variants),
                    "any_observed_date_in_window": any(
                        r["observed_date_scope"] == "in_window" for r in members
                    ),
                    "resolved": False,
                }
            )
    conflict_ids = {c["possible_event_group_id"] for c in collisions}
    allowed_nonblockers = {
        "duplicate_content",
        "base_shares_unknown",
        "status_normalized",
        "event_date_relevance_unknown",
    }
    for row in rows:
        issues = pending_flags[row["observation_id"]]
        duplicates = contents[row["content_id"]]
        assert row["duplicate_occurrences"] == duplicates
        if duplicates > 1:
            issues.add("duplicate_content")
        candidate_conflict = len(candidates[row["candidate_group_id"]]) > 1
        assert row["candidate_conflict"] == candidate_conflict
        if candidate_conflict:
            issues.add("candidate_version_conflict")
        assert set(json.loads(row["quality_flags"])) == issues
        blockers = issues - allowed_nonblockers
        if not row["implementation_candidate"]:
            blockers.add("not_implementation")
        conflict = row["possible_event_group_id"] in conflict_ids
        assert row["possible_event_conflict"] == conflict
        if conflict:
            blockers.add("possible_event_terms_conflict")
        assert json.loads(row["structural_blockers"]) == sorted(blockers)
        assert row["structurally_ready"] == (not blockers)
        assert row["historical_pit_certified"] is False and row["cashflow_eligible"] is False
    assert sealed_read(out / "collisions.json")["possible_event_collisions"] == collisions
    assert report["all"] == aggregate(rows)
    assert report["in_window"] == aggregate(
        [r for r in rows if r["observed_date_scope"] == "in_window"]
    )
    profiles = sealed_read(out / "profiles.json")
    for per_code in profiles["per_code"]:
        subset = [r for r in rows if r["instrument_id"] == per_code["instrument_id"]]
        assert per_code["all"] == aggregate(subset)
        assert per_code["in_window"] == aggregate(
            [r for r in subset if r["observed_date_scope"] == "in_window"]
        )
    for year, summary in profiles["yearly"].items():
        assert summary == aggregate(
            [r for r in rows if any(r[k] and r[k].startswith(year) for k in event_dates)]
        )
    assert report["possible_event_collision_groups"] == len(collisions)
    assert report["in_window_collision_groups"] == sum(
        c["any_observed_date_in_window"] for c in collisions
    )
    for key in [
        "execution_authority",
        "cashflow_eligible",
        "historical_pit_certified",
        "complete_event_history",
    ]:
        assert report[key] is False
    proof = atomic_seal(
        out / "independent_proof.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "report_fingerprint": report["fingerprint"],
            "checked_occurrences": len(rows),
            "checked_codes": len(profiles["per_code"]),
            "checked_year_profiles": len(profiles["yearly"]),
            "checked_source_files": len(config["inputs"]),
            "all_checks_passed": True,
            "method": (
                "separate raw decoding and row/field/flag/group/profile assertions; "
                "same-agent self-check"
            ),
            "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "seconds": time.monotonic() - started,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        },
    )
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    check(Path.cwd())
