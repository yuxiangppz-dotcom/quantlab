"""Compile dated CSI800 membership from a published anchor and reviewed notices.

Inputs are immutable research evidence, not monthly memberships.  Monthly
observations are used only as independent endpoint checks.  Any unbound input,
failed event precondition, unknown notice date, or endpoint difference stops the
build.  The first usable interval starts after the complete anchor was public.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_json(path: Path, bindings: dict[str, str]) -> dict:
    bindings[str(path.resolve())] = sha(path)
    return json.loads(path.read_text())


def publication(source: str, effective: date, notices: dict[str, dict], conditional: dict) -> date:
    if source == "cond-2025":
        return date.fromisoformat(conditional[str(effective)])
    if source.startswith("v2-"):
        return date.fromisoformat(source.removeprefix("v2-"))
    if source.startswith("csi:"):
        ident = source.split(":")[1]
    elif source.startswith("v4-csi:"):
        ident = source.split(":")[1]
    elif source.startswith("v1-dated:"):
        ident = source.split(":")[1]
    elif source.startswith(("body-", "cond-")):
        ident = source.split("-")[1]
    else:
        raise ValueError(f"unmapped notice source:{source}")
    if ident not in notices:
        raise ValueError(f"notice absent from full official index:{ident}")
    return date.fromisoformat(notices[ident]["publishDate"])


def normalize_observed(code: str, day: date, changes: list[dict]) -> str:
    for change in changes:
        if code == change["new_instrument_id"] and day < change["effective_date"]:
            return change["old_instrument_id"]
    return code


def build(
    anchor_path: Path,
    diagnostic_path: Path,
    index_path: Path,
    changes_path: Path,
    conditional_path: Path,
    code_change_pdf: Path,
    output: Path,
    *,
    end: date,
) -> dict:
    bindings: dict[str, str] = {}
    anchor = checked_json(anchor_path, bindings)
    diagnostic = checked_json(diagnostic_path, bindings)
    index = checked_json(index_path, bindings)
    conditional_facts = checked_json(conditional_path, bindings)
    bindings[str(changes_path.resolve())] = sha(changes_path)
    if anchor["schema"] != "quantlab_csi800_historical_anchor_v1":
        raise ValueError("unrecognized complete anchor")
    if anchor["validation"]["fund_union"] != 800 or len(set(anchor["members"])) != 800:
        raise ValueError("anchor has no verified 800-member identity")
    if diagnostic["status"] != "diagnostic_only" or diagnostic.get("event_conflicts"):
        raise ValueError("reviewed transition diagnostic is conflicted")
    # Verify every prior attachment and raw observation that the reviewed event
    # extraction actually used.  A stale diagnostic must never authorize reuse.
    for key in ("source_hashes", "observation_file_hashes"):
        for path, expected in diagnostic[key].items():
            source = Path(path)
            actual = sha(source)
            if actual != expected:
                raise ValueError(f"reviewed source changed:{source}")
            bindings[str(source.resolve())] = actual
    for fund in anchor["funds"]:
        ident = fund["url"].split("/")[-1].removesuffix(".PDF")
        pdf = anchor_path.parent / f"{ident}.pdf"
        if sha(pdf) != fund["pdf_sha256"]:
            raise ValueError(f"anchor fund report changed:{ident}")
        bindings[str(pdf.resolve())] = sha(pdf)
    if index["code"] != "200" or len(index["data"]) != index["total"]:
        raise ValueError("official notice index incomplete")
    notices = {str(item["id"]): item for item in index["data"]}
    if len(notices) != index["total"]:
        raise ValueError("official notice index has duplicate IDs")
    conditional = {
        f["effective_date"]: f["notice_publication_date"] for f in conditional_facts["facts"]
    }
    changes_frame = pd.read_csv(changes_path, dtype=str)
    changes = [
        {**row, "effective_date": date.fromisoformat(row["effective_date"])}
        for row in changes_frame.to_dict("records")
    ]
    successor = next((row for row in changes if row["old_instrument_id"] == "300114.SZ"), None)
    if successor is None or successor["new_instrument_id"] != "302132.SZ":
        raise ValueError("reviewed issuer code lifecycle changed")
    expected_pdf = re.search(r"PDF sha256=([0-9a-f]{64})", successor["note"])
    if expected_pdf is None or sha(code_change_pdf) != expected_pdf.group(1):
        raise ValueError("issuer code-change announcement does not match reviewed hash")
    bindings[str(code_change_pdf.resolve())] = sha(code_change_pdf)

    # CSI notice 4718: 000748 was removed and 002670 added when 000748 delisted
    # on 2017-01-18.  The anchor's independent 2017-01-26 check binds this pair.
    current = set(anchor["members"])
    if "000748.SZ" not in current or "002670.SZ" in current:
        raise ValueError("initial official replacement precondition failed")
    current.remove("000748.SZ")
    current.add("002670.SZ")
    if date.fromisoformat(notices["4718"]["publishDate"]) >= date(2017, 1, 18):
        raise ValueError("initial adjustment not announced before effective date")
    events_by_day: dict[date, list[dict]] = defaultdict(list)
    for row in diagnostic["rows"]:
        if row["event_conflicts"] or row["precondition_failures"]:
            raise ValueError(f"reviewed event conflict:{row['to']}")
        for event in row["results"]:
            if event["outcome"] != "applied":
                raise ValueError(f"unapplied reviewed event:{event}")
            day = date.fromisoformat(event["date"])
            pubdates = [publication(s, day, notices, conditional) for s in event["sources"]]
            if max(pubdates) >= day:
                raise ValueError(f"notice not public before membership effective:{event}")
            events_by_day[day].append({**event, "public_date": str(max(pubdates))})
    events_by_day[successor["effective_date"]].append(
        {
            "action": "identity",
            "old": successor["old_instrument_id"],
            "code": successor["new_instrument_id"],
            "date": str(successor["effective_date"]),
            "sources": ["issuer-code-change:1222485220"],
            "public_date": "2025-02-07",
        }
    )
    observations = {}
    for path in diagnostic["observation_file_hashes"]:
        frame = pd.read_parquet(path, columns=["trade_date", "con_code"])
        for raw_day, group in frame.groupby("trade_date"):
            day = date.fromisoformat(f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}")
            codes = {normalize_observed(code, day, changes) for code in group.con_code}
            if len(codes) != 800:
                raise ValueError(f"monthly observation not 800:{day}")
            if day in observations and observations[day] != codes:
                raise ValueError(f"same-day observation conflict:{day}")
            observations[day] = codes
    if len(observations) != len(diagnostic["observation_file_hashes"]):
        raise ValueError("historical observation count differs from reviewed inputs")
    if current != observations[date(2017, 1, 26)]:
        raise ValueError("anchor plus first official replacement misses January observation")
    usable = datetime.fromisoformat(anchor["known_at"]).date() + timedelta(days=1)
    first = max(usable, date(2017, 4, 1))
    checkpoints = sorted(set(events_by_day) | set(observations))
    snapshots = []
    cursor = first
    known = anchor["known_at"]
    source_ids = ["fund-union-2016-anchor", "csi:4718"]
    for day in checkpoints:
        if day < date(2017, 1, 26):
            raise ValueError(f"unexpected pre-January event:{day}")
        if day > end:
            break
        if day >= first and day > cursor:
            snapshots.append(
                {
                    "start": str(cursor),
                    "end": str(day - timedelta(days=1)),
                    "members": sorted(current),
                    "complete": True,
                    "known_at": known,
                    "source_id": "+".join(source_ids),
                    "revision_id": "notice-chain-v1",
                }
            )
            cursor = day
        for event in sorted(
            events_by_day.get(day, []), key=lambda e: (e["action"] != "remove", e["code"])
        ):
            if event["action"] == "identity":
                if event["old"] not in current:
                    continue
                current.remove(event["old"])
                if event["code"] in current:
                    raise ValueError(f"duplicate successor:{event['code']}")
                current.add(event["code"])
            elif event["action"] == "remove":
                if event["code"] not in current:
                    raise ValueError(f"remove nonmember:{event}")
                current.remove(event["code"])
            else:
                if event["code"] in current:
                    raise ValueError(f"add existing member:{event}")
                current.add(event["code"])
            source_ids = event["sources"]
            public_date = date.fromisoformat(event["public_date"])
            available = datetime.combine(
                public_date + timedelta(days=1), time(18), tzinfo=ZoneInfo("Asia/Shanghai")
            ).isoformat()
            known = max(known, available)
        if len(current) != 800:
            raise ValueError(f"event chain not exactly 800:{day}:{len(current)}")
        if day in observations and current != observations[day]:
            raise ValueError(
                f"official chain differs from independent monthly observation:{day}:"
                f"missing={sorted(observations[day] - current)}:"
                f"excess={sorted(current - observations[day])}"
            )
    snapshots.append(
        {
            "start": str(cursor),
            "end": str(end),
            "members": sorted(current),
            "complete": True,
            "known_at": known,
            "source_id": "+".join(source_ids),
            "revision_id": "notice-chain-v1",
        }
    )
    if any(
        datetime.fromisoformat(row["known_at"]).date() >= date.fromisoformat(row["start"])
        for row in snapshots
    ):
        raise ValueError("membership publication not available before interval start")
    payload = {
        "schema": "quantlab_index_membership_v1",
        "index": "000906.SH",
        "semantics": "published_effective_intervals",
        "snapshots": snapshots,
        "validation": {
            "monthly_observations": len(observations),
            "notice_index_unique": len(notices),
            "first_usable_day": str(first),
            "last_day": str(end),
        },
        "input_sha256": {**bindings, str(Path(__file__).resolve()): sha(Path(__file__))},
        "limitation": (
            "Certified against the retrieved complete CSI notice API index and all "
            "monthly endpoints; the API cannot independently prove no historically "
            "omitted notices."
        ),
    }
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "anchor",
        "diagnostic",
        "index",
        "changes",
        "conditional",
        "code-change-pdf",
        "output",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--end", default="2026-09-10", type=date.fromisoformat)
    args = parser.parse_args()
    result = build(
        args.anchor,
        args.diagnostic,
        args.index,
        args.changes,
        args.conditional,
        args.code_change_pdf,
        args.output,
        end=args.end,
    )
    print(json.dumps(result["validation"]))


if __name__ == "__main__":
    main()
