"""Fixed first-entry proposals; no actual scheduler, fills or return calculation."""

from __future__ import annotations

import copy
import json
import math
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.execution.models import OrderSession
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.closing_quantity import load_catalogue
from quantlab.research.costs import load_research_cost_profile
from quantlab.research.quantity_kernel import ResearchFeeScenario, ResearchQuantityRules
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.stock_replay_inputs import canonical_bar, partition

BASE = "data/products/research_program/launch_20260912/"
CONFIG = "config/s4_first_entry_plan_v1.json"
OUTPUT = BASE + "s4_first_entry_plan"
DECISION, EXECUTION, FOLLOWING = "2021-12-31", "2022-01-04", "2022-01-05"
CAPITAL, SLOT, SLOTS, MIN_SIGNALS = 20_000_000, 800_000, 20, 30
SAVED = {
    "selection": BASE + "selection.json",
    "calendar": BASE + "funding_inputs.json",
    "observations": BASE + "stock_replay_inputs/observations.json",
    "parent_report": BASE + "stock_replay_inputs/report.json",
    "parent_proof": BASE + "stock_replay_inputs/independent_proof.json",
    "liquidity": BASE + "liquidity_amount_lower_bound/projection.json",
    "liquidity_report": BASE + "liquidity_amount_lower_bound/report.json",
    "liquidity_proof": BASE + "liquidity_amount_lower_bound/independent_proof.json",
}
PINS = {
    "selection": "04a36754511be4136685a3ce378d60aa8c746ca218da7fa66d62df23b2e5840e",
    "parent_report": "e8ea83c4503107b6a14de3e6b7ad30d7ad80fc4578c541d7bdfdf794b8df176d",
    "liquidity_report": "0bd006ccedc2d370646b97f8315fa749135dc13c7a98001f0bd7174268fc76fb",
}


def rank_proposals(rows):
    """Only code and copied decision signal can affect raw proposal membership."""
    codes = [r["instrument_id"] for r in rows]
    if not codes or codes != sorted(set(codes)):
        raise DataValidationError("proposal population must have unique ordered identifiers")
    finite = []
    for row in rows:
        value = row["copied_S4_A"]
        if value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            raise DataValidationError("saved score must be finite numeric or explicit unknown")
        finite.append((row["instrument_id"], value))
    if len(finite) < MIN_SIGNALS:
        return (), len(finite)
    return tuple(code for code, _ in sorted(finite, key=lambda x: (-x[1], x[0]))[:SLOTS]), len(
        finite
    )


def code_scope(code):
    """A code-prefix rule lookup scope, explicitly not historical identity evidence."""
    if not isinstance(code, str) or re.fullmatch(r"\d{6}\.(SH|SZ)", code) is None:
        return None
    if code.endswith(".SH"):
        if code.startswith("688"):
            return "SSE", "STAR"
        if code.startswith(("600", "601", "603", "605")):
            return "SSE", "MAIN"
    if code.endswith(".SZ"):
        if code.startswith(("300", "301")):
            return "SZSE", "CHINEXT"
        if code.startswith(("000", "001", "002", "003")):
            return "SZSE", "MAIN"
    return None


def quantity_rule(rule):
    if rule is None:
        return None
    return ResearchQuantityRules(
        rule.rule_id,
        rule.effective_from,
        rule.effective_to,
        rule.buy_min_quantity,
        rule.buy_quantity_step,
        rule.sell_min_quantity,
        rule.sell_quantity_step,
        rule.max_limit_quantity,
        rule.allow_full_odd_lot_exit,
    )


