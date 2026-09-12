"""Calendar-true conditions and observational evidence, not trade simulations."""

import json

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research import s4_pilot
from quantlab.research.s4_pilot import (
    VARIANTS,
    daily_metrics,
    signal_values,
    start_attempt,
    validate_saved_labels,
)


def inputs():
    days = pd.bdate_range("2019-10-08", periods=70)
    codes = ["A", "B", "C"]
    frames = []
    for j, code in enumerate(codes):
        close = (
            100 + np.arange(70, dtype=float)
            if j == 0
            else (200 - np.arange(70, dtype=float) / 2 if j == 1 else np.full(70, 50.0))
        )
        frames.append(
            pd.DataFrame(
                {
                    "instrument_id": code,
                    "trade_date": days,
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "adj_factor": 1.0,
                }
            )
        )
    frame = pd.concat(frames, ignore_index=True)
    warm = frame[frame.trade_date.isin(days[:61])].copy()
    parent = frame[frame.trade_date.isin(days[61:])].copy().reset_index(drop=True)
    parent["adjusted_close"] = parent.close
    parent["F1"] = [-1, 0, 1, 0, 2, np.nan, 1, 2, 3] * 3
    parent["momentum20"] = 0.1
    parent["circ_mv"] = 10000.0
    parent["turnover_rate"] = 1.0
    parent["label_5"] = np.nan
    parent["label_entry_date"] = pd.NaT
    parent["label_exit_date"] = pd.NaT
    return parent, warm, list(days[61:]), list(days[:61]), codes


def compute(bundle=None):
    return signal_values(*(bundle or inputs()))


def test_relative_three_day_and_sixty_day_change_have_literal_values():
    result = compute()
    first = result[result.trade_date == result.trade_date.min()].set_index("instrument_id")
    changes = np.array([161 / 158 - 1, 169.5 / 171 - 1, 0])
    assert first.change3.to_numpy() == pytest.approx(changes)
    assert first["S4-A"].to_numpy() == pytest.approx(np.median(changes) - changes)
    assert first.loc["A", "change60"] == pytest.approx(161 / 101 - 1)
    assert first.loc["B", "change60"] == pytest.approx(169.5 / 199.5 - 1)
    assert first.condition_B.tolist() == [True, False, False]
    assert first["S4-B"].notna().tolist() == [True, False, False]
    assert first.condition_C.isna().all()


def test_strict_cross_equality_unknowns_and_first_day_never_cross_codes():
    result = compute()
    for _, group in result.groupby("instrument_id"):
        condition = group.condition_C.reset_index(drop=True)
        assert pd.isna(condition[0])
        assert condition[1:5].tolist() == [False, True, False, True]
        assert condition[5:7].isna().all()
        assert condition[7:].tolist() == [False, False]
        assert group["S4-C"].notna().tolist() == [
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
        ]


def test_future_price_and_flow_mutations_do_not_change_prior_signals():
    bundle = inputs()
    original = compute(bundle)
    frame = bundle[0].copy()
    future = frame.trade_date >= bundle[2][5]
    for key in ("open", "close", "high", "low", "adjusted_close"):
        frame.loc[future, key] *= 10
    frame.loc[future, "F1"] = -999
    frame["label_5"] = 999.0
    changed = compute((frame, *bundle[1:]))
    columns = ["change3", "change60", *VARIANTS, "condition_A", "condition_B", "condition_C"]
    past = original.trade_date < bundle[2][5]
    pd.testing.assert_frame_equal(original.loc[past, columns], changed.loc[past, columns])


def test_all_sixty_prior_observations_are_required_without_warmup_gap_compression():
    bundle = inputs()
    warm = bundle[1]
    removed = (warm.instrument_id == "A") & (warm.trade_date == bundle[3][1])
    result = compute((bundle[0], warm.loc[~removed], *bundle[2:]))
    a = result[result.instrument_id == "A"].reset_index(drop=True)
    assert pd.isna(a.condition_B[0]) and pd.isna(a.change60[0])
    assert a.condition_B[1]
    assert a["S4-A"].notna().all()


def test_oldest_extra_warmup_day_is_outside_first_sixty_day_change():
    bundle = inputs()
    warm = bundle[1].copy()
    warm.loc[warm.trade_date == bundle[3][0], "close"] = np.nan
    pd.testing.assert_series_equal(
        compute().change60, compute((bundle[0], warm, *bundle[2:])).change60
    )


def test_missing_current_bar_remains_a_session_and_invalidates_entire_three_day_window():
    bundle = inputs()
    frame = bundle[0].copy()
    mask = (frame.instrument_id == "A") & (frame.trade_date == bundle[2][2])
    frame.loc[mask, "close"] = np.nan
    frame.loc[mask, "adjusted_close"] = np.nan
    result = compute((frame, *bundle[1:]))
    a = result[result.instrument_id == "A"].reset_index(drop=True)
    assert len(result) == 27
    assert a.change3.iloc[2:6].isna().all()
    assert pd.notna(a.change3.iloc[6])
    assert a.change60.iloc[2:].isna().all()
    assert a.condition_C.iloc[2] and pd.isna(a["S4-C"].iloc[2])


