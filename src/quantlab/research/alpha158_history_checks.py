"""A priori native parity, causal samples and historical overlap checks."""

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_audit import (
    OUTPUT_DIRECTORY,
    evaluate_pair,
    load_audit_report,
    require_parity,
)
from quantlab.research.alpha158_native import FIELDS, KEYS


def compare_overlap(actual, expected, names, config):
    actual, expected = (
        frame.sort_values(KEYS).reset_index(drop=True) for frame in (actual, expected)
    )
    if not actual[KEYS].astype(str).equals(expected[KEYS].astype(str)):
        raise DataValidationError("frozen Alpha158 overlap identity mismatch")
    a, b = actual[names].to_numpy(), expected[names].to_numpy()
    finite_a, finite_b = np.isfinite(a), np.isfinite(b)
    if not np.array_equal(finite_a, finite_b) or not np.array_equal(np.isnan(a), np.isnan(b)):
        raise DataValidationError("frozen Alpha158 overlap missingness mismatch")
    same = np.isclose(
        a, b, rtol=config["overlap_rtol"], atol=config["overlap_atol"], equal_nan=True
    )
    if not same.all():
        raise DataValidationError(
            f"frozen Alpha158 numerical overlap mismatch: {int((~same).sum())}"
        )
    return {
        "rows": len(actual),
        "features": len(names),
        "mismatches": 0,
        "max_absolute_difference": float(np.abs(a[finite_a] - b[finite_a]).max())
        if finite_a.any()
        else None,
    }


def frozen_overlap(root, native, usable, codes, config, native_contract):
    selected = sorted(set(codes) & set(config["sample_codes"]))
    if not selected:
        return None
    out = root / OUTPUT_DIRECTORY
    reference = load_audit_report(out, full_verify=True)
    if reference["fingerprint"] != config["reference_report_fingerprint"]:
        raise DataValidationError("frozen five-code audit changed")
    start, end = reference["contract"]["start"], reference["contract"]["end"]
    names = [x["name"] for x in native_contract["features"]]
    checks = {}
    for kind, frame in (("native", native), ("usable", usable)):
        expected = pd.read_parquet(out / f"{kind}_features.parquet")
        checks[kind] = compare_overlap(
            frame.loc[frame.instrument_id.isin(selected) & frame.trade_date.between(start, end)],
            expected.loc[
                expected.instrument_id.isin(selected) & expected.trade_date.between(start, end)
            ],
            names,
            config,
        )
    return {"codes": selected, **checks}


def causal_sample(mapped, folder, contract):
    """First lexicographic code; 160 sessions from first mapped close (or calendar start)."""
    code = sorted(mapped.instrument_id.unique())[0]
    frame = mapped.loc[mapped.instrument_id.eq(code)].reset_index(drop=True)
    valid = np.flatnonzero(np.isfinite(frame.close))
    first = int(valid[0]) if len(valid) else 0
    # Keep a fixed 160-session slice whenever that much history exists.
    first = min(first, max(0, len(frame) - 160))
    sample = frame.iloc[first : first + 160].copy().reset_index(drop=True)
    sessions = pd.DatetimeIndex(sample.trade_date)
    native, comparisons = evaluate_pair(sample, folder / "sample", sessions, contract)
    count = min(101, len(sample))
    prefix, _ = evaluate_pair(sample.iloc[:count], folder / "prefix", sessions[:count], contract)
    require_parity(native.iloc[:count], prefix, contract)
    changed = sample.assign(future_return_5d=-999.0)
    changed.loc[count:, FIELDS] *= 3
    changed_native, _ = evaluate_pair(changed, folder / "future", sessions, contract)
    require_parity(native.iloc[:count], changed_native.iloc[:count], contract)
    return {
        "code": code,
        "start": str(sessions[0].date()),
        "end": str(sessions[-1].date()),
        "expressions": len(comparisons),
        "provider_parity": True,
        "prefix_invariant": True,
        "future_and_label_invariant": True,
    }
