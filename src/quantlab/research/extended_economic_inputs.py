"""Bind the already closed raw partitions, prediction files and lifecycle evidence."""

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.backtest.delisting_facts import load_validated_facts
from quantlab.backtest.lifecycle import LEGACY_DELIST_DATE_INCLUSIVE, LifecycleMonitor
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.dataset import _build_delist_dates, _build_list_dates
from quantlab.research.extended_annual_review import read_verification
from quantlab.research.extended_completion_assembly import read_assembly
from quantlab.research.extended_completion_protocol import OUTPUT as ANNUAL
from quantlab.research.extended_economic_contract import (
    CONTRACT,
    END,
    KEYS,
    SIGNAL_END,
    START,
    make_targets,
    target_manifest,
)
from quantlab.research.extended_frequency_protocol import OLD
from quantlab.research.round2_dataset import InputBinding

HISTORY = "data/products/alpha158_staging/alpha158_history_20260911"
REPORT = "729926c1c8276af6834fbe7536ac4fbb87b056cad79039930b6c99dbacdb4cd8"
PROOF = "2447285f9c3f16223493f69167ce977dc09940076e8d8acd002731e9379f50de"


def bound_json(binding, path, expected=None):
    d = json.loads(binding.read(path))
    if d.get("fingerprint") != canonical_payload_fingerprint(
        {k: v for k, v in d.items() if k != "fingerprint"}
    ) or (expected is not None and d["fingerprint"] != expected):
        raise DataValidationError(f"economic source receipt changed: {path}")
    return d


def verified_bytes(binding, path, expected):
    data = binding.read(path)
    if binding.entries[path.resolve().relative_to(binding.root).as_posix()] != expected:
        raise DataValidationError(f"economic source file changed: {path}")
    return data


def load_policies(root, binding, report, config):
    frames = {}
    for slot, ref in report["references"].items():
        folder = root / ref["folder"]
        raw = binding.read(folder / "predictions.parquet")
        name = (folder / "predictions.parquet").relative_to(root).as_posix()
        if binding.entries[name]["sha256"] != ref["prediction_file_sha256"]:
            raise DataValidationError("saved prediction bytes differ from completed annual report")
        reuse_slot = config["model_specs"][slot]["reuse_slot"]
        model = (
            root / OLD / "fits" / reuse_slot / "model.pkl"
            if reuse_slot is not None
            else folder / "model.pkl"
        )
        binding.read(model)
        if (
            binding.entries[model.relative_to(root).as_posix()]["sha256"]
            != ref["saved_model_sha256"]
        ):
            raise DataValidationError("saved model reference changed")
        f = pd.read_parquet(io.BytesIO(raw), use_threads=False)
        f["trade_date"] = pd.to_datetime(f.trade_date)
        if len(f) != ref["prediction_rows"]:
            raise DataValidationError("saved prediction row count changed")
        frames[slot] = f
    policies = {}
    for kind in ("ridge", "lightgbm"):
        for policy in ("weekly", "monthly"):
            parts = []
            for week in config["weeks"]:
                slot = week["week_id"] if policy == "weekly" else week["monthly_anchor"]
                f = frames[f"{slot}_{kind}"]
                part = f.loc[f.trade_date.isin(pd.to_datetime(week["prediction_sessions"]))]
                if len(part) != week["prediction_rows"]:
                    raise DataValidationError("economic saved weekly mapping changed")
                parts.append(part)
            policies[f"{kind}_{policy}"] = pd.concat(parts, ignore_index=True)
    return policies


def adjusted_marks(raw, inventory):
    if raw[KEYS].isna().any().any() or raw.duplicated(KEYS).any():
        raise DataValidationError("raw economic marks have duplicate or missing identities")
    if not raw.instrument_id.isin(inventory["instruments"]).all():
        raise DataValidationError("raw economic marks escaped historical identity inventory")
    raw = raw.copy()
    raw["trade_date"] = pd.to_datetime(raw.trade_date)
    start = pd.to_datetime(raw.instrument_id.map(inventory["list_dates"]))
    end = pd.to_datetime(raw.instrument_id.map(inventory["delist_dates"]))
    known = start.notna()
    active = known & raw.trade_date.ge(start) & (end.isna() | raw.trade_date.le(end))
    price_ok = np.isfinite(raw.close) & raw.close.gt(0)
    factor_ok = np.isfinite(raw.adj_factor) & raw.adj_factor.gt(0)
    values = raw.close * raw.adj_factor
    valid = active & price_ok & factor_ok & np.isfinite(values) & values.gt(0)
    reason = np.select(
        [~known, ~active, ~price_ok, ~factor_ok, ~np.isfinite(values) | values.le(0)],
        [
            "unknown_lifecycle",
            "inactive_lifecycle",
            "invalid_close",
            "invalid_factor",
            "invalid_adjusted_mark",
        ],
        default="valid",
    )
    frame = raw[KEYS].assign(adj_close=values.where(valid), mark_reason=reason)
    frame["trade_date"] = frame.trade_date.dt.date
    return frame.sort_values(KEYS).reset_index(drop=True)


