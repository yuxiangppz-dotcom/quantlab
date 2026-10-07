"""Group sizes follow the actual input; repairs keep all unreported fields."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from test_daily_contract import inputs, output

from quantlab.scout.daily_contract import compact_fact_refs
from quantlab.scout.daily_correction import apply_patch_output, patch_plan
from quantlab.scout.daily_stages import (
    discovery_contract,
    investigation_contract,
    investigation_errors,
)


@pytest.mark.parametrize(
    "text", ["基础化工资金博弈", "否则仅资金博弈", "需观察新增订单，否则仅资金博弈"]
)
def test_qualitative_funding_risk_is_not_a_claim_about_fund_numbers(text):
    from quantlab.scout.daily_semantics import prose_errors

    assert prose_errors(text, {}) == []


@pytest.mark.parametrize(
    "text",
    [
        "资金流窗口净额均为负",
        "资金博弈净额均为负",
        "资金博弈流入",
        "资金博弈2000万元",
        "资金博弈推动上涨",
    ],
)
def test_funding_assertions_still_require_facts(text):
    from quantlab.scout.daily_semantics import prose_errors

    assert "unbound_quantitative_interpretation" in prose_errors(text, {})


@pytest.mark.parametrize("count", [1, 5, 13, 24, 160])
def test_group_size_is_derived_from_input_not_fixed_four(count):
    pool = [{"instrument_id": f"000{i:03d}.SZ"} for i in range(count)]
    ids = [c["instrument_id"] for c in pool]
    schemas = [
        discovery_contract(nextday=True, instrument_ids=ids),
        investigation_contract(pool),
    ]
    for schema in schemas:
        node = schema["properties"]["hypotheses"]["items"]["properties"]["instrument_ids"]
        validator = Draft202012Validator(node)
        assert node["maxItems"] == count
        assert validator.is_valid(ids)
        assert validator.is_valid(ids[:5])
        assert not validator.is_valid([ids[0], ids[0]])
        assert not validator.is_valid(["999999.SH"])


def test_unbound_investigation_prose_uses_one_field_patch_and_retains_counterevidence():
    packet, _ = compact_fact_refs(inputs())
    schema = investigation_contract(packet["candidates"], packet)
    previous = {
        "hypotheses": [],
        "opportunities": [
            {"instrument_id": r["instrument_id"], "analysis": deepcopy(r["analysis"])}
            for r in output()["comparisons"]
        ],
    }
    previous["opportunities"][0]["analysis"]["incremental_change"] = "资金流窗口净额均为负"
    before = deepcopy(previous)
    errors = investigation_errors(previous, packet, schema)
    assert any(e["code"] == "unbound_quantitative_interpretation" for e in errors)
    plan = patch_plan(previous, errors, schema)
    path = "/opportunities/0/analysis/incremental_change"
    assert plan and set(plan["targets"]) == {path}
    # The model must supply a bound fact or state the gap. No program-authored
    # positive interpretation is substituted for the original funding concern.
    corrected = "资金证据尚未绑定，保留资金风险待核实"
    patched, patch_errors = apply_patch_output(
        previous, {"patches": [{"path": path, "op": "replace", "value": corrected}]}, plan
    )
    assert patch_errors == [] and investigation_errors(patched, packet, schema) == []
    expected = deepcopy(before)
    expected["opportunities"][0]["analysis"]["incremental_change"] = corrected
    assert patched == expected and previous == before
    bad, _ = apply_patch_output(
        previous,
        {"patches": [{"path": path, "op": "replace", "value": "资金流窗口净额均为负"}]},
        plan,
    )
    assert investigation_errors(bad, packet, schema)
    _, unauthorized = apply_patch_output(
        previous,
        {
            "patches": [
                {
                    "path": "/opportunities/1/analysis/incremental_change",
                    "op": "replace",
                    "value": corrected,
                }
            ]
        },
        plan,
    )
    assert unauthorized


@pytest.mark.parametrize("valid_patch", [True, False])
def test_actual_daily_investigation_performs_only_one_directed_correction(valid_patch):
    from quantlab.scout.daily_budget import DailyResearch, DailyValidationError

    packet, _ = compact_fact_refs(inputs())
    schema = investigation_contract(packet["candidates"], packet)
    previous = {
        "hypotheses": [],
        "opportunities": [
            {"instrument_id": r["instrument_id"], "analysis": deepcopy(r["analysis"])}
            for r in output()["comparisons"]
        ],
    }
    previous["opportunities"][0]["analysis"]["incremental_change"] = "资金流窗口净额均为负"
    path = "/opportunities/0/analysis/incremental_change"

    class Stub:
        model = "offline-only"

        def __init__(self):
            self.calls = []

        def ask(self, prompt, contract):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                result = deepcopy(previous)
            else:
                assert len(self.calls) == 2 and "patches" in contract["properties"]
                result = {
                    "patches": [
                        {
                            "path": path,
                            "op": "replace",
                            "value": "资金证据尚未绑定，原风险待核实"
                            if valid_patch
                            else "程序事实：[[fact:missing]]",
                        }
                    ]
                }
            return result, {"usage": {"total_tokens": 100}, "choices": []}

    transport = Stub()
    client = DailyResearch(transport)
    kwargs = {"validator": lambda value: investigation_errors(value, packet, schema)}
    if valid_patch:
        result, _ = client.ask("fixed source input", schema, **kwargs)
        expected = deepcopy(previous)
        expected["opportunities"][0]["analysis"]["incremental_change"] = (
            "资金证据尚未绑定，原风险待核实"
        )
        assert result == expected
    else:
        with pytest.raises(DailyValidationError):
            client.ask("fixed source input", schema, **kwargs)
    assert len(transport.calls) == 2 and client.repairs == 1
    assert client.correction["mode"] == "single_directed_model_patch"
    assert client.correction["authorized_paths"] == [path]