def nominal_quantity(price_fen, rule):
    if price_fen is None or rule is None:
        return None
    if type(price_fen) is not int or not 0 < price_fen <= 10**15:
        raise DataValidationError("sizing requires a positive integer-fen raw decision close")
    if type(rule) is not ResearchQuantityRules:
        raise DataValidationError("sizing requires explicit quantity rule")
    maximum = min(SLOT // price_fen, rule.max_order_quantity)
    if maximum < rule.buy_minimum:
        return 0
    return (
        rule.buy_minimum + (maximum - rule.buy_minimum) // rule.buy_increment * rule.buy_increment
    )


def fee_context(profile, name):
    if name not in ("baseline", "stress"):
        raise DataValidationError("only the two frozen fee contexts are supported")
    return asdict(
        ResearchFeeScenario(
            "s4-first-entry-" + name,
            date(2022, 1, 1),
            date(2023, 8, 27),
            Decimal(profile["commission_rate"]),
            profile["stock_minimum_commission_fen"],
            Decimal("0"),
            Decimal("0.001"),
            None,
            None,
            Decimal("0.0005" if name == "baseline" else "0.0015"),
        )
    )


def required_unknowns(context, quantity, decision_price):
    """Disclose all necessary unknowns without invoking the mutation scheduler."""
    if quantity == 0:
        return ()
    result = ["historical_identity_and_signal_eligibility_unverified"]
    if decision_price is None:
        result.append("decision_raw_price_unknown")
    for field in ("calendar_verified", "market_open", "corporate_actions_processed"):
        if context[field] is not True:
            result.append(field + ("_unknown" if context[field] is None else "_false"))
    for field in (
        "raw_close_fen",
        "low_fen",
        "high_fen",
        "down_limit_fen",
        "up_limit_fen",
        "prior20_amount_fen",
        "session_amount_fen",
        "session_volume_shares",
        "rules",
    ):
        if context[field] is None:
            result.append(field + "_unknown")
    if context["prior20_sessions"] != 20:
        result.append("prior20_coverage_incomplete")
    for field in ("additional_fee_rate", "additional_fee_fixed_fen"):
        if context["fees"][field] is None:
            result.append(field + "_unknown")
    return tuple(result)


def project(rows, decision_bars, liquidity, catalogue, profile):
    selected, finite = rank_proposals(rows)
    codes = [r["instrument_id"] for r in rows]
    if set(liquidity) != set(codes) or set(decision_bars) - set(codes):
        raise DataValidationError("liquidity/decision population changed")
    result = []
    for row in rows:
        code = row["instrument_id"]
        source = row["session"]
        if (
            row["signal_date"] != DECISION
            or source["instrument_id"] != code
            or source["execution_date"] != EXECUTION
            or source["next_session"] != FOLLOWING
            or source["evidence_date"] != EXECUTION
            or source["prior20_asof"] != DECISION
            or any(source[k] is not None for k in ("rules", "fees", "participation"))
        ):
            raise DataValidationError("source context or chronology changed")
        bound = liquidity[code]
        if bound["copied_exact_adv20_fen"] != source["prior20_amount_fen"]:
            raise DataValidationError("liquidity projection does not bind original exact value")
        scope = code_scope(code)
        resolved = (
            catalogue.resolve(
                *scope, date.fromisoformat(EXECUTION), session=OrderSession.CLOSING_AUCTION
            )
            if scope
            else None
        )
        rule = quantity_rule(resolved)
        bar = canonical_bar(decision_bars.get(code, {}))
        price = bar["close_fen"]
        chosen = code in selected
        quantity = nominal_quantity(price, rule) if chosen else None
        contexts, missing = {}, {}
        for scenario in ("baseline", "stress"):
            context = {
                **copy.deepcopy(source),
                "prior20_amount_fen": bound["adv20_floor_fen"],
                "prior20_sessions": 20 - len(bound["unknown_dates"]),
                "participation": Decimal("0.01"),
                "rules": None if rule is None else asdict(rule),
                "fees": fee_context(profile, scenario),
            }
            contexts[scenario] = context
            missing[scenario] = list(required_unknowns(context, quantity, price)) if chosen else []
        result.append(
            {
                "instrument_id": code,
                "copied_S4_A": row["copied_S4_A"],
                "selected_raw_proposal": chosen,
                "proposal_rank": selected.index(code) + 1 if chosen else None,
                "nominal_slot_fen": SLOT if chosen else 0,
                "decision_raw_close_fen": price,
                "nominal_quantity": quantity,
                "nominal_notional_fen": price * quantity
                if chosen and quantity is not None
                else None,
                "sizing_status": "not_selected"
                if not chosen
                else (
                    "unknown"
                    if quantity is None
                    else "below_minimum"
                    if quantity == 0
                    else "proposal"
                ),
                "rule_lookup_scope": scope,
                "historical_identity_certified": False,
                "liquidity_basis": "saved_numeric_prior20_floor_fen_lower_bound",
                "st_source_records": copy.deepcopy(row["st_source_records"]),
                "suspensions_source_records": copy.deepcopy(row["suspensions_source_records"]),
                "contexts": contexts,
                "required_unknowns": missing,
            }
        )
    unknown = Counter(reason for r in result for reason in r["required_unknowns"]["baseline"])
    return {
        "rows": result,
        "selected_in_priority_order": list(selected),
        "finite_signal_count": finite,
        "status": "not_started_necessary_facts_unknown" if selected else "not_started_signal_count",
        "initial_cash_fen": CAPITAL,
        "target_gross_fen": SLOT * len(selected),
        "cash_outside_nominal_slots_fen": CAPITAL - SLOT * len(selected),
        "necessary_unknown_counts": dict(sorted(unknown.items())),
        "economic_paths_started": 0,
        "completed_trading_days": 0,
        "net_return": None,
        "drawdown": None,
        "execution_authority": False,
        "performance_evidence": False,
    }


def load_inputs(root):
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    expected_fingerprint = "58beb026844d9ddcb7350402ea81a1b877d38b5bd4dc7d8caef17eb01f62735a"
    if (
        canonical_payload_fingerprint(config) != expected_fingerprint
        or config["schema"] != "s4_first_entry_plan_v1"
        or config["output"] != OUTPUT
        or config["saved"] != SAVED
        or config["pins"] != PINS
        or config["decision_price_path"]
        != "data/canonical/daily/year=2021/month=12/2021-12-31.parquet"
        or config["resources"]
        != {
            "max_wakeup_seconds": 300,
            "max_rss_bytes": 2 * 1024**3,
            "max_generated_bytes": 64 * 1024**2,
            "reserve_host_D_bytes": 8 * 1024**3,
        }
    ):
        raise DataValidationError("fixed first-entry input scope changed")
    required = set(SAVED.values()) | {
        config["decision_price_path"],
        "config/research_user_costs_v1.json",
    }
    if not required <= set(config["inputs"]):
        raise DataValidationError("missing first-entry source binding")
    verify_entries(root, config["inputs"])
    for name, entry in config["inputs"].items():
        if (root / name).stat().st_size != entry["bytes"]:
            raise DataValidationError("input byte size changed")
    saved = {k: sealed_read(root / p) for k, p in SAVED.items()}
    if any(saved[k]["fingerprint"] != pin for k, pin in PINS.items()):
        raise DataValidationError("original evidence pin changed")
    if (
        saved["observations"]["fingerprint"] != saved["parent_report"]["observations_fingerprint"]
        or saved["parent_proof"]["report_fingerprint"] != saved["parent_report"]["fingerprint"]
        or saved["liquidity"]["fingerprint"] != saved["liquidity_report"]["projection_fingerprint"]
        or saved["liquidity_proof"]["report_fingerprint"]
        != saved["liquidity_report"]["fingerprint"]
        or saved["liquidity_report"]["old_report_fingerprint"]
        != saved["parent_report"]["fingerprint"]
    ):
        raise DataValidationError("saved proof or projection chain changed")
    codes = saved["selection"]["instrument_ids"]
    if len(codes) != 256 or codes != sorted(set(codes)):
        raise DataValidationError("original256 population changed")
    for key in ("observations", "liquidity"):
        if [r["instrument_id"] for r in saved[key]["rows"]] != codes:
            raise DataValidationError("bound256 observation grid changed")
    days = saved["calendar"]["sessions"]
    index = days.index(DECISION)
    if days[index : index + 3] != [DECISION, EXECUTION, FOLLOWING]:
        raise DataValidationError("bound decision calendar changed")
    bars = partition(root / config["decision_price_path"], date.fromisoformat(DECISION))
    bars = bars[bars.instrument_id.isin(codes)]
    profile = load_research_cost_profile(root / "config/research_user_costs_v1.json")
    if profile["commission_rate"] != "0.000086" or profile["stock_minimum_commission_fen"] != 500:
        raise DataValidationError("declared user commission changed")
    return config, saved, bars.set_index("instrument_id").to_dict("index"), profile


def run(root):
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("first-entry source must be pushed")
    config, saved, bars, profile = load_inputs(root)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("first-entry projection attempt already consumed")
        budget = Budget(out, config["resources"])
        budget.check()
        intent = atomic_seal(
            out / "intent.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "inputs": config["inputs"],
                "economic_paths_started": 0,
                "scope": "one_first_day_intent_projection_only",
            },
        )
        try:
            with budget.watchdog():
                plan = project(
                    saved["observations"]["rows"],
                    bars,
                    {r["instrument_id"]: r for r in saved["liquidity"]["rows"]},
                    load_catalogue(root),
                    profile,
                )
                output = atomic_seal(out / "plan.json", plan)
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "plan_fingerprint": output["fingerprint"],
                        "population": len(plan["rows"]),
                        "selected": len(plan["selected_in_priority_order"]),
                        "positive_nominal_quantity": sum(
                            r["sizing_status"] == "proposal" for r in plan["rows"]
                        ),
                        "necessary_unknown_counts": plan["necessary_unknown_counts"],
                        "provider_calls": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "execution_authority": False,
                        "performance_evidence": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes": budget.check(),
                        },
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {"error_type": type(exc).__name__, "intent_fingerprint": intent["fingerprint"]},
            )
            raise
