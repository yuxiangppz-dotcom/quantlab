"""One lagged F7 exploratory association; no PIT, profit or execution authority."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.funding_pilot import (
    block_interval,
    nullable_mean,
    rank_correlation,
    valid_numeric,
)
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/f7_financing_diagnostic_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/f7_financing_diagnostic"
CONFIG_FINGERPRINT = "65b1d60e875a70cd354064956f432a935eca5aeed4e520001bdda871f39bbbf3"
KEYS = ["instrument_id", "trade_date"]
COPY = ["momentum20", "label_5", "label_entry_date", "label_exit_date", "circ_mv", "turnover_rate"]


def _grid(frame, field, days, codes):
    data = frame[[*KEYS, field]].copy()
    data["trade_date"] = pd.to_datetime(data.trade_date, errors="raise")
    if (
        data[KEYS].isna().any().any()
        or data.duplicated(KEYS).any()
        or not data.instrument_id.isin(codes).all()
        or not data.trade_date.isin(days).all()
    ):
        raise DataValidationError("unknown/duplicate/out-of-window financing input key")
    return data.set_index(KEYS).reindex(pd.MultiIndex.from_product([codes, days], names=KEYS))[
        field
    ]


def financing_features(amounts, balances, sessions, codes, decisions):
    days, decisions = pd.DatetimeIndex(sessions), pd.DatetimeIndex(decisions)
    if (
        not codes
        or list(codes) != sorted(set(codes))
        or len(days) < 7
        or days.has_duplicates
        or not days.is_monotonic_increasing
        or days.tz is not None
        or days.hasnans
        or not days.normalize().equals(days)
        or not len(decisions)
        or decisions.has_duplicates
        or not decisions.is_monotonic_increasing
        or not decisions.isin(days[6:]).all()
    ):
        raise DataValidationError("financing calendar must include six ordered warmup sessions")
    grid = pd.DataFrame(
        {
            "amount": _grid(amounts, "amount", days, codes),
            "rzye": _grid(balances, "rzye", days, codes),
        }
    ).reset_index()
    grid["amount"] = valid_numeric(grid.amount, positive=True)
    grid["rzye"] = valid_numeric(grid.rzye)
    parts = []
    for _, g in grid.groupby("instrument_id", sort=False):
        g = g.copy()
        balance, amount = g.rzye, g.amount
        complete_balances = balance.rolling(6, min_periods=6).count().eq(6)
        complete_amounts = amount.rolling(5, min_periods=5).count().eq(5)
        denominator = amount.rolling(5, min_periods=5).sum()
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            raw = ((balance - balance.shift(5)) / denominator).where(
                complete_balances & complete_amounts & np.isfinite(denominator) & denominator.gt(0)
            )
        g["F7"] = raw.where(np.isfinite(raw)).shift(1)
        g["source_trade_date"] = g.trade_date.shift(1)
        g["source_rzye"] = balance.shift(1)
        g["source_rzye_five_sessions_before"] = balance.shift(6)
        g["source_amount5_cny"] = denominator.shift(1)
        g["balance_window_complete"] = complete_balances.shift(1, fill_value=False)
        g["amount_window_complete"] = complete_amounts.shift(1, fill_value=False)
        parts.append(g[g.trade_date.isin(decisions)].drop(columns=["amount", "rzye"]))
    return pd.concat(parts, ignore_index=True).sort_values(KEYS).reset_index(drop=True)


def daily_metrics(frame):
    records = []
    for day, g in frame.groupby("trade_date", sort=True):
        common = g[["F7", "momentum20", "label_5"]].replace([np.inf, -np.inf], np.nan).dropna()
        score, pairs = rank_correlation(common.F7, common.label_5, 30)
        reference, _ = rank_correlation(-common.momentum20, common.label_5, 30)
        if score is None or reference is None:
            score = reference = None
        size, _ = rank_correlation(g.F7, np.log(g.circ_mv.where(g.circ_mv > 0)), 30)
        turnover, _ = rank_correlation(g.F7, g.turnover_rate, 30)
        overlap, _ = rank_correlation(g.F7, -g.momentum20, 30)
        records.append(
            {
                "trade_date": day,
                "feature_valid": int(np.isfinite(g.F7).sum()),
                "balance_complete": int(g.balance_window_complete.sum()),
                "amount_complete": int(g.amount_window_complete.sum()),
                "pairs": pairs,
                "rank_ic": score,
                "reference_rank_ic": reference,
                "paired_difference": score - reference if score is not None else None,
                "size_rank_correlation": size,
                "turnover_rank_correlation": turnover,
                "reversal_rank_correlation": overlap,
            }
        )
    return pd.DataFrame(records)


def summaries(metrics):
    periods = [(str(y), y, y) for y in range(2020, 2025)] + [
        ("2020-2022", 2020, 2022),
        ("2023-2024", 2023, 2024),
        ("2020-2024", 2020, 2024),
    ]
    columns = [
        "rank_ic",
        "reference_rank_ic",
        "paired_difference",
        "size_rank_correlation",
        "turnover_rank_correlation",
        "reversal_rank_correlation",
    ]
    result = []
    for name, start, end in periods:
        g = metrics[metrics.trade_date.dt.year.between(start, end)]
        result.append(
            {
                "period": name,
                "calendar_sessions": len(g),
                "valid_ic_sessions": int(g.rank_ic.notna().sum()),
                "paired_observations": int(g.pairs.sum()),
                **{"mean_" + c: nullable_mean(g[c]) for c in columns},
                "ic_block_95_interval": block_interval(g.rank_ic),
                "difference_block_95_interval": block_interval(g.paired_difference),
            }
        )
    return result


def load_contract(root):
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    if canonical_payload_fingerprint(config) != CONFIG_FINGERPRINT:
        raise DataValidationError("fixed financing diagnostic contract changed")
    verify_entries(root, config["inputs"])
    for path, entry in config["inputs"].items():
        if (root / path).stat().st_size != entry["bytes"]:
            raise DataValidationError("financing input byte length changed")
    selection = sealed_read(root / config["selection_path"])
    parent, proof = (
        sealed_read(root / config["parent_report_path"]),
        sealed_read(root / config["parent_proof_path"]),
    )
    intake = sealed_read(root / config["intake_path"] / "report.json")
    intake_proof = sealed_read(root / config["intake_path"] / "proof.json")
    if any(
        x["fingerprint"] != config["pins"][key]
        for key, x in (
            ("selection", selection),
            ("parent_report", parent),
            ("intake_report", intake),
            ("intake_proof", intake_proof),
        )
    ):
        raise DataValidationError("financing prerequisite pin changed")
    if (
        proof["report_fingerprint"] != parent["fingerprint"]
        or intake_proof["report_fingerprint"] != intake["fingerprint"]
        or intake["requests_attempted"] != 256
        or intake["stop_reason"] != "request_scope_exhausted"
        or config["inputs"][config["parent_path"]]
        != parent["artifacts"]["features_and_labels.parquet"]
        or any(
            x[k] is not False
            for x in (parent, intake)
            for k in ("historical_pit_certified", "performance_evidence", "execution_authority")
        )
    ):
        raise DataValidationError("financing source proof/authority mismatch")
    calendar = sealed_read(root / config["calendar_path"])
    days = calendar["sessions"]
    sessions = days[days.index("2019-12-24") : days.index("2024-12-31") + 1]
    decisions = [d for d in sessions if d >= "2020-01-02"]
    if len(sessions) != 1218 or len(decisions) != 1212 or len(selection["instrument_ids"]) != 256:
        raise DataValidationError("financing fixed calendar/population changed")
    return config, selection["instrument_ids"], sessions, decisions


def read_frames(root, config, codes):
    parent = pd.read_parquet(
        root / config["parent_path"], columns=[*KEYS, "amount", *COPY], use_threads=False
    )
    amounts = [parent[[*KEYS, "amount"]]]
    for path in config["warmup_paths"]:
        warm = pd.read_parquet(root / path, columns=[*KEYS, "amount"], use_threads=False)
        expected = pd.Timestamp(path.rsplit("/", 1)[-1].removesuffix(".parquet"))
        if not pd.to_datetime(warm.trade_date).eq(expected).all() or warm.duplicated(KEYS).any():
            raise DataValidationError("warmup amount partition date/key changed")
        amounts.append(warm[warm.instrument_id.isin(codes)])
    rows = []
    for code in codes:
        path = root / config["intake_path"] / "attempts" / ("margin_" + code)
        result = sealed_read(path / "result.json")
        verify_entries(path, result["artifacts"])
        payload = json.loads((path / "response.body").read_bytes())["data"]
        for values in payload["items"]:
            row = dict(zip(payload["fields"], values, strict=True))
            if row["ts_code"] != code:
                raise DataValidationError("financing response mapped to another security")
            rows.append(
                {
                    "instrument_id": code,
                    "trade_date": pd.Timestamp(row["trade_date"]),
                    "rzye": row["rzye"],
                }
            )
    return (
        parent,
        pd.concat(amounts, ignore_index=True),
        pd.DataFrame(rows, columns=[*KEYS, "rzye"]),
    )


def run(root):
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("financing diagnostic requires pushed code")
    config, codes, sessions, decisions = load_contract(root)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("F7 actual segment already consumed")
        budget = Budget(out, config["resources"])
        budget.check()
        intent = atomic_seal(
            out / "intent.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "config_sha256": _sha(root / CONFIG),
                "funding_identity": "F7",
                "funding_identities_cumulative": 5,
                "actual_attempt": 1,
                "provider_calls": 0,
                "economic_paths": 0,
                "model_fits": 0,
            },
        )
        try:
            with budget.watchdog():
                parent, amounts, balances = read_frames(root, config, codes)
                features = financing_features(amounts, balances, sessions, codes, decisions)
                expected_keys = pd.MultiIndex.from_product(
                    [codes, pd.DatetimeIndex(decisions)], names=KEYS
                )
                if not pd.MultiIndex.from_frame(parent[KEYS]).sort_values().equals(expected_keys):
                    raise DataValidationError("financing parent grid changed")
                features = features.merge(
                    parent[[*KEYS, *COPY]], on=KEYS, how="left", validate="one_to_one"
                )
                if len(features) != 310272:
                    raise DataValidationError("financing output grid changed")
                metrics = daily_metrics(features)
                summary = summaries(metrics)
                features.to_parquet(out / "features.parquet", index=False)
                metrics.to_parquet(out / "daily_metrics.parquet", index=False)
                metrics.to_csv(out / "daily_metrics.csv", index=False)
                artifacts = {
                    n: {"sha256": _sha(out / n), "bytes": (out / n).stat().st_size}
                    for n in ("features.parquet", "daily_metrics.parquet", "daily_metrics.csv")
                }
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "funding_identity": "F7",
                        "funding_identities_cumulative": 5,
                        "grid_rows": len(features),
                        "feature_valid": int(features.F7.notna().sum()),
                        "feature_unknown": int(features.F7.isna().sum()),
                        "summaries": summary,
                        "artifacts": artifacts,
                        "provider_calls": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "historical_pit_certified": False,
                        "performance_evidence": False,
                        "execution_authority": False,
                        "candidate_promotion_eligible": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "generated_bytes": budget.check(),
                        },
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {"error_type": type(exc).__name__, "intent_fingerprint": intent["fingerprint"]},
            )
            raise
