"""Predeclared research boundaries and an append-only trial register."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pandas as pd

from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.ml.artifacts import fingerprint
from quantlab.research.ml.io import research_output, write_json


def initialize(path, development_end, holdout_start, holdout_end):
    research_output(path)
    if not development_end < holdout_start <= holdout_end:
        raise ValueError("development/holdout boundaries must be ordered and disjoint")
    path.mkdir(parents=True, exist_ok=False)
    payload = {
        "development_end": str(development_end),
        "holdout_start": str(holdout_start),
        "holdout_end": str(holdout_end),
        "created_at": datetime.now(UTC).isoformat(),
        "selection_metric": "validation_rank_ic_and_after_cost_robustness",
        "prior_test_exposure_known": False,
        "warning": "registration cannot undo previous exposure to these dates",
    }
    write_json(path / "study.json", payload)
    return payload


def reserve(path, start, end, specification, *, final_holdout=False):
    research_output(path)
    protocol = json.loads((path / "study.json").read_text())
    start, end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    # Hash exactly the JSON representation that will be persisted (tuples become lists).
    specification = json.loads(json.dumps(specification, allow_nan=False))
    key = fingerprint(specification)
    with exclusive_job(path):
        if final_holdout:
            if not (
                pd.Timestamp(protocol["holdout_start"]).date()
                <= start
                <= end
                <= pd.Timestamp(protocol["holdout_end"]).date()
            ):
                raise ValueError("final evaluation must stay within the predeclared holdout")
            claim = path / "holdout_claim.json"
            if claim.exists():
                saved = json.loads(claim.read_text())
                if saved["specification"] != specification:
                    raise ValueError("holdout already claimed by another frozen specification")
            else:
                write_json(claim, {"specification_sha256": key, "specification": specification})
        elif end > pd.Timestamp(protocol["development_end"]).date():
            raise ValueError("development trial crosses the predeclared boundary")
        entry = {
            "specification_sha256": key,
            "specification": specification,
            "start": str(start),
            "end": str(end),
            "final_holdout": final_holdout,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        write_json(path / "trials" / f"{uuid4().hex}.json", entry)
        return entry