def test_price_unit_adjustment_uses_adjusted_research_series_only():
    bundle = inputs()
    expected = compute(bundle)
    frame, warm = bundle[0].copy(), bundle[1].copy()
    for source in (frame, warm):
        for key in ("open", "close", "high", "low"):
            source[key] /= 2
        source.adj_factor *= 2
    actual = compute((frame, warm, *bundle[2:]))
    pd.testing.assert_frame_equal(
        expected[["change3", "change60", *VARIANTS]], actual[["change3", "change60", *VARIANTS]]
    )
    assert "fill" not in actual and "nav" not in actual


@pytest.mark.parametrize(
    "kind", ["duplicate", "missing", "extra_code", "extra_date", "price_tamper", "bool_flow"]
)
def test_changed_parent_or_grid_fails_closed(kind):
    bundle = inputs()
    frame = bundle[0].copy()
    if kind == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif kind == "missing":
        frame = frame.iloc[1:]
    elif kind == "extra_code":
        frame.loc[0, "instrument_id"] = "D"
    elif kind == "extra_date":
        frame.loc[0, "trade_date"] = pd.Timestamp("2025-01-01")
    elif kind == "price_tamper":
        frame.loc[0, "adjusted_close"] = 1234
    else:
        frame.F1 = True
    with pytest.raises(DataValidationError):
        compute((frame, *bundle[1:]))


def test_parent_f1_and_label_columns_are_not_recomputed():
    bundle = inputs()
    result = compute(bundle)
    columns = ["F1", "label_5", "label_entry_date", "label_exit_date", "momentum20"]
    pd.testing.assert_frame_equal(result[columns], bundle[0][columns])


def test_label_chronology_must_remain_next_session_to_sixth_and_same_year():
    days = pd.bdate_range("2020-01-02", periods=10)
    frame = pd.DataFrame(
        {
            "trade_date": [days[0]],
            "label_5": [0.1],
            "label_entry_date": [days[1]],
            "label_exit_date": [days[6]],
        }
    )
    validate_saved_labels(frame, days)
    frame.label_entry_date = days[0]
    with pytest.raises(DataValidationError):
        validate_saved_labels(frame, days)
    frame.label_5 = np.nan
    validate_saved_labels(frame, days)
    cross = pd.bdate_range("2020-12-28", periods=10)
    frame = pd.DataFrame(
        {
            "trade_date": [cross[0]],
            "label_5": [0.1],
            "label_entry_date": [cross[1]],
            "label_exit_date": [cross[6]],
        }
    )
    with pytest.raises(DataValidationError):
        validate_saved_labels(frame, cross)


def population():
    n = 50
    f = pd.DataFrame(
        {
            "trade_date": pd.Timestamp("2020-01-02"),
            **{key: np.arange(n, dtype=float) for key in VARIANTS},
            "momentum20": -np.arange(n, dtype=float),
            "label_5": np.arange(n, dtype=float),
            "circ_mv": np.arange(1, n + 1, dtype=float),
            "turnover_rate": np.arange(n, dtype=float),
        }
    )
    for key in ("condition_A", "condition_B", "condition_C"):
        f[key] = pd.Series(True, index=f.index, dtype="boolean")
    return f


def test_sparse_condition_is_not_lowered_to_manufacture_a_result():
    f = population()
    f.loc[:20, "S4-C"] = np.nan
    f.loc[:19, "condition_C"] = False
    f.loc[20, "condition_C"] = pd.NA
    metrics = daily_metrics(f).set_index("variant")
    c = metrics.loc["S4-C"]
    assert c.pairs == 29 and pd.isna(c.rank_ic) and pd.isna(c.reversal_rank_ic)
    assert (
        c.condition_pass_count == 29
        and c.condition_fail_count == 20
        and c.condition_unknown_count == 1
    )
    assert metrics.loc["S4-A", "rank_ic"] == pytest.approx(1)


def test_paired_comparison_uses_exact_same_rows_with_tie_ranks():
    f = population()
    f["S4-A"] = np.floor(f["S4-A"] / 2)
    f.loc[0, "label_5"] = np.nan
    f.loc[1, "momentum20"] = np.nan
    metrics = daily_metrics(f).set_index("variant")
    expected = f.iloc[2:][["S4-A", "label_5"]].corr(method="spearman").iloc[0, 1]
    assert metrics.loc["S4-A", "pairs"] == 48
    assert metrics.loc["S4-A", "rank_ic"] == pytest.approx(expected)
    assert metrics.loc["S4-A", "paired_ic_difference"] == pytest.approx(expected - 1)


