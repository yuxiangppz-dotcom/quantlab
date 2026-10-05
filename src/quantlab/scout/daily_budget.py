"""Fixed four-request ceiling, one correction, conservative token reservations."""

from __future__ import annotations

import json
import time
from types import MappingProxyType

from jsonschema import Draft202012Validator

from quantlab.scout.ai import SYSTEM
from quantlab.scout.daily_contract import compact
from quantlab.scout.models import fingerprint

POLICY = {
    "max_calls": 4,
    "max_repairs": 1,
    "max_output_tokens": 32768,
    "max_input_chars": 180000,
    "max_input_bytes": 300000,
    "max_total_tokens": 1200000,
    "request_timeout_seconds": 180,
    "run_timeout_seconds": 1800,
}


class DailyBudgetError(ValueError):
    pass


class DailyValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("daily_output_invalid")


class DailyResearch:
    def __init__(self, client, *, journal=None, progress=None, started_clock=None):
        self.client = client
        self.model = client.model
        self.policy = MappingProxyType(dict(POLICY))
        self.journal = journal
        self.progress = progress or (lambda _: None)
        self.started_clock = time.monotonic() if started_clock is None else started_clock
        self.spent = 0
        self.repairs = 0
        self.requests = []
        self.failed_response = None
        self.last_errors = []
        self.stage = 0

    @property
    def calls(self):
        return self.client.calls

    def check_input(self, prompt, schema, *, extra_chars=0, extra_bytes=0):
        if len(prompt) + extra_chars > self.policy["max_input_chars"]:
            raise DailyBudgetError("daily_input_char_budget")
        # Include the schema and system prompt, not just user text.
        size = len(prompt.encode()) + len(json.dumps(schema).encode()) + len(SYSTEM.encode()) + 1000
        if size + extra_bytes > self.policy["max_input_bytes"]:
            raise DailyBudgetError("daily_input_byte_budget")
        return size + self.policy["max_output_tokens"]

    def _save(self, index, name, value):
        if self.journal:
            self.journal.mkdir(parents=True, exist_ok=True)
            path = self.journal / f"{index:02d}-{name}.json"
            with path.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)

    def _call(self, prompt, schema):
        self.failed_response = None
        if len(self.requests) >= self.policy["max_calls"]:
            raise DailyBudgetError("daily_call_budget")
        if time.monotonic() - self.started_clock >= self.policy["run_timeout_seconds"]:
            raise DailyBudgetError("daily_run_timeout")
        reservation = self.check_input(prompt, schema)
        if self.spent + reservation > self.policy["max_total_tokens"]:
            raise DailyBudgetError("daily_total_token_budget")
        self.spent += reservation
        record = {
            "stage": self.stage,
            "prompt": prompt,
            "schema": schema,
            "prompt_sha256": fingerprint(prompt),
            "reserved_tokens": reservation,
            "delivery_status": "attempted_delivery_unknown",
        }
        self.requests.append(record)
        index = len(self.requests)
        self._save(index, "request", record)
        self.progress(f"模型阶段 {self.stage}，请求 {index}/{self.policy['max_calls']}")
        raw = None
        try:
            result, raw = self.client.ask(prompt, schema)
        finally:
            raw = raw or getattr(self.client, "failed_response", None)
            self.failed_response = raw
            if raw is not None:
                record["delivery_status"] = "response_received"
                record["raw_response"] = raw
                usage = raw.get("usage", {})
                total = usage.get("total_tokens")
                if isinstance(total, int) and total >= 0:
                    self.spent += total - reservation
                    record["charged_tokens"] = total
                else:
                    record["charged_tokens"] = reservation
                    record["token_charge_unknown"] = True
                self._save(index, "response", raw)
            else:
                record["charged_tokens"] = reservation
                record["token_charge_unknown"] = True
            self._save(
                index,
                "receipt",
                {k: v for k, v in record.items() if k not in {"prompt", "schema", "raw_response"}},
            )
        if self.spent > self.policy["max_total_tokens"]:
            raise DailyBudgetError("daily_total_token_budget_exceeded")
        return result, raw

    def _attempt(self, prompt, schema, validator):
        try:
            result, raw = self._call(prompt, schema)
            errors = validator(result) if validator else []
        except DailyBudgetError:
            raise
        except ValueError:
            raw = self.failed_response
            if raw is None:
                raise  # Unknown delivery: no transport retry.
            choice = raw.get("choices", [{}])[0]
            try:
                result = json.loads(choice.get("message", {}).get("content"))
            except (ValueError, TypeError):
                result = None
            errors = [
                {"code": "adapter_invalid_response", "finish_reason": choice.get("finish_reason")}
            ]
            if result is None:
                errors.append({"code": "invalid_or_truncated_json"})
            else:
                errors.extend(
                    validator(result)
                    if validator
                    else [
                        {"path": list(e.path), "code": "schema", "detail": e.message[:300]}
                        for e in Draft202012Validator(schema).iter_errors(result)
                    ]
                )
        self.last_errors = errors
        self.requests[-1]["validation_errors"] = errors
        self._save(len(self.requests), "errors", errors)
        return result, raw, errors

    def ask(self, prompt, schema, search=False, validator=None):
        if search:
            raise ValueError("Daily input uses collected sources only")
        self.stage += 1
        previous, raw, errors = self._attempt(prompt, schema, validator)
        if not errors:
            self.failed_response = None
            return previous, raw
        if self.repairs >= self.policy["max_repairs"]:
            raise DailyValidationError(errors)
        self.repairs += 1
        # The full response/error list is archived. Resend only the original facts
        # and a compact complete error matrix, never previous replies/history.
        matrix = [
            [
                e.get("path", []),
                e["code"],
                e.get("detail", "")[:120],
                {k: v for k, v in e.items() if k not in {"path", "code", "detail"}},
            ]
            for e in errors
        ]
        decision_keys = (
            "instrument_id",
            "primary_type",
            "rank",
            "final_status",
            "comparator_id",
            "fact_ids",
        )
        decisions = (
            [
                [r.get(k) for k in decision_keys]
                for r in (previous or {}).get("comparisons", [])
                if isinstance(r, dict)
            ]
            if isinstance(previous, dict)
            else []
        )
        correction = (
            prompt + "\n这是唯一一次定向纠错。保留反证，不改变预算或来源。"
            "按全部错误清单纠正并输出完整schema JSON。\n"
            "保留此前未报错的分级和比较决定，不另选一套名单。比较对象必须同类型且unselected。"
            "禁止词净流入/净流出改用对应资金事实占位符，保留资金反证；H5等固定期限可保留。"
            "新使用的占位符务必加入该行fact_ids。只有报错处需要修复，并维护全局排名一致性。\n"
            + compact(
                {
                    "error_columns": ["path", "code", "detail", "context"],
                    "errors": matrix,
                    "decision_columns": list(decision_keys),
                    "previous_decisions": decisions,
                }
            )
        )
        result, raw, errors = self._attempt(correction, schema, validator)
        if errors:
            raise DailyValidationError(errors)
        self.failed_response = None
        return result, raw

    def summary(self):
        return {
            "policy": dict(self.policy),
            "requests": len(self.requests),
            "repairs": self.repairs,
            "charged_or_reserved_tokens": self.spent,
            "errors": self.last_errors,
            "request_hashes": [r["prompt_sha256"] for r in self.requests],
        }
