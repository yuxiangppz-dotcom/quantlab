"""Fixed four-request ceiling, one correction, conservative token reservations."""

from __future__ import annotations

import json
import time
from hashlib import sha256
from types import MappingProxyType

from jsonschema import Draft202012Validator

from quantlab.scout.ai import SYSTEM
from quantlab.scout.daily_contract import compact
from quantlab.scout.daily_correction import apply_patch_output, patch_plan
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
        self.correction = None
        self.input_checks = []

    @property
    def calls(self):
        return self.client.calls

    def check_input(self, prompt, schema, *, extra_chars=0, extra_bytes=0):
        size = len(prompt.encode()) + len(json.dumps(schema).encode()) + len(SYSTEM.encode()) + 1000
        check = {
            "prompt_chars": len(prompt),
            "request_bytes": size,
            "reserved_chars": extra_chars,
            "reserved_bytes": extra_bytes,
            "char_limit": self.policy["max_input_chars"],
            "byte_limit": self.policy["max_input_bytes"],
            "before_request_number": len(self.requests) + 1,
            "schema_id": schema.get("$id"),
            "errors": [],
        }
        if len(prompt) + extra_chars > self.policy["max_input_chars"]:
            check["errors"].append("daily_input_char_budget")
        if size + extra_bytes > self.policy["max_input_bytes"]:
            check["errors"].append("daily_input_byte_budget")
        self.input_checks.append(check)
        self._save(0, f"budget-check-{len(self.input_checks):02d}", check)
        if "daily_input_char_budget" in check["errors"]:
            raise DailyBudgetError("daily_input_char_budget")
        # Include the schema and system prompt, not just user text.
        if "daily_input_byte_budget" in check["errors"]:
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
            "prompt_text_sha256": sha256(prompt.encode()).hexdigest(),
            "schema_sha256": fingerprint(schema),
            "schema_id": schema.get("$id"),
            "decision_prompt_version": "scout_next_session_v2"
            if "[SCOUT_NEXT_SESSION_V1]" in prompt
            else "scout_decision_v2"
            if "[SCOUT_DECISION_V2]" in prompt
            else "legacy",
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

    def ask(self, prompt, schema, search=False, validator=None, fact_subjects=None):
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
        nextday = "next_session" in schema.get("$id", "")
        if nextday:
            decision_keys += ("ranking_state", "participation_status")
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
            "按全部错误清单纠正并输出符合schema的数据JSON，不输出schema定义。\n"
            "保留此前未报错的分级和比较决定，不另选一套名单。"
            + (
                "主目标为次日；比较对象来自冻结comparable_ids，可也是入选股。"
                "研究排序与参与状态分开，保留次日条件和事实引用。"
                "重点必须有对应kind的driver和具体mechanism，observation需填写给定D1时点、"
                "指标与预期状态，unknown或待验证/强势继续观察不能支撑focus。"
                "MA20/EMA12/RSI14/ATR14等固定技术术语可保留，读数/百分比仍须事实占位符。"
                if nextday
                else "比较对象必须同类型且unselected。H5等固定期限可保留。"
            )
            + "禁止词净流入/净流出改用对应资金事实占位符，保留资金反证。"
            "事实清单由程序汇总，不需要重复输出fact_ids。只有报错处需要修复，并维护全局排名一致性。\n"
            + compact(
                {
                    "error_columns": ["path", "code", "detail", "context"],
                    "errors": matrix,
                    "decision_columns": list(decision_keys),
                    "previous_decisions": decisions,
                }
            )
        )
        plan = patch_plan(previous, errors, schema, fact_subjects=fact_subjects)
        if plan is not None and validator is not None:
            correction += (
                "\n本次只输出patches补丁JSON，不输出comparisons或完整报告。"
                "只能修改授权path；索引与证券对应原回复，不能重排数组。"
                "未授权字段由程序逐字保留；补丁合并后仍校验完整报告。"
                "日期与资金方向只用已有事实/事件卡展示，不在修改段落里自行写读数或数量。\n"
                + compact({"authorized_fields": plan["context"]})
            )

            def patch_validation(value):
                assembled, patch_errors = apply_patch_output(previous, value, plan)
                return patch_errors or validator(assembled)

            patch, raw, errors = self._attempt(correction, plan["schema"], patch_validation)
            result, patch_errors = apply_patch_output(previous, patch, plan)
            self.correction = {
                "mode": "single_directed_model_patch",
                "stage": self.stage,
                "previous_output_sha256": fingerprint(previous),
                "patch_sha256": fingerprint(patch),
                "assembled_output_sha256": fingerprint(result) if result is not None else None,
                "authorized_paths": sorted(plan["targets"]),
                "changed_paths": [p["path"] for p in patch.get("patches", [])]
                if not patch_errors
                else [],
            }
            self._save(
                len(self.requests),
                "assembled-output",
                {"provenance": self.correction, "output": result},
            )
        else:
            result, raw, errors = self._attempt(correction, schema, validator)
            self.correction = {"mode": "single_full_correction", "stage": self.stage}
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
            "correction": self.correction,
            "input_checks": self.input_checks,
        }