def test_constant_reference_clears_both_sides_of_paired_statistics():
    f = population()
    f.momentum20 = 0.0
    m = daily_metrics(f)
    assert m[["rank_ic", "reversal_rank_ic", "paired_ic_difference"]].isna().all().all()


def test_signal_exposures_and_eligibility_do_not_depend_on_future_labels():
    f = population()
    original = daily_metrics(f)
    f.label_5 = np.nan
    changed = daily_metrics(f)
    columns = [k for k in original if k.endswith("correlation") or k.startswith("condition_")]
    pd.testing.assert_frame_equal(original[columns], changed[columns])


def test_consumed_or_unexplained_attempt_is_never_restarted(tmp_path):
    result = start_attempt(tmp_path, "head", "parent", "config", {})
    assert result["candidate_identities_used"] == 3 and result["economic_paths_used"] == 0
    with pytest.raises(DataValidationError, match="consumed"):
        start_attempt(tmp_path, "head", "parent", "config", {})


def test_unexplained_output_does_not_reset_attempt_budget(tmp_path):
    (tmp_path / "partial.csv").write_text("partial")
    with pytest.raises(DataValidationError, match="consumed"):
        start_attempt(tmp_path, "head", "parent", "config", {})


def test_missing_proof_never_computes_or_loads_inputs(tmp_path, monkeypatch):
    config = tmp_path / s4_pilot.CONFIG
    config.parent.mkdir()
    config.write_text(json.dumps({"output": "data/pilot"}))
    monkeypatch.setattr(s4_pilot, "load_inputs", lambda root: pytest.fail("missing proof"))
    assert s4_pilot.read_progress(tmp_path) is None
    assert not (tmp_path / "data/pilot").exists()


@pytest.fixture
def proof_evidence(tmp_path, monkeypatch):
    from quantlab.research.alpha158_store import atomic_seal
    from quantlab.research.input_audit import _sha

    config = {"output": "data/pilot"}
    path = tmp_path / s4_pilot.CONFIG
    path.parent.mkdir()
    path.write_text(json.dumps(config))
    out = tmp_path / "data/pilot"
    out.mkdir(parents=True)
    (out / "signals.parquet").write_bytes(b"fixture signal artifact")
    parent = {"fingerprint": "parent"}
    monkeypatch.setattr(s4_pilot, "load_inputs", lambda root: (config, parent, {}))
    intent = atomic_seal(out / "started.json", {"config_sha256": _sha(path)})
    report_data = {
        "parent_report_fingerprint": "parent",
        "intent_fingerprint": intent["fingerprint"],
        "variant_ids": list(VARIANTS),
        "actual_attempts": 1,
        "candidate_identities_used": 3,
        **dict.fromkeys(s4_pilot.FLAGS, False),
        "provider_calls_used": 0,
        "economic_paths_used": 0,
        "model_fits_used": 0,
        "artifacts": {"signals.parquet": {"sha256": _sha(out / "signals.parquet")}},
    }
    proof_data = {
        **dict.fromkeys(s4_pilot.FLAGS, False),
        "all_signal_rows_verified": True,
        "all_daily_pairs_verified": True,
        "summaries_and_intervals_verified": True,
        "input_hashes_verified": True,
        "artifacts": {},
    }
    return tmp_path, out, report_data, proof_data


@pytest.mark.parametrize(
    "bad", ["none", "authority", "unverified", "different_report", "changed_signal", "spent_path"]
)
def test_proof_gate_rejects_unverified_or_changed_evidence(proof_evidence, bad):
    from quantlab.research.alpha158_store import atomic_seal

    root, out, report, proof = proof_evidence
    if bad == "authority":
        report["execution_authority"] = True
    if bad == "spent_path":
        report["economic_paths_used"] = 1
    sealed = atomic_seal(out / "report.json", report)
    proof["report_fingerprint"] = sealed["fingerprint"]
    if bad == "unverified":
        proof["all_signal_rows_verified"] = False
    if bad == "different_report":
        proof["report_fingerprint"] = "other"
    atomic_seal(out / "independent_proof.json", proof)
    if bad == "changed_signal":
        (out / "signals.parquet").write_bytes(b"changed")
    if bad == "none":
        assert s4_pilot.read_progress(root)["fingerprint"] == sealed["fingerprint"]
    else:
        with pytest.raises(DataValidationError):
            s4_pilot.read_progress(root)


def test_s4_page_without_saved_proof_stays_read_only(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from quantlab.ui import research_program

    monkeypatch.setattr(research_program, "PROJECT_ROOT", tmp_path)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_s4_progress\nrender_s4_progress()"
    ).run()
    assert not app.exception and not app.button
    assert "尚未完成复核" in app.info[0].value
