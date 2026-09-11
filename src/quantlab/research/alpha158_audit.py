"""Exclusive five-code Alpha158 mapping audit, without model or performance search."""

from __future__ import annotations

import argparse
import io
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.context_backfill import _calendar
from quantlab.data.models import DataValidationError
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.alpha158_native import (
    FIELDS,
    KEYS,
    RAW_FIELDS,
    apply_evidence_mask,
    compare_native,
    evaluate_native,
    load_contract,
    map_inputs,
    verify_library,
    write_binary_cache,
)
from quantlab.research.dataset import _build_delist_dates, _build_list_dates
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import (
    InputBinding,
    code_binding,
    sealed_read,
    sealed_write,
    verify_entries,
)

OUTPUT_DIRECTORY = "data/products/qlib_alpha158/qlib_alpha158_parity_20260911"


def require_parity(a, b, contract):
    comparisons = compare_native(a, b, contract)
    if any(item["mismatch_rows"] for item in comparisons):
        raise DataValidationError("native Alpha158 numerical or missingness parity failed")
    return comparisons


def evaluate_pair(mapped, folder, sessions, contract):
    write_binary_cache(mapped, folder, sessions)
    primary = evaluate_native(mapped, folder, contract)
    reference = evaluate_native(mapped, folder, contract, memory=True)
    return primary, require_parity(primary, reference, contract)


def synthetic_fixture_checks(folder: Path, contract):
    """Synthetic fixtures only, including independent analytic same-session formulas."""
    folder.mkdir(parents=True, exist_ok=False)
    sessions = pd.bdate_range("2020-01-01", periods=160)
    t = np.arange(len(sessions))
    close = 10 + t / 32 + np.sin(t / 3) / 8
    volume = 10000 + t * 64 + (t % 7) * 256
    raw = pd.DataFrame(
        {
            "instrument_id": "000001.SZ",
            "trade_date": sessions,
            "open": close - 0.125,
            "close": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "volume": volume,
            "amount": (close - 0.0625) * volume,
            "adj_factor": 1.0,
        }
    )
    lists = {"000001.SZ": date(2000, 1, 1)}
    mapped, _ = map_inputs(raw, sessions, ["000001.SZ"], lists, {})
    baseline, normal = evaluate_pair(mapped, folder / "normal", sessions, contract)
    matrix = mapped.set_index(KEYS)
    o, h, low, c, v = (
        matrix[field].to_numpy(dtype=float) for field in ("open", "high", "low", "close", "vwap")
    )
    expected = {
        "KMID": (c - o) / o,
        "KLEN": (h - low) / o,
        "KMID2": (c - o) / (h - low + 1e-12),
        "KUP": (h - np.maximum(o, c)) / o,
        "KUP2": (h - np.maximum(o, c)) / (h - low + 1e-12),
        "KLOW": (np.minimum(o, c) - low) / o,
        "KLOW2": (np.minimum(o, c) - low) / (h - low + 1e-12),
        "KSFT": (2 * c - h - low) / o,
        "KSFT2": (2 * c - h - low) / (h - low + 1e-12),
        "OPEN0": o / c,
        "HIGH0": h / c,
        "LOW0": low / c,
        "VWAP0": v / c,
    }
    for name, values in expected.items():
        np.testing.assert_allclose(baseline[name], values, rtol=1e-5, atol=1e-6)
    # A consistent 2:1 synthetic split must preserve both adjusted fields and factors.
    split = raw.copy()
    split.loc[80:, ["open", "high", "low", "close"]] /= 2
    split.loc[80:, "volume"] *= 2
    split.loc[80:, "adj_factor"] *= 2
    split_mapped, _ = map_inputs(split, sessions, ["000001.SZ"], lists, {})
    pd.testing.assert_frame_equal(mapped, split_mapped)
    split_native, split_comparison = evaluate_pair(
        split_mapped, folder / "split", sessions, contract
    )
    require_parity(baseline, split_native, contract)
    edge = raw.copy()
    edge.loc[90, ["volume", "amount"]] = 0
    edge.loc[100, "adj_factor"] = np.nan
    edge.loc[110:115, ["open", "high", "low", "close"]] = 10.0
    edge.loc[110:115, "amount"] = edge.loc[110:115, "volume"] * 10
    edge = edge.drop(index=80)
    edge_mapped, edge_evidence = map_inputs(edge, sessions, ["000001.SZ"], lists, {})
    edge_native, edge_comparison = evaluate_pair(
        edge_mapped, folder / "gaps_zero_flat", sessions, contract
    )
    guarded, _ = apply_evidence_mask(edge_native, edge_mapped, contract)
    assert guarded.loc[80:139, "MA60"].isna().all()
    assert guarded.loc[90:94, "VMA5"].isna().all()
    assert edge_evidence.loc[80, "exclusion_reason"] == "missing_daily"
    assert np.isnan(guarded.loc[90, "VWAP0"])
    # Altering future raw values/labels, or truncating the future, cannot alter a prefix.
    changed = raw.assign(future_return_5d=-999.0)
    changed.loc[101:, ["open", "high", "low", "close", "amount"]] *= 3
    changed_mapped, _ = map_inputs(changed, sessions, ["000001.SZ"], lists, {})
    changed_native, _ = evaluate_pair(changed_mapped, folder / "future", sessions, contract)
    require_parity(
        baseline.iloc[:101].reset_index(drop=True),
        changed_native.iloc[:101].reset_index(drop=True),
        contract,
    )
    truncated = mapped.iloc[:101].copy()
    short, _ = evaluate_pair(truncated, folder / "prefix", sessions[:101], contract)
    require_parity(baseline.iloc[:101].reset_index(drop=True), short, contract)
    return {
        "synthetic_only": True,
        "native_features_checked": len(normal),
        "analytic_same_session_features_checked": list(expected),
        "all_provider_parity": True,
        "split_adjustment_invariant": True,
        "future_and_label_invariant": True,
        "prefix_invariant": True,
        "missing_history_masked": True,
        "zero_volume_preserved_as_unknown": True,
        "cases": {
            "normal": len(normal),
            "split": len(split_comparison),
            "gaps_zero_flat": len(edge_comparison),
            "future": 158,
            "prefix": 158,
        },
    }