def load_market(root, binding):
    base = root / HISTORY
    inventory = bound_json(binding, base / "inventory.json")
    closed = bound_json(binding, base / "report.json")
    if (
        inventory["historical_market_coverage_complete"] is not False
        or closed["inventory_fingerprint"] != inventory["fingerprint"]
        or closed["status"] != "complete"
    ):
        raise DataValidationError("historical raw inventory is not the closed declared population")
    structural = [
        "data/canonical/calendar/calendar.parquet",
        "data/canonical/securities/securities.parquet",
        "config/security_code_changes.csv",
    ]
    for name in structural:
        verified_bytes(binding, root / name, inventory["source_files"][name])
    storage = ParquetStorage(root / "data/canonical")
    securities = storage.load_securities()
    changes = load_security_code_changes(root / structural[-1])
    calendar = storage.load_trading_calendar()
    listed, delisted = (
        _build_list_dates(securities, changes),
        _build_delist_dates(securities, changes),
    )
    for name, mapping in (("list_dates", listed), ("delist_dates", delisted)):
        if inventory[name] != {
            i: str(mapping[i]) if mapping.get(i) else None for i in inventory["instruments"]
        }:
            raise DataValidationError("historical security identities or lifecycle dates changed")
    sessions = [d for d in inventory["sessions"] if START <= d <= END]
    declared_raw = {(s["date"], s["kind"]): s for s in inventory["sources"]}
    for day in sessions:
        for kind in ("daily", "adj_factor"):
            source = declared_raw.get((day, kind))
            if source is None or source["status"] != "present":
                raise DataValidationError(f"required whole economic source missing: {day} {kind}")
    if sessions != sorted(
        {str(c.trade_date) for c in calendar if c.is_open and START <= str(c.trade_date) <= END}
    ):
        raise DataValidationError("frozen market calendars disagree")
    parts = []
    for month in sorted({d[:7] for d in sessions}):
        name = f"raw-{month}"
        r = bound_json(
            binding, base / "receipts" / f"{name}.json", closed["partition_receipts"][name]
        )
        count = 0
        for path, entry in sorted(r["artifacts"].items()):
            raw = verified_bytes(binding, base / path, entry)
            if not path.endswith(".parquet"):
                continue
            f = pd.read_parquet(
                io.BytesIO(raw), columns=[*KEYS, "close", "adj_factor"], use_threads=False
            )
            if not pd.to_datetime(f.trade_date).dt.strftime("%Y-%m").eq(month).all():
                raise DataValidationError("raw economic partition month differs")
            count += len(f)
            parts.append(f.loc[pd.to_datetime(f.trade_date).isin(pd.to_datetime(sessions))])
        if count != r["result"]["raw_rows"]:
            raise DataValidationError("raw economic partition row population changed")
        for name, entry in r["result"]["source_files"].items():
            if inventory["source_files"].get(name) != entry:
                raise DataValidationError("raw receipt escaped inventory source lineage")
    marks = adjusted_marks(pd.concat(parts, ignore_index=True), inventory)
    facts_path = root / CONTRACT["risk_facts_path"]
    binding.read(facts_path)
    facts, _, errors = load_validated_facts(
        facts_path, [(c.trade_date, c.is_open) for c in calendar]
    )
    if errors:
        raise DataValidationError("; ".join(errors))
    monitor = LifecycleMonitor(securities, changes, mode=LEGACY_DELIST_DATE_INCLUSIVE)
    return marks, sessions, monitor, facts, inventory


def collect(root):
    root = Path(root).resolve()
    binding = InputBinding(root)
    report = read_assembly(root)
    proof = read_verification(root, report)
    if report["fingerprint"] != REPORT or proof is None or proof["fingerprint"] != PROOF:
        raise DataValidationError("complete annual prediction proof is required")
    bound_json(binding, root / ANNUAL / "combined/report.json", REPORT)
    bound_json(binding, root / ANNUAL / "independent_annual" / f"{REPORT}.json", PROOF)
    plan = bound_json(binding, root / ANNUAL / "plan.json")
    policies = load_policies(root, binding, report, plan["config"])
    marks, sessions, monitor, facts, inventory = load_market(root, binding)
    expected_dates = [d for d in sessions if d <= SIGNAL_END]
    for f in policies.values():
        if sorted(f.trade_date.dt.strftime("%Y-%m-%d").unique()) != expected_dates:
            raise DataValidationError("policy signal market coverage changed")
        if len(f) != plan["config"]["policy_members_per_model"]:
            raise DataValidationError("policy full member population changed")
    targets = make_targets(policies, sessions)
    binding.check()
    summary = {
        "contract": CONTRACT,
        "annual_report": REPORT,
        "annual_proof": PROOF,
        "history_inventory": inventory["fingerprint"],
        "inputs": binding.entries,
        "targets": target_manifest(targets),
        "sessions": sessions,
        "mark_rows": len(marks),
        "mark_reasons": marks.mark_reason.value_counts().to_dict(),
    }
    return summary, marks, targets, monitor, facts
