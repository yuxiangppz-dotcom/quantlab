import json
import time

import pytest

from quantlab.scout.daily_budget import DailyBudgetError, DailyResearch, DailyValidationError


class Fake:
    model = "fixture"

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []
        self.failed_response = None

    def ask(self, prompt, schema):
        self.failed_response = None
        self.calls.append(prompt)
        result = next(self.outputs)
        if isinstance(result, Exception):
            raise result
        raw = {
            "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 100},
        }
        return result, raw


def test_fixed_policy_input_precheck_and_call_ceiling(tmp_path):
    fake = Fake([{}] * 5)
    client = DailyResearch(fake, journal=tmp_path)
    with pytest.raises(TypeError):
        client.policy["max_calls"] = 99
    with pytest.raises(DailyBudgetError):
        client.ask("x" * 180001, {})
    assert fake.calls == []
    for _ in range(4):
        client.ask("small", {})
    with pytest.raises(DailyBudgetError):
        client.ask("small", {})
    assert len(fake.calls) == 4 and client.spent == 400
    assert len(list(tmp_path.glob("*-request.json"))) == 4


def test_single_complete_error_correction_no_growing_history(tmp_path):
    client = DailyResearch(Fake([{"bad": True}, {"bad": True}, {}]), journal=tmp_path)
    errors = [{"code": "first"}, {"code": "second"}]
    with pytest.raises(DailyValidationError) as exc:
        client.ask("original", {}, validator=lambda _: errors)
    assert exc.value.errors == errors
    assert len(client.calls) == 2 and client.repairs == 1
    assert "first" in client.calls[1] and "second" in client.calls[1]
    # A later stage cannot obtain a second repair.
    with pytest.raises(DailyValidationError):
        client.ask("next_stage", {}, validator=lambda _: errors)
    assert len(client.calls) == 3


def test_unknown_delivery_is_retained_and_never_retried(tmp_path):
    fake = Fake([RuntimeError("network")])
    client = DailyResearch(fake, journal=tmp_path)
    with pytest.raises(RuntimeError):
        client.ask("original", {})
    assert len(fake.calls) == 1 and client.repairs == 0
    receipt = json.loads((tmp_path / "01-receipt.json").read_text())
    assert receipt["token_charge_unknown"] and receipt["charged_tokens"] > 32768


def test_deadline_and_reservation_are_checked_before_send():
    fake = Fake([{}])
    client = DailyResearch(fake, started_clock=time.monotonic() - 1801)
    with pytest.raises(DailyBudgetError):
        client.ask("x", {})
    assert fake.calls == []
    client = DailyResearch(fake)
    client.spent = client.policy["max_total_tokens"]
    with pytest.raises(DailyBudgetError):
        client.ask("x", {})
    assert fake.calls == []


def test_second_adapter_failure_collects_all_schema_errors_without_third_call(tmp_path):
    class Invalid(Fake):
        def ask(self, prompt, schema):
            self.calls.append(prompt)
            self.failed_response = {
                "choices": [{"finish_reason": "length", "message": {"content": '{"x": 1}'}}],
                "usage": {"total_tokens": 20},
            }
            raise ValueError("adapter invalid")

    client = DailyResearch(Invalid([]), journal=tmp_path)
    schema = {"type": "object", "required": ["first", "second"]}
    with pytest.raises(DailyValidationError) as exc:
        client.ask("original", schema)
    assert len(client.calls) == 2
    assert sum(e["code"] == "schema" for e in exc.value.errors) == 2
    assert any(e.get("finish_reason") == "length" for e in exc.value.errors)
    assert (tmp_path / "02-errors.json").exists()