def stage_inputs(root, contract, binding):
    storage = ParquetStorage(root / "data/canonical")
    binding.read(storage.calendar_path)
    binding.read(storage.securities_path)
    changes_path = root / "config/security_code_changes.csv"
    binding.read(changes_path)
    calendar = storage.load_trading_calendar()
    all_dates = sorted({item.trade_date for item in calendar if item.is_open})
    first = next(i for i, day in enumerate(all_dates) if str(day) >= contract["start"])
    if first < contract["warmup_sessions"]:
        raise DataValidationError("insufficient audited calendar warm-up")
    read_start = all_dates[first - contract["warmup_sessions"]]
    _, observed = _calendar(storage, read_start, date.fromisoformat(contract["end"]))
    sessions = sorted({item.trade_date for item in observed if item.is_open})
    sources, rows = [], []
    for day in sessions:
        tables = {}
        for kind, path in (
            ("daily", storage.daily_bars_path(day)),
            ("adj_factor", storage.adj_factor_path(day)),
        ):
            entry = {"dataset": kind, "date": str(day), "path": path.relative_to(root).as_posix()}
            if not path.exists():
                tables[kind] = pd.DataFrame(columns=[*KEYS, *RAW_FIELDS])
                entry["status"] = "missing"
            else:
                frame = pd.read_parquet(io.BytesIO(binding.read(path)), use_threads=False)
                if (
                    frame[KEYS].isna().any().any()
                    or frame.duplicated(KEYS).any()
                    or not pd.to_datetime(frame.trade_date).eq(pd.Timestamp(day)).all()
                ):
                    raise DataValidationError(f"invalid canonical partition: {entry['path']}")
                tables[kind] = frame.loc[frame.instrument_id.isin(contract["instruments"])].copy()
                tables[kind]["trade_date"] = pd.to_datetime(tables[kind].trade_date)
                entry.update(status="present", selected_rows=len(tables[kind]))
            sources.append(entry)
        daily = tables["daily"][[*KEYS, *[field for field in RAW_FIELDS if field != "adj_factor"]]]
        factor = tables["adj_factor"][[*KEYS, "adj_factor"]]
        daily["trade_date"], factor["trade_date"] = (
            pd.to_datetime(daily.trade_date),
            pd.to_datetime(factor.trade_date),
        )
        rows.append(daily.merge(factor, on=KEYS, how="left", validate="one_to_one"))
    raw = pd.concat(rows, ignore_index=True)
    securities = storage.load_securities()
    changes = load_security_code_changes(changes_path)
    mapped, evidence = map_inputs(
        raw,
        sessions,
        contract["instruments"],
        _build_list_dates(securities, changes),
        _build_delist_dates(securities, changes),
    )
    return mapped, evidence, sessions, sources


