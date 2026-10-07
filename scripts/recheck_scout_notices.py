"""Save a separate, read-only CNINFO notice comparison for a completed Scout report."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from quantlab.scout.models import SHANGHAI, fingerprint
from quantlab.scout.recheck import compare_announcement_index
from quantlab.scout.sources import collect_cninfo_announcements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_json", type=Path)
    parser.add_argument("output_json", type=Path)
    args = parser.parse_args()
    if args.report_json.resolve().parent in args.output_json.resolve().parents:
        parser.error("output must be outside the archived run directory")
    if args.output_json.exists():
        parser.error("output already exists; choose a new path")
    report = json.loads(args.report_json.read_text(encoding="utf-8"))
    manifest = json.loads(args.report_json.with_name("manifest.json").read_text(encoding="utf-8"))
    if manifest.get("report_sha256") != fingerprint(report):
        parser.error("archived report does not match its manifest")
    if report["status"] != "live_research_unvalidated":
        parser.error("source report must be a completed live research run")
    now = datetime.now(SHANGHAI)
    codes = [item["instrument_id"] for item in report["selection"]["selected"]]
    evidence, coverage = collect_cninfo_announcements(
        {"cninfo_announcements": True, "lookback_hours": report["config"]["lookback_hours"]},
        now,
        True,
        codes,
        post_selection=True,
    )
    comparison = compare_announcement_index(report, evidence, coverage, datetime.now(SHANGHAI))
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output_json.open("x", encoding="utf-8") as output:
            json.dump(comparison, output, ensure_ascii=False, indent=2)
            output.write("\n")
    except FileExistsError:
        parser.error("output already exists; choose a new path")
    print(args.output_json.resolve())


if __name__ == "__main__":
    main()
