"""A small, versioned investment mandate, shared by research and daily operation."""

import json
from datetime import date
from pathlib import Path

from quantlab.research.ml.artifacts import fingerprint

EVIDENCE = ("membership", "industries", "event_coverage")


def load_strategy(project):
    path = project.get("strategy")
    if path is None:
        return None  # Frozen v1 experiments retain their original contract.
    value = json.loads(Path(path).read_text())
    required = {
        "schema",
        "index",
        "min_listing_sessions",
        "liquidity_window",
        "min_median_amount_cny",
        "membership_exit",
        "study",
        "capital_scenarios_cny",
        "slippage_bps",
        "release",
        *EVIDENCE,
    }
    if set(value) != required or value["schema"] != "quantlab_csi800_strategy_v1":
        raise ValueError("invalid CSI800 strategy fields/schema")
    if value["index"] != "000906.SH" or value["membership_exit"] != "buffered":
        raise ValueError("require historical CSI800 and buffered membership exits")
    for key in ("min_listing_sessions", "liquidity_window"):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError(f"invalid strategy integer:{key}")
    if type(value["min_median_amount_cny"]) is not int or value["min_median_amount_cny"] < 0:
        raise ValueError("invalid liquidity floor")
    for key in ("capital_scenarios_cny", "slippage_bps"):
        numbers = value[key]
        if (
            not numbers
            or len(set(numbers)) != len(numbers)
            or any(type(n) is not int or n <= 0 for n in numbers)
        ):
            raise ValueError(f"positive distinct strategy scenarios required:{key}")
    if project["capital_cny"] not in value["capital_scenarios_cny"]:
        raise ValueError("account capital must be included in stress scenarios")
    study = value["study"]
    if set(study) != {"development_end", "holdout_start", "holdout_end", "phase"}:
        raise ValueError("invalid study specification")
    d, s, e = (
        date.fromisoformat(study[k]) for k in ("development_end", "holdout_start", "holdout_end")
    )
    if not d < s <= e or study["phase"] not in {"development", "final_holdout"}:
        raise ValueError("invalid study interval/phase")
    release = value["release"]
    if set(release) != {"min_sessions", "max_drawdown", "min_relative_return"}:
        raise ValueError("invalid release criteria")
    import math

    if (
        type(release["min_sessions"]) is not int
        or release["min_sessions"] < 60
        or any(
            type(release[k]) not in (int, float) or not math.isfinite(release[k])
            for k in ("max_drawdown", "min_relative_return")
        )
        or not 0 < release["max_drawdown"] < 1
    ):
        raise ValueError("invalid release thresholds")
    value["strategy_sha256"] = fingerprint(
        {**value, "study": {k: v for k, v in study.items() if k != "phase"}}
    )
    value["policy_sha256"] = fingerprint(
        {
            k: value[k]
            for k in (
                "schema",
                "index",
                "min_listing_sessions",
                "liquidity_window",
                "min_median_amount_cny",
                "membership_exit",
            )
        }
    )
    for key in EVIDENCE:
        value[key] = (Path(path).parent / value[key]).resolve()
    return value