def run_audit(root: Path = PROJECT_ROOT, *, progress=print):
    from threadpoolctl import threadpool_info

    contract = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    runtime = verify_library(contract)
    pools = threadpool_info()
    if any(item["num_threads"] > 2 for item in pools):
        raise DataValidationError("native audit requires at most two computation threads")
    free = shutil.disk_usage("/mnt/d").free
    if free < contract["max_generated_bytes"] + 1024**3:
        raise DataValidationError("host D drive cannot support the declared cache budget")
    out = root / OUTPUT_DIRECTORY
    out.mkdir(parents=True, exist_ok=False)
    sealed_write(
        out / "started.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "code_head": head,
            "contract": contract,
            "qlib_runtime": runtime,
            "computation_pools": pools,
            "host_D_free_bytes": free,
            "new_fit_attempts": 0,
        },
    )
    try:
        mapped, evidence, sessions, sources = stage_inputs(root, contract, binding)
        evidence.to_parquet(out / "input_evidence.parquet", index=False)
        mapped.to_parquet(out / "mapped_fields.parquet", index=False)
        progress(
            f"staged {len(mapped)} code/session identities; native Alpha158 evaluation", flush=True
        )
        native, comparisons = evaluate_pair(mapped, out / "native_cache", sessions, contract)
        usable, full_coverage = apply_evidence_mask(native, mapped, contract)
        native.to_parquet(out / "native_features.parquet", index=False)
        usable.to_parquet(out / "usable_features.parquet", index=False)
        cutoff = pd.Timestamp(contract["parity"]["causality_cutoff"])
        prefix = mapped.loc[mapped.trade_date.le(cutoff)].copy()
        prefix_sessions = [day for day in sessions if pd.Timestamp(day) <= cutoff]
        before, _ = evaluate_pair(prefix, out / "prefix_cache", prefix_sessions, contract)
        require_parity(
            native.loc[native.trade_date.le(cutoff)].reset_index(drop=True), before, contract
        )
        future = mapped.copy()
        future.loc[future.trade_date.gt(cutoff), FIELDS] *= 3
        changed, _ = evaluate_pair(future, out / "future_cache", sessions, contract)
        require_parity(
            native.loc[native.trade_date.le(cutoff)].reset_index(drop=True),
            changed.loc[changed.trade_date.le(cutoff)].reset_index(drop=True),
            contract,
        )
        progress(
            "real prefix/future invariance passed; running independent synthetic fixtures",
            flush=True,
        )
        fixtures = synthetic_fixture_checks(out / "synthetic_fixtures", contract)
        target = usable.trade_date.between(contract["start"], contract["end"])
        target_evidence = evidence.loc[
            evidence.trade_date.between(contract["start"], contract["end"])
        ]
        coverage = [
            {
                **item,
                "target_rows": int(target.sum()),
                "target_native_finite": int(np.isfinite(native.loc[target, item["name"]]).sum()),
                "target_usable_rows": int(np.isfinite(usable.loc[target, item["name"]]).sum()),
            }
            for item in comparisons
        ]
        for entry in sources:
            if entry["status"] == "missing" and (root / entry["path"]).exists():
                raise DataValidationError("missing source appeared during native audit")
        binding.check()
        if verify_library(contract) != runtime:
            raise DataValidationError("Qlib runtime changed during native audit")
        size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        if size > contract["max_generated_bytes"]:
            raise DataValidationError("native audit exceeded the fixed output budget")
        report = {
            "schema_version": 1,
            "status": "complete",
            "completed_at": datetime.now(UTC).isoformat(),
            "code_head": head,
            "contract": contract,
            "qlib_runtime": runtime,
            "computation_pools": pools,
            "inputs": binding.entries,
            "sources": sources,
            "cache_bytes": size,
            "source_sessions_with_warmup": len(sessions),
            "all_grid_rows": len(mapped),
            "target_grid_rows": int(target.sum()),
            "target_active_rows": int(target_evidence.lifecycle_active.sum()),
            "target_observed_rows": int(target_evidence.daily_observed.sum()),
            "target_exclusions": target_evidence.exclusion_reason.value_counts().to_dict(),
            "target_by_code": [
                {
                    "instrument_id": code,
                    "rows": len(part),
                    "active_rows": int(part.lifecycle_active.sum()),
                    "observed_rows": int(part.daily_observed.sum()),
                }
                for code, part in target_evidence.groupby("instrument_id")
            ],
            "features": coverage,
            "full_window_coverage": full_coverage,
            "fixtures": fixtures,
            "real_prefix_invariant": True,
            "real_future_invariant": True,
            "new_fit_attempts": 0,
            "performance_eligible": False,
            "execution_authority": False,
            "fresh_forward_evidence": False,
            "strategy_promoted": False,
            "limitations": [
                "five fixed codes are not a representative investment universe",
                "native provider parity is not independent formula reimplementation",
                "complete-window eligibility is stricter than Qlib native missing/partial rules",
                "VWAP is derived from amount/volume, not independently observed",
                "retrospective revised provider history; historical knowability not certified",
                "portfolio execution, full costs and corporate actions remain unresolved",
            ],
            "artifacts": {
                p.relative_to(out).as_posix(): {"sha256": _sha(p)}
                for p in sorted(out.rglob("*"))
                if p.is_file()
            },
        }
        return sealed_write(out / "report.json", report)
    except BaseException as exc:
        sealed_write(
            out / "failed.json",
            {
                "status": "failed",
                "at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "new_fit_attempts": 0,
            },
        )
        raise


