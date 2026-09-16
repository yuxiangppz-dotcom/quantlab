"""Optional provider acquisition of CSI800 monthly observations, never PIT certification."""

import calendar
import json
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import complete, verify_completed
from quantlab.research.ml.io import sha256, write_json


def index_observations(client, output, start, end):
    """Client must be ObservedClient; caller explicitly authorizes provider access.

    This stores observation dates, weights, actual download times and raw hashes.
    The separate universe compiler deliberately refuses this schema as effective
    membership evidence. Empty/partial/duplicate snapshots never become 'complete'.
    """
    if start > end or end > datetime.now(UTC).date():
        raise ValueError("invalid index observation interval")
    output = Path(output)
    month = date(start.year, start.month, 1)
    result = []
    with exclusive_job(output):
        while month <= end:
            last = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
            left, right = max(start, month), min(end, last)
            folder = output / f"{left}_{right}"
            if folder.exists():
                verify_completed(folder)
                receipt = json.loads((folder / "observation.json").read_text())
                for path, digest in receipt["raw_responses"].items():
                    if sha256(path) != digest:
                        raise ValueError("index raw observation changed")
            else:
                before = len(client.records)
                frame = client.index_weight(
                    index_code="000906.SH",
                    start_date=left.strftime("%Y%m%d"),
                    end_date=right.strftime("%Y%m%d"),
                )
                required = {"index_code", "con_code", "trade_date", "weight"}
                if frame.empty or not required.issubset(frame):
                    raise ValueError("index observation missing fields/rows")
                frame = frame[sorted(required)].copy()
                dates = pd.to_datetime(frame.trade_date, format="%Y%m%d", errors="raise").dt.date
                if (
                    not dates.between(left, right).all()
                    or not frame.index_code.eq("000906.SH").all()
                    or frame.duplicated(["trade_date", "con_code"]).any()
                ):
                    raise ValueError("index observation scope/identity mismatch")
                for _, group in frame.groupby("trade_date"):
                    weights = pd.to_numeric(group.weight, errors="raise")
                    if (
                        len(group) != 800
                        or not np.isfinite(weights).all()
                        or (weights < 0).any()
                        or abs(weights.sum() - 100) > 0.1
                    ):
                        raise ValueError("incomplete CSI800 monthly snapshot/weights")
                receipt = {
                    "schema": "quantlab_index_observations_v1",
                    "index": "000906.SH",
                    "start": str(left),
                    "end": str(right),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "raw_responses": dict(client.records[before:]),
                    "effective_intervals_certified": False,
                    "historical_known_at_certified": False,
                }
                if not receipt["raw_responses"]:
                    raise ValueError("raw provider observations required")
                with TemporaryDirectory(dir=output) as temp:
                    stage = Path(temp) / "observation"
                    stage.mkdir()
                    frame.to_parquet(stage / "weights.parquet", index=False)
                    write_json(stage / "observation.json", receipt)
                    complete(stage)
                    stage.rename(folder)
            result.append(str(folder))
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return {"status": "complete", "observations": result, "effective_membership_certified": False}
