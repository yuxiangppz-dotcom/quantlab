"""One project configuration; all paths are relative to its file, never shell cwd."""

import json
import math
from datetime import date
from pathlib import Path

PATHS = (
    "canonical",
    "raw",
    "receipts",
    "workspace",
    "account",
    "registry",
    "ml_config",
    "context",
    "availability",
    "execution_policy",
    "corporate_actions",
)


def load_project(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    required = {
        "schema",
        *PATHS,
        "indices",
        "benchmark",
        "start",
        "end",
        "test_start",
        "test_end",
        "capital_cny",
        "provider_interval",
    }
    if set(value) != required or value["schema"] != "quantlab_project_v1":
        raise ValueError("unexpected project configuration fields/schema")
    for name in PATHS:
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError(f"missing project path:{name}")
        value[name] = (path.parent / value[name]).resolve()
    if not value["indices"] or value["benchmark"] not in value["indices"]:
        raise ValueError("benchmark must be included in downloaded indices")
    if type(value["capital_cny"]) is not int or value["capital_cny"] <= 0:
        raise ValueError("capital_cny must be a positive integer")
    bounds = [date.fromisoformat(value[k]) for k in ("start", "test_start", "test_end", "end")]
    if bounds != sorted(bounds):
        raise ValueError("require start <= test_start <= test_end <= end")
    if (
        isinstance(value["provider_interval"], bool)
        or not math.isfinite(value["provider_interval"])
        or value["provider_interval"] < 0
    ):
        raise ValueError("invalid provider interval")
    roots = [value[x] for x in ("canonical", "raw", "receipts", "workspace", "account", "registry")]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise ValueError("source/archive/receipts/workspace paths must be disjoint")
    return value