def load_audit_report(out: Path, *, full_verify=False):
    report = sealed_read(out / "report.json")
    if report["contract"] != load_contract() or report.get("new_fit_attempts") != 0:
        raise DataValidationError("Alpha158 audit identity mismatch")
    for key in (
        "performance_eligible",
        "execution_authority",
        "fresh_forward_evidence",
        "strategy_promoted",
    ):
        if report.get(key) is not False:
            raise DataValidationError("factor parity cannot gain performance/execution authority")
    names = [item["name"] for item in report["contract"]["features"]]
    features = report.get("features", [])
    if (
        report.get("status") != "complete"
        or not (0 <= report["target_active_rows"] <= report["target_grid_rows"])
        or [item.get("name") for item in features] != names
        or report.get("real_prefix_invariant") is not True
        or report.get("real_future_invariant") is not True
        or report.get("fixtures", {}).get("all_provider_parity") is not True
        or any(
            item.get("mismatch_rows") != 0
            or item["target_usable_rows"] > report["target_active_rows"]
            or not (
                0
                <= item["target_usable_rows"]
                <= item["target_native_finite"]
                <= item["target_rows"]
                == report["target_grid_rows"]
            )
            for item in features
        )
    ):
        raise DataValidationError("Alpha158 report is incomplete or has mismatched evidence")
    if full_verify:
        verify_entries(out, report["artifacts"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "verify"))
    args = parser.parse_args()
    if args.action == "run":
        import pyarrow as pa
        from threadpoolctl import threadpool_limits

        pa.set_cpu_count(2)
        pa.set_io_thread_count(2)
        with threadpool_limits(limits=2):
            report = run_audit()
    else:
        report = load_audit_report(PROJECT_ROOT / OUTPUT_DIRECTORY, full_verify=True)
    print(json.dumps({"action": args.action, "fingerprint": report["fingerprint"]}), flush=True)


if __name__ == "__main__":
    main()
