"""Offline CSI800 observation audit; never certifies membership or writes canonical data."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def bound_bytes(path, bindings):
    path = Path(path).resolve()
    raw = path.read_bytes()
    bindings[str(path)] = digest(raw)
    return raw


def read_fact(path, bindings):
    return json.loads(bound_bytes(path, bindings))


def full_code(value):
    code = str(value).strip()
    if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code):
        return code
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError(f"invalid security code: {code}")
    if code.startswith(("6", "9")):
        return code + ".SH"
    if code.startswith(("0", "2", "3")):
        return code + ".SZ"
    if code.startswith(("4", "8")):
        return code + ".BJ"
    raise ValueError(f"unknown exchange: {code}")


def deduplicate_events(events):
    """Sources corroborate one fact; conflicting candidates authorize no event."""
    groups = defaultdict(dict)
    for event in events:
        if event["action"] not in {"add", "remove"}:
            raise ValueError(f"unknown action: {event['action']}")
        fact = {k: v for k, v in event.items() if k not in {"src", "sources"}}
        fact["date"] = date.fromisoformat(str(fact["date"]))
        key = (fact["date"], fact["code"])
        fingerprint = json.dumps(fact, sort_keys=True, default=str)
        sources = set(event.get("sources", []))
        if event.get("src"):
            sources.add(event["src"])
        if not sources:
            raise ValueError("event requires source provenance")
        group = groups[key]
        if fingerprint not in group:
            group[fingerprint] = {**fact, "sources": set()}
        group[fingerprint]["sources"].update(sources)
    merged, conflicts = [], []
    for (day, code), variants in sorted(groups.items()):
        candidates = [{**v, "sources": sorted(v["sources"])} for _, v in sorted(variants.items())]
        if len(candidates) > 1:
            conflicts.append(
                {
                    "date": day,
                    "code": code,
                    "reason": "same_day_conflicting_facts_or_unknown_order",
                    "candidates": candidates,
                }
            )
        else:
            merged.append(candidates[0])
    return merged, conflicts


def decompose_transition(start, end, events, lower, upper):
    """Audit every window. Same-date contradictory facts stop its derivation."""
    selected = [e for e in events if lower < date.fromisoformat(str(e["date"])) <= upper]
    merged, conflicts = deduplicate_events(selected)
    result = {
        "from": str(lower),
        "to": str(upper),
        "event_count": len(merged),
        "event_conflicts": conflicts,
        "results": [],
        "precondition_failures": [],
    }
    if conflicts:
        return {**result, "classification": "event_conflict", "derived_endpoint": None}
    working = set(start)
    for e in merged:
        present = e["code"] in working
        valid = present if e["action"] == "remove" else not present
        outcome = "applied" if valid else "precondition_failed"
        record = {**e, "outcome": outcome}
        result["results"].append(record)
        if not valid:
            result["precondition_failures"].append(record)
        elif e["action"] == "remove":
            working.remove(e["code"])
        else:
            working.add(e["code"])
    # Distinct securities on the same date commute. No source-label ordering.
    result.update(
        derived_endpoint=sorted(working),
        unexplained_in=sorted(set(end) - working),
        unexplained_out=sorted(working - set(end)),
    )
    if result["precondition_failures"]:
        cls = "invalid_events"
    elif not merged:
        cls = "no_events_endpoint_same" if start == end else "missing_events"
    elif working != end:
        cls = "endpoint_conflict"
    elif start == end:
        cls = "valid_intra_month_round_trip"
    else:
        cls = "endpoint_match_clean"
    result["classification"] = cls
    return result


def load_observations(root):
    """Bind the exact bytes parsed; retain both conflicting observations."""
    observations, bindings, candidates = {}, {}, defaultdict(list)
    files = sorted(Path(root).glob("*/weights.parquet"))
    if not files:
        raise ValueError("no observation files")
    for path in files:
        frame = pd.read_parquet(
            io.BytesIO(bound_bytes(path, bindings)), columns=["trade_date", "con_code"]
        )
        days = pd.to_datetime(frame.trade_date, format="%Y%m%d").dt.date
        for day, group in frame.assign(day=days).groupby("day"):
            members = frozenset(full_code(c) for c in group.con_code)
            candidates[day].append(
                {
                    "path": str(path.resolve()),
                    "sha256": bindings[str(path.resolve())],
                    "members": sorted(members),
                }
            )
            if day not in observations:
                observations[day] = members
    conflicts = []
    for day, records in sorted(candidates.items()):
        distinct = {tuple(r["members"]) for r in records}
        if len(distinct) > 1:
            sets = [set(x) for x in distinct]
            conflicts.append(
                {
                    "date": str(day),
                    "observations": records,
                    "diff_codes": sorted(set.union(*sets) - set.intersection(*sets)),
                }
            )
    return observations, bindings, conflicts


def load_events(root, intake, bindings):
    """Load events from every reviewed source."""
    events = []

    def add_batch(facts_json, source_label, date_key="effective_date"):
        for f in facts_json:
            eff = date.fromisoformat(f[date_key])
            for a in f["added"]:
                events.append(
                    {
                        "date": eff,
                        "code": full_code(a["code"] if isinstance(a, dict) else a),
                        "action": "add",
                        "src": f"{source_label}:{f.get('notice_id', f[date_key])}",
                    }
                )
            for r in f["removed"]:
                events.append(
                    {
                        "date": eff,
                        "code": full_code(r["code"] if isinstance(r, dict) else r),
                        "action": "remove",
                        "src": f"{source_label}:{f.get('notice_id', f[date_key])}",
                    }
                )

    v1 = read_fact(
        root / "membership/recovered-attachments/new-dated-adjustments-reviewed-v1.json", bindings
    )
    add_batch(v1["facts"], "v1-dated")

    body = read_fact(root / "membership/csi-202106-body-reviewed-v2.json", bindings)
    eff = date.fromisoformat(body["first_applicable_trading_date"])
    for c in body["candidate_csi800_union_changes"]["added"]:
        events.append({"date": eff, "code": full_code(c), "action": "add", "src": "body-12470"})
    for c in body["candidate_csi800_union_changes"]["removed"]:
        events.append({"date": eff, "code": full_code(c), "action": "remove", "src": "body-12470"})

    e = read_fact(root / "membership/conditional-2021-energy-reviewed.json", bindings)
    eff_e = date.fromisoformat(e["effective_date"])
    events.append(
        {"date": eff_e, "code": full_code(e["add"]), "action": "add", "src": "cond-13420"}
    )
    events.append(
        {"date": eff_e, "code": full_code(e["remove"]), "action": "remove", "src": "cond-13420"}
    )

    v2 = read_fact(root / "reviewed-v2/reviewed_facts.json", bindings)
    for f in v2["facts"]:
        if f.get("kind") == "index_constituent_change":
            eff = date.fromisoformat(f["effective_date"])
            for c in f["added"]:
                events.append(
                    {
                        "date": eff,
                        "code": full_code(c),
                        "action": "add",
                        "src": f"v2-{f['announcement_date']}",
                    }
                )
            for c in f["removed"]:
                events.append(
                    {
                        "date": eff,
                        "code": full_code(c),
                        "action": "remove",
                        "src": f"v2-{f['announcement_date']}",
                    }
                )

    c25 = read_fact(root / "membership/conditional-adjustments-2025-reviewed.json", bindings)
    for f in c25["facts"]:
        eff = date.fromisoformat(f["effective_date"])
        events.append(
            {"date": eff, "code": full_code(f["add"]), "action": "add", "src": "cond-2025"}
        )
        events.append(
            {"date": eff, "code": full_code(f["remove"]), "action": "remove", "src": "cond-2025"}
        )

    v4_path = intake / "reviewed-v4/csi_adjustments.json"
    v4 = read_fact(v4_path, bindings)
    for a in v4["adjustments"]:
        fs = a.get("first_effective_session")
        if not fs:
            continue
        eff = date.fromisoformat(fs)
        delta = a.get("csi800_net_delta", {})
        for c in delta.get("add", []):
            events.append(
                {"date": eff, "code": full_code(c), "action": "add", "src": f"v4-{a['source_id']}"}
            )
        for c in delta.get("remove", []):
            events.append(
                {
                    "date": eff,
                    "code": full_code(c),
                    "action": "remove",
                    "src": f"v4-{a['source_id']}",
                }
            )

    return events


def build_audit(observation_root, facts_root, intake_root):
    observations, observation_hashes, conflicts = load_observations(observation_root)
    payload = {
        "schema": "quantlab_transition_decomposition_v6",
        "created_at": datetime.now(UTC).isoformat(),
        "script": {
            "path": str(Path(__file__).resolve()),
            "sha256": digest(Path(__file__).read_bytes()),
        },
        "observation_file_hashes": observation_hashes,
        "same_day_observation_conflicts": conflicts,
        "historical_membership_certified": False,
        "production_applied": False,
        "limitations": [
            "Monthly endpoints do not prove intra-month completeness.",
            "Initial membership anchor and historical availability remain uncertified.",
            "Missing loaded events do not establish absence of external evidence.",
            "No automatic universe admission or identity-code substitution.",
        ],
        "rows": [],
    }
    if conflicts:
        return {**payload, "status": "blocked_observation_conflict", "classification_counts": {}}
    facts = {}
    events = load_events(Path(facts_root), Path(intake_root), facts)
    days = sorted(observations)
    rows = [
        decompose_transition(observations[a], observations[b], events, a, b)
        for a, b in zip(days, days[1:], strict=False)
    ]
    merged, event_conflicts = deduplicate_events(events)
    payload.update(
        status="diagnostic_only",
        fact_file_hashes=facts,
        event_count_unique=len(merged),
        event_conflicts=event_conflicts,
        transitions_total=len(rows),
        rows=rows,
        classification_counts=dict(Counter(r["classification"] for r in rows)),
    )
    return payload


def render_report(payload):
    lines = [
        "# CSI800 observation audit",
        "",
        f"Status: {payload['status']}",
        "",
        "Diagnostic only: no membership/PIT certification or production admission.",
        "",
        "| Classification | Windows |",
        "|---|---:|",
    ]
    for name, count in sorted(payload["classification_counts"].items()):
        lines.append(f"| {name} | {count} |")
    lines += [
        "",
        "| Window end | Classification | Residual in | Residual out |",
        "|---|---|---:|---:|",
    ]
    for row in payload["rows"]:
        if row["classification"] not in {
            "no_events_endpoint_same",
            "endpoint_match_clean",
            "valid_intra_month_round_trip",
        }:
            lines.append(
                f"| {row['to']} | {row['classification']} | "
                f"{len(row.get('unexplained_in', []))} | "
                f"{len(row.get('unexplained_out', []))} |"
            )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("observations", "facts-root", "intake-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    payload = build_audit(args.observations, args.facts_root, args.intake_root)
    artifact = args.output / "audit.json"
    with artifact.open("x") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
    (args.output / "report.md").write_text(render_report(payload))
    (args.output / "artifact.sha256").write_text(digest(artifact.read_bytes()) + "  audit.json\n")
    print(json.dumps({"status": payload["status"], "counts": payload["classification_counts"]}))
    return 2 if payload["status"].startswith("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
