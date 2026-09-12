from copy import deepcopy
from datetime import date

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.extended_economic_contract import CONTROL, POLICIES, scenarios
from quantlab.research.extended_economic_review import comparable_prefix, validate_review


def proof_fixture():
    plan = {
        "fingerprint": "1998aba5fc0d8022fe3c359b85c26e04e967f752827e0bcbc878e7cb75263590",
        "code_head": "source",
    }
    common = {
        "execution_authority": False,
        "complete_user_fee_accounting": False,
        "historical_market_coverage_complete": False,
    }
    report = {
        "fingerprint": "eb265b2145ca2a8ab8d5b1095eb57222d955dd8e3336348b263521f095de9feb",
        "plan": plan["fingerprint"],
        "source_head": "source",
        "status": "complete",
        "finished_paths": 60,
        "consumed_paths": 60,
        "scenarios": [],
        **common,
    }
    statuses = []
    for s in scenarios():
        summary = {
            "scenario": s,
            "engine_status": "completed",
            "valid_through": "2026-09-07",
            "valid_sessions": 247,
            "position_rows": 100,
            "plan": plan["fingerprint"],
            "source_head": "source",
            **common,
        }
        report["scenarios"].append({"scenario": s, "status": "finished", "summary": summary})
        statuses.append(
            {
                "scenario": s["id"],
                "status": "completed",
                "valid_through": "2026-09-07",
                "sessions": 247,
                "position_rows": 100,
            }
        )
    proof = {
        "fingerprint": "c970ab1981d907bb0d5ce649238f0d29bbb56df03bddd2c7f2c83320a2670ad9",
        "report_fingerprint": report["fingerprint"],
        "plan_fingerprint": plan["fingerprint"],
        "source_head": "source",
        "verified_paths": 60,
        "independent_target_rows": 444072,
        "independent_raw_marks": 1278503,
        "all_12_strategy_control_groups_symmetric": True,
        "capital_scale_invariance_checked": True,
        "all_original_inputs_unchanged": True,
        "extra_engine_runs": 0,
        "new_fits": 0,
        "provider_calls": 0,
        "statuses": statuses,
        "path_receipts": {s["id"]: "receipt" for s in scenarios()},
        "max_absolute_cash_position_residual": 2e-16,
        **common,
    }
    return report, plan, proof


def test_closed_proof_passes():
    validate_review(*proof_fixture())


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "report",
        "paths",
        "authority",
        "fees",
        "coverage",
        "count_type",
        "residual",
        "status",
        "end",
        "new_fit",
        "receipt",
    ],
)
def test_changed_evidence_cannot_gain_a_verified_label(change):
    report, plan, proof = deepcopy(proof_fixture())
    if change == "source":
        proof["source_head"] = "other"
    elif change == "report":
        report["fingerprint"] = "other"
    elif change == "paths":
        proof["statuses"].pop()
    elif change == "authority":
        report["scenarios"][0]["summary"]["execution_authority"] = True
    elif change == "fees":
        proof["complete_user_fee_accounting"] = True
    elif change == "coverage":
        proof["historical_market_coverage_complete"] = True
    elif change == "count_type":
        proof["verified_paths"] = 60.0
    elif change == "residual":
        proof["max_absolute_cash_position_residual"] = 0.1
    elif change == "status":
        report["scenarios"][0]["summary"]["engine_status"] = "other"
    elif change == "end":
        report["scenarios"][0]["summary"]["valid_through"] = "2026-09-08"
    elif change == "new_fit":
        proof["new_fits"] = 1
    else:
        proof["path_receipts"].pop(next(iter(proof["path_receipts"])))
    with pytest.raises(DataValidationError):
        validate_review(report, plan, proof)


def frames():
    f = pd.DataFrame(
        {
            "trade_date": [date(2025, 9, d) for d in (1, 2, 3)],
            "value_after_declared_friction": [1.0, 0.9, 5.0],
            "declared_friction_charge": [0.0, 0.001, 0.001],
        }
    )
    result = {name: f.copy() for name in POLICIES}
    result[CONTROL] = result[CONTROL].iloc[:2].copy()
    return result


def test_comparison_uses_mechanical_common_prefix_and_retains_negative_result():
    table, curve = comparable_prefix(frames())
    assert len(curve) == 2
    assert table.common_end.unique().tolist() == ["2025-09-02"]
    assert table.sessions.tolist() == [2] * 5
    assert table.return_after_declared_friction.tolist() == pytest.approx([-0.1] * 5)
    assert table.drawdown.tolist() == pytest.approx([-0.1] * 5)
    assert table.difference_from_cohort_control.tolist() == [0.0] * 5


@pytest.mark.parametrize("change", ["missing_policy", "gap", "duplicate", "different_basis", "nan"])
def test_comparison_does_not_fill_missing_dates_or_rebase_another_initial_value(change):
    data = frames()
    key = POLICIES[-1]
    if change == "missing_policy":
        data.pop(key)
    elif change == "gap":
        data[key] = data[key].iloc[[0, 2]]
    elif change == "duplicate":
        data[key] = pd.concat([data[key], data[key].iloc[:1]])
    elif change == "different_basis":
        data[key].loc[0, "value_after_declared_friction"] = 2.0
    else:
        data[key].loc[1, "value_after_declared_friction"] = float("nan")
    with pytest.raises(DataValidationError):
        comparable_prefix(data)
