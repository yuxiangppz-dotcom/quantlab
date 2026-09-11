from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_native import (
    FIELDS,
    KEYS,
    apply_evidence_mask,
    compare_native,
    load_contract,
    map_inputs,
)
from quantlab.research.qlib_adapter import to_qlib_static_loader


def source_frame():
    sessions = pd.bdate_range("2025-02-03", periods=30)
    frame = pd.DataFrame(
        {
            "instrument_id": "600000.SH",
            "trade_date": sessions,
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": 1000.0,
            "amount": 10500.0,
            "adj_factor": 2.0,
        }
    )
    return frame, sessions, {"600000.SH": date(2000, 1, 1)}


def test_mapping_units_adjustment_and_labels_are_explicit():
    frame, sessions, lists = source_frame()
    mapped, evidence = map_inputs(frame, sessions, list(lists), lists, {})
    assert mapped.close.eq(22).all()
    assert mapped.volume.eq(500).all()
    assert mapped.vwap.eq(21).all()
    assert evidence.derived_raw_vwap.eq(10.5).all()
    assert mapped[FIELDS].dtypes.eq("float32").all()
    changed = frame.assign(future_return_5d=999, label_end_date="2099-01-01")
    pd.testing.assert_frame_equal(map_inputs(changed, sessions, list(lists), lists, {})[0], mapped)
    # Incorrect share/lot units are visible, not silently corrected from the outcome.
    wrong = frame.assign(volume=frame.volume * 100)
    bad, observed = map_inputs(wrong, sessions, list(lists), lists, {})
    assert bad.vwap.isna().all()
    assert observed.exclusion_reason.eq("derived_vwap_outside_bar").all()
    assert observed.volume.eq(100000).all()


def test_missing_zero_volume_and_calendar_gaps_preserve_identities():
    frame, sessions, lists = source_frame()
    frame.loc[3, ["volume", "amount"]] = 0
    frame.loc[4, "adj_factor"] = np.nan
    frame = frame.drop(index=2)
    mapped, evidence = map_inputs(frame, sessions, list(lists), lists, {})
    assert len(mapped) == 30
    assert mapped.loc[2, FIELDS].isna().all()
    assert np.isnan(mapped.loc[3, "volume"]) and np.isnan(mapped.loc[3, "vwap"])
    assert mapped.loc[3, "close"] == 22
    assert evidence.loc[3, "volume"] == 0
    assert evidence.loc[2, "exclusion_reason"] == "missing_daily"
    assert mapped.loc[4, FIELDS].isna().all()


def test_code_switch_never_backfills_or_stitches_codes():
    frame, sessions, _ = source_frame()
    frame = pd.concat([frame.assign(instrument_id=code) for code in ["300114.SZ", "302132.SZ"]])
    lists = {"300114.SZ": date(2010, 1, 1), "302132.SZ": date(2025, 2, 17)}
    mapped, evidence = map_inputs(
        frame, sessions, list(lists), lists, {"300114.SZ": date(2025, 2, 16)}
    )
    old = mapped.instrument_id.eq("300114.SZ")
    new = ~old
    switch = mapped.trade_date.ge("2025-02-17")
    assert mapped.loc[old & switch, FIELDS].isna().all().all()
    assert mapped.loc[new & ~switch, FIELDS].isna().all().all()
    assert len(evidence) == 60
    native = mapped[KEYS].copy()
    contract = {"features": [{"name": "MA5", "dependencies": ["close"], "lookback_sessions": 4}]}
    native["MA5"] = 1.0
    # Supply native values in sorted identity order, as the native API promises.
    native = native.sort_values(KEYS).reset_index(drop=True)
    guarded, _ = apply_evidence_mask(native, mapped, contract)
    first_new = guarded.loc[
        guarded.instrument_id.eq("302132.SZ") & guarded.trade_date.ge("2025-02-17")
    ]
    assert first_new.MA5.iloc[:4].isna().all()
    assert first_new.MA5.iloc[4] == 1


