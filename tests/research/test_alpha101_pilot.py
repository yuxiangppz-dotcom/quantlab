"""Causal formula arithmetic and fixed observational comparison boundaries."""

import json

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha101_pilot import FORMULAS, formula_values, paired_metrics, start_attempt


def rows():
    return pd.DataFrame(
        {
            "instrument_id": ["A"] * 4,
            "trade_date": pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]),
            "open": [10.0, 12.0, 11.0, 13.0],
            "close": [11.0, 11.5, 13.0, 12.0],
            "high": [12.0, 12.5, 14.0, 14.0],
            "low": [9.0, 11.0, 10.0, 11.0],
            "volume": [1000.0, 1200.0, 1000.0, 1000.0],
            "adj_factor": [1.0] * 4,
        }
    )


def compute(frame):
    return formula_values(frame, rows().trade_date.tolist(), ["A"])


def test_literal_formulas_have_independent_numeric_examples():
    result = compute(rows())
    assert np.isnan(result.alpha101_12.iloc[0])
    assert result.alpha101_12.iloc[1:].tolist() == [-0.5, 1.5, 0.0]
    assert result.alpha101_101.tolist() == pytest.approx(
        [1 / 3.001, -0.5 / 1.501, 2 / 4.001, -1 / 3.001]
    )


def test_future_price_volume_and_labels_do_not_change_past_signal():
    frame = rows()
    original = compute(frame)
    frame.loc[3, ["open", "close", "high", "low", "volume"]] = [90, 100, 110, 80, 12345]
    frame["label_5"] = [-100, 100, -100, 100]
    altered = compute(frame)
    pd.testing.assert_frame_equal(original.loc[:2, list(FORMULAS)], altered.loc[:2, list(FORMULAS)])


def test_adjustment_step_only_masks_the_raw_cross_event_pair():
    frame = rows()
    frame.loc[2:, "adj_factor"] = 1.5
    result = compute(frame)
    assert np.isnan(result.alpha101_12.iloc[2])
    assert result.alpha101_12.iloc[3] == 0
    assert result.alpha101_101.iloc[2] == pytest.approx(2 / 4.001)


@pytest.mark.parametrize(
    "column,value", [("volume", 0), ("close", np.nan), ("high", 8), ("low", np.inf)]
)
def test_missing_invalid_bar_is_not_bridged(column, value):
    frame = rows()
    frame.loc[1, column] = value
    result = compute(frame)
    assert result.loc[1:2, "alpha101_12"].isna().all()
    assert np.isnan(result.alpha101_101.iloc[1])


def test_flat_bar_yields_zero_indicator_without_tradability_claim():
    frame = rows()
    frame.loc[1, ["open", "high", "low", "close"]] = 10
    result = compute(frame)
    assert result.alpha101_101.iloc[1] == 0
    assert "tradable" not in result and "fill" not in result


def test_session_gap_and_duplicate_fail_instead_of_collapsing_time():
    frame = rows()
    with pytest.raises(DataValidationError):
        compute(frame.drop(index=1))
    with pytest.raises(DataValidationError):
        compute(pd.concat([frame, frame.iloc[:1]]))


def test_previous_day_never_leaks_from_other_security():
    frame = pd.concat([rows(), rows().assign(instrument_id="B")])
    result = formula_values(frame, rows().trade_date.tolist(), ["A", "B"])
    assert result.groupby("instrument_id").head(1).alpha101_12.isna().all()


def test_volume_unit_scale_preserves_formula_values():
    frame = rows()
    expected = compute(frame)
    frame.volume *= 100
    pd.testing.assert_series_equal(expected.alpha101_12, compute(frame).alpha101_12)


def test_boolean_measure_is_unknown_input_not_numeric_truth():
    frame = rows()
    frame.volume = True
    with pytest.raises(DataValidationError):
        compute(frame)


def population():
    n = 50
    return pd.DataFrame(
        {
            "trade_date": pd.Timestamp("2020-01-02"),
            "alpha101_12": np.arange(n, dtype=float),
            "alpha101_101": np.arange(n, dtype=float),
            "momentum20": -np.arange(n, dtype=float),
            "label_5": np.arange(n, dtype=float),
            "circ_mv": np.arange(1, n + 1, dtype=float),
            "turnover_rate": np.arange(n, dtype=float),
        }
    )


def test_pairwise_difference_uses_identical_stock_rows_and_dates():
    frame = population()
    frame.loc[0, "alpha101_12"] = np.nan
    frame.loc[1, "momentum20"] = np.nan
    result = paired_metrics(frame).set_index("formula")
    assert result.loc["alpha101_12", "pairs"] == 48
    assert result.loc["alpha101_101", "pairs"] == 49
    assert result.paired_ic_difference.tolist() == pytest.approx([0, 0])
    frame.alpha101_12 = -frame.alpha101_12
    altered = paired_metrics(frame).set_index("formula")
    assert altered.loc["alpha101_12", "paired_ic_difference"] == pytest.approx(-2)


def test_constant_formula_keeps_both_paired_ic_summaries_unknown():
    frame = population()
    frame.alpha101_12 = 0.0
    result = paired_metrics(frame).set_index("formula")
    assert (
        result.loc["alpha101_12", ["rank_ic", "reversal_rank_ic", "paired_ic_difference"]]
        .isna()
        .all()
    )


def test_exposure_checks_do_not_use_future_label_availability():
    frame = population()
    original = paired_metrics(frame)
    frame.label_5 = np.nan
    altered = paired_metrics(frame)
    columns = [c for c in original if c.endswith("correlation")]
    pd.testing.assert_frame_equal(original[columns], altered[columns])
    assert altered.rank_ic.isna().all()


def test_failed_started_attempt_is_never_reused(tmp_path):
    started = start_attempt(tmp_path, "head", "parent", "config")
    with pytest.raises(DataValidationError, match="consumed"):
        start_attempt(tmp_path, "head", "parent", "config")
    assert started["actual_attempt"] == 1 and started["model_fits_used"] == 0


def test_unexplained_partial_output_does_not_reset_attempt(tmp_path):
    (tmp_path / "partial.csv").write_text("partial")
    with pytest.raises(DataValidationError):
        start_attempt(tmp_path, "head", "parent", "config")


def test_missing_proof_never_loads_parent_or_runs_pilot(tmp_path, monkeypatch):
    from quantlab.research import alpha101_pilot

    config = tmp_path / alpha101_pilot.CONFIG
    config.parent.mkdir()
    config.write_text(json.dumps({"output": "data/pilot"}))
    monkeypatch.setattr(alpha101_pilot, "load_inputs", lambda root: pytest.fail("no proof"))
    assert alpha101_pilot.read_progress(tmp_path) is None
    assert not (tmp_path / "data/pilot").exists()


def test_missing_alpha_evidence_page_does_not_start_research(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from quantlab.ui import research_program

    monkeypatch.setattr(research_program, "PROJECT_ROOT", tmp_path)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_alpha_progress\nrender_alpha_progress()"
    ).run()
    assert not app.exception
    assert "尚未完成复核" in app.info[0].value
    assert not app.button
