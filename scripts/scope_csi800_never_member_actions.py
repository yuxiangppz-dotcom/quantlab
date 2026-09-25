#!/usr/bin/env python3
"""Scope reviewed cash-compensation issues unreachable by a flat CSI800 account.

The issue remains in the scoped audit. This does not turn a special payment
into an ordinary dividend or certify its account settlement/tax treatment.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

CODE = "002192.SZ"
RECORD_DATES = {"2018-04-24", "2019-04-23"}
START = date(2018, 1, 1)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scope(
    corporate: dict,
    membership: dict,
    calendar: pd.DataFrame,
    code_change_rows: list[dict],
    review: dict,
    raw_root: Path,
    source_root: Path,
    bindings: dict[str, str],
) -> tuple[dict, list[dict]]:
    if corporate["coverage"].get("minimum_replay_date") != START.isoformat():
        raise ValueError("flat account inception is not 2018-01-01")
    if (
        membership.get("schema") != "quantlab_index_membership_v1"
        or membership.get("index") != "000906.SH"
        or membership.get("semantics") != "published_effective_intervals"
    ):
        raise ValueError("historical CSI800 membership is not certified")
    if any(CODE in {r["old_instrument_id"], r["new_instrument_id"]} for r in code_change_rows):
        raise ValueError("security identity changed")
    facts = [x for x in review["facts"] if x["record_date"] in RECORD_DATES]
    if {x["record_date"] for x in facts} != RECORD_DATES or len(facts) != 2:
        raise ValueError("two reviewed compensation facts required")
    unresolved = list(corporate["coverage"]["unresolved"])
    scoped = list(corporate["coverage"].get("out_of_scope_historical_issues", []))
    end = max(date.fromisoformat(d) for d in RECORD_DATES)
    dates = pd.to_datetime(calendar["trade_date"]).dt.date
    relevant = calendar.loc[(dates >= START) & (dates <= end)].copy()
    relevant["trade_date"] = dates[(dates >= START) & (dates <= end)]
    open_days = []
    for day in pd.date_range(START, end).date:
        rows = relevant[relevant["trade_date"].eq(day)]
        if len(rows) != 2 or set(rows["exchange"]) != {"SSE", "SZSE"}:
            raise ValueError(f"incomplete exchange calendar:{day}")
        if len(set(rows["is_open"])) != 1:
            raise ValueError(f"exchange calendars disagree:{day}")
        if bool(rows["is_open"].iloc[0]):
            open_days.append(day)
    if not open_days:
        raise ValueError("no trading sessions in membership proof")
    snapshots = membership["snapshots"]
    for day in open_days:
        selected = [s for s in snapshots if s["start"] <= str(day) <= s["end"]]
        if len(selected) != 1:
            raise ValueError(f"missing or overlapping membership:{day}")
        row = selected[0]
        known_at = pd.Timestamp(row.get("known_at"))
        cutoff = datetime.combine(day, time(18), SHANGHAI)
        if (
            row.get("complete") is not True
            or len(row.get("members", [])) != 800
            or not row.get("source_id")
            or not row.get("revision_id")
            or "monthly_observation" in row["source_id"]
            or known_at.tzinfo is None
            or known_at > cutoff
        ):
            raise ValueError(f"uncertified membership:{day}")
        if CODE in row["members"]:
            raise ValueError(f"account could acquire issue security:{day}")
    for fact in sorted(facts, key=lambda x: x["record_date"]):
        if (
            fact["instrument_id"] != CODE
            or fact["classification"]
            != "restructuring_performance_cash_compensation_not_ordinary_dividend"
            or fact["production_applied"] is not False
        ):
            raise ValueError("unreviewed compensation fact")
        record = fact["record_date"]
        if date.fromisoformat(record) not in open_days:
            raise ValueError(f"record date outside trading calendar:{record}")
        source = source_root / fact["issuer_file"]
        raw_path = raw_root / f"{CODE}.parquet"
        if (
            digest(source) != fact["issuer_sha256"]
            or digest(raw_path) != fact["vendor_rows_sha256"]
        ):
            raise ValueError(f"compensation source changed:{record}")
        raw = pd.read_parquet(raw_path)
        rows = raw[raw["record_date"].eq(record.replace("-", "")) & raw["div_proc"].eq("实施")]
        if (
            len(rows) != 1
            or pd.notna(rows.iloc[0]["ex_date"])
            or rows.iloc[0]["pay_date"] != fact["vendor_pay_date"].replace("-", "")
        ):
            raise ValueError(f"compensation vendor row changed:{record}")
        matches = [
            x
            for x in unresolved
            if x["instrument_id"] == CODE
            and x["record_date"] == record.replace("-", "")
            and x["ex_date"] is None
            and x["reason"] == "missing_ex_date"
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one unresolved issue:{record}")
        if any(
            x["instrument_id"] == CODE and x.get("record_date") in {record, record.replace("-", "")}
            for x in corporate["events"]
        ):
            raise ValueError(f"compensation already executable:{record}")
        unresolved.remove(matches[0])
        proof_days = sum(day <= date.fromisoformat(record) for day in open_days)
        scoped.append(
            {
                "issue": matches[0],
                "classification": "flat_account_never_held_outside_CSI800_before_record_date",
                "account_inception": START.isoformat(),
                "record_date": record,
                "verified_trading_sessions": proof_days,
                "membership_sha256": bindings["membership"],
                "calendar_sha256": bindings["calendar"],
                "code_changes_sha256": bindings["code_changes"],
                "issuer_source_sha256": fact["issuer_sha256"],
                "vendor_rows_sha256": fact["vendor_rows_sha256"],
                "settlement_and_tax_certified": False,
            }
        )
    result = {
        **corporate,
        "coverage": {
            **corporate["coverage"],
            "unresolved": unresolved,
            "out_of_scope_historical_issues": scoped,
        },
    }
    return result, scoped[-2:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "corporate",
        "membership",
        "calendar",
        "code-changes",
        "review",
        "raw-root",
        "source-root",
        "output-dir",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    inputs = {
        "corporate": args.corporate,
        "membership": args.membership,
        "calendar": args.calendar,
        "code_changes": args.code_changes,
        "review": args.review,
    }
    bindings = {name: digest(path) for name, path in inputs.items()}
    with args.code_changes.open(newline="") as f:
        code_change_rows = list(csv.DictReader(f))
    result, newly_scoped = scope(
        json.loads(args.corporate.read_text()),
        json.loads(args.membership.read_text()),
        pd.read_parquet(args.calendar),
        code_change_rows,
        json.loads(args.review.read_text()),
        args.raw_root,
        args.source_root,
        bindings,
    )
    args.output_dir.mkdir(parents=True)
    output = args.output_dir / "corporate_actions.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    receipt = {
        "schema": "quantlab_never_member_action_scope_v1",
        "inputs": bindings,
        "script_sha256": digest(Path(__file__)),
        "newly_scoped": newly_scoped,
        "output_sha256": digest(output),
        "remaining_unresolved": len(result["coverage"]["unresolved"]),
    }
    (args.output_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    print(
        f"scoped={len(newly_scoped)} unresolved={receipt['remaining_unresolved']} output={output}"
    )


if __name__ == "__main__":
    main()