def test_strict_masks_cannot_turn_native_boolean_defaults_into_complete_evidence():
    frame, sessions, lists = source_frame()
    mapped, _ = map_inputs(frame.drop(index=10), sessions, list(lists), lists, {})
    contract = {"features": [{"name": "COUNT5", "dependencies": ["close"], "lookback_sessions": 5}]}
    native = mapped[KEYS].assign(COUNT5=0.0)
    guarded, coverage = apply_evidence_mask(native, mapped, contract)
    assert guarded.COUNT5.iloc[:5].isna().all()
    assert guarded.COUNT5.iloc[10:16].isna().all()
    assert guarded.COUNT5.iloc[16] == 0.0
    assert coverage[0]["incomplete_but_native_finite_rows"] == 11
    corrupted = native.iloc[1:].reset_index(drop=True)
    with pytest.raises(DataValidationError, match="membership"):
        apply_evidence_mask(corrupted, mapped, contract)


def test_contract_and_input_tampering_fail_closed(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "qlib_alpha158_audit_v1.json").write_text('{"feature_count":999}')
    with pytest.raises(DataValidationError, match="contract"):
        load_contract(tmp_path)
    frame, sessions, lists = source_frame()
    with pytest.raises(DataValidationError, match="duplicate"):
        map_inputs(pd.concat([frame, frame.iloc[:1]]), sessions, list(lists), lists, {})
    with pytest.raises(DataValidationError, match="calendar"):
        map_inputs(frame, sessions[::-1], list(lists), lists, {})
    with pytest.raises(DataValidationError, match="lifecycle"):
        map_inputs(frame, sessions, list(lists), {}, {})
    contract = {"features": [{"name": "MA5"}]}
    left = frame[KEYS].assign(MA5=1.0)
    right = left.copy()
    right.loc[0, "MA5"] = np.nan
    assert compare_native(left, right, contract)[0]["mismatch_rows"] == 1


@pytest.mark.parametrize("feature", ["future_return_5d", "LABEL0", "label_end_date"])
def test_qlib_allowlist_rejects_labels_before_loading_optional_runtime(feature):
    frame, _, _ = source_frame()
    frame[feature] = 1.0
    with pytest.raises(ValueError, match="labels"):
        to_qlib_static_loader(frame, [feature])


def test_report_rejects_tampering_incomplete_parity_and_authority(tmp_path):
    from quantlab.research.alpha158_audit import load_audit_report
    from quantlab.research.round2_dataset import sealed_write

    contract = load_contract()
    report = {
        "contract": contract,
        "status": "complete",
        "new_fit_attempts": 0,
        "performance_eligible": False,
        "execution_authority": False,
        "fresh_forward_evidence": False,
        "strategy_promoted": False,
        "real_prefix_invariant": True,
        "real_future_invariant": True,
        "fixtures": {"all_provider_parity": True},
        "target_grid_rows": 10,
        "features": [
            {
                "name": item["name"],
                "mismatch_rows": 0,
                "target_rows": 10,
                "target_usable_rows": 8,
                "target_native_finite": 9,
            }
            for item in contract["features"]
        ],
        "artifacts": {},
    }

    def attempt(name, payload):
        out = tmp_path / name
        out.mkdir()
        sealed_write(out / "report.json", payload)
        return out

    out = attempt("valid", report)
    assert len(load_audit_report(out, full_verify=True)["features"]) == 158
    for name, updates in (
        ("authority", {"execution_authority": True}),
        ("missing", {"features": report["features"][:-1]}),
        ("mismatch", {"features": [{**item, "mismatch_rows": 1} for item in report["features"]]}),
        ("unfinished", {"status": "running"}),
        ("future", {"real_future_invariant": False}),
        ("fit", {"new_fit_attempts": 1}),
    ):
        with pytest.raises(DataValidationError):
            load_audit_report(attempt(name, {**report, **updates}))
    path = out / "report.json"
    path.write_text(path.read_text().replace('"new_fit_attempts": 0', '"new_fit_attempts": 1'))
    with pytest.raises(DataValidationError, match="fingerprint"):
        load_audit_report(out)
