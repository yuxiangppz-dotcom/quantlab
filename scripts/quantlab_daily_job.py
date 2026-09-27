"""Local scheduler entry point; provider access and deployment are explicit.

Run around 17:20 Asia/Shanghai with --project config/project.local.json.
Add --sync --execute only with authority to call the provider/write Canonical.
The frozen strategy cutoff and the verified trading calendar remain authoritative.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quantlab.data.storage import ParquetStorage
from quantlab.pipeline.cli import main as pipeline
from quantlab.pipeline.config import load_project
from quantlab.pipeline.ingestion import calendar_days
from quantlab.research.alpha158_store import exclusive_job


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.sync and not args.execute:
        parser.error("--sync requires --execute for provider/Canonical access")
    project = load_project(args.project)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    common = ["--project", str(args.project.resolve())]
    with exclusive_job(project["workspace"] / "scheduler"):
        if args.sync:
            code = pipeline(
                [*common, "sync", "--start", str(today), "--end", str(today), "--execute"]
            )
            if code:
                return code
        days = calendar_days(
            ParquetStorage(project["canonical"]).load_trading_calendar(), today, today
        )
        if not days:
            print(json.dumps({"status": "not_a_trading_session", "date": str(today)}))
            return 0
        for command in (
            ["daily", "--as-of", str(today)],
            ["health"],
            ["monitor", "--as-of", str(today)],
        ):
            code = pipeline([*common, *command])
            if code:
                return code
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from None
