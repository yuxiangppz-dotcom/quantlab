"""Independent scalar first-entry reconciliation; never invokes the plan writer."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path

from quantlab.execution.models import OrderSession
from quantlab.research.alpha158_store import atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.closing_quantity import load_catalogue
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.s4_entry_plan import OUTPUT, load_inputs


def prove(root):
    started = time.monotonic()
    binding = InputBinding(root)
    head = code_binding(root, binding)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        atomic_seal(
            out / "proof_intent.json",
            {"at": datetime.now(UTC).isoformat(), "source_head": head, "proof_attempt": 1},
        )
        config, saved, bars, _ = load_inputs(root)
        report = sealed_read(out / "report.json")
        plan = sealed_read(out / "plan.json")
        assert report["plan_fingerprint"] == plan["fingerprint"]
        rows = saved["observations"]["rows"]
        known = [r for r in rows if r["copied_S4_A"] is not None]
        ranks = {
            r["instrument_id"]: 1
            + sum(
                s["copied_S4_A"] > r["copied_S4_A"]
                or (
                    s["copied_S4_A"] == r["copied_S4_A"] and s["instrument_id"] < r["instrument_id"]
                )
                for s in known
            )
            for r in known
        }
        expected_order = [next(c for c, k in ranks.items() if k == i) for i in range(1, 21)]
        assert plan["selected_in_priority_order"] == expected_order
        assert plan["finite_signal_count"] == len(known)
        liquidity = {r["instrument_id"]: r for r in saved["liquidity"]["rows"]}
        catalogue = load_catalogue(root)
        counts = {}
        for original, result in zip(rows, plan["rows"], strict=True):
            code = original["instrument_id"]
            assert result["instrument_id"] == code
            assert result["copied_S4_A"] == original["copied_S4_A"]
            chosen = code in expected_order
            assert result["selected_raw_proposal"] is chosen
            assert result["proposal_rank"] == (ranks[code] if chosen else None)
            assert result["nominal_slot_fen"] == (800000 if chosen else 0)
            raw = bars.get(code, {})
            prices = []
            for name in ("low", "open", "close", "high"):
                value = raw.get(name)
                exact = Fraction(str(value)) * 100 if value is not None else None
                prices.append(
                    int(exact)
                    if exact is not None and exact > 0 and exact.denominator == 1
                    else None
                )
            low, opening, close, high = prices
            valid = (
                all(x is not None for x in prices)
                and low <= opening <= high
                and low <= close <= high
            )
            price = close if valid else None
            assert result["decision_raw_close_fen"] == price
            scope = result["rule_lookup_scope"]
            if code.startswith("688"):
                assert scope == ["SSE", "STAR"]
            elif code.endswith(".SH"):
                assert scope == ["SSE", "MAIN"]
            elif code.startswith(("300", "301")):
                assert scope == ["SZSE", "CHINEXT"]
            else:
                assert scope == ["SZSE", "MAIN"]
            rule = catalogue.resolve(*scope, date(2022, 1, 4), session=OrderSession.CLOSING_AUCTION)
            quantity = None
            if chosen and price is not None and rule is not None:
                quantity = min(800000 // price, rule.max_limit_quantity)
                while (
                    quantity >= rule.buy_min_quantity
                    and (quantity - rule.buy_min_quantity) % rule.buy_quantity_step
                ):
                    quantity -= 1
                if quantity < rule.buy_min_quantity:
                    quantity = 0
            assert result["nominal_quantity"] == quantity
            assert result["nominal_notional_fen"] == (
                quantity * price if quantity is not None else None
            )
            for field in ("st_source_records", "suspensions_source_records"):
                assert result[field] == original[field]
            for scenario, slippage in (("baseline", "0.0005"), ("stress", "0.0015")):
                context = result["contexts"][scenario]
                for field, value in original["session"].items():
                    if field not in {
                        "prior20_amount_fen",
                        "prior20_sessions",
                        "participation",
                        "rules",
                        "fees",
                    }:
                        assert context[field] == value
                assert context["prior20_amount_fen"] == liquidity[code]["adv20_floor_fen"]
                assert context["prior20_sessions"] == 20 - len(liquidity[code]["unknown_dates"])
                assert context["participation"] == "0.01"
                if rule:
                    expected_rule = dict(
                        scenario_id=rule.rule_id,
                        effective_from=str(rule.effective_from),
                        effective_through=str(rule.effective_to),
                        buy_minimum=rule.buy_min_quantity,
                        buy_increment=rule.buy_quantity_step,
                        sell_minimum=rule.sell_min_quantity,
                        sell_increment=rule.sell_quantity_step,
                        max_order_quantity=rule.max_limit_quantity,
                        full_position_odd_exit=rule.allow_full_odd_lot_exit,
                    )
                    assert context["rules"] == expected_rule
                fee = context["fees"]
                assert fee == dict(
                    scenario_id="s4-first-entry-" + scenario,
                    effective_from="2022-01-01",
                    effective_through="2023-08-27",
                    commission_rate="0.000086",
                    minimum_commission_fen=500,
                    buy_stamp_rate="0",
                    sell_stamp_rate="0.001",
                    additional_fee_rate=None,
                    additional_fee_fixed_fen=None,
                    adverse_slippage_rate=slippage,
                )
                reasons = []
                if chosen and quantity != 0:
                    reasons.append("historical_identity_and_signal_eligibility_unverified")
                    if price is None:
                        reasons.append("decision_raw_price_unknown")
                    for field in (
                        "calendar_verified",
                        "market_open",
                        "corporate_actions_processed",
                    ):
                        if context[field] is not True:
                            reasons.append(
                                field + ("_unknown" if context[field] is None else "_false")
                            )
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
                            reasons.append(field + "_unknown")
                    if context["prior20_sessions"] != 20:
                        reasons.append("prior20_coverage_incomplete")
                    reasons.extend(
                        ["additional_fee_rate_unknown", "additional_fee_fixed_fen_unknown"]
                    )
                assert result["required_unknowns"][scenario] == reasons
                if scenario == "baseline":
                    for reason in reasons:
                        counts[reason] = counts.get(reason, 0) + 1
        assert plan["necessary_unknown_counts"] == report["necessary_unknown_counts"] == counts
        assert plan["initial_cash_fen"] == 20000000
        assert plan["target_gross_fen"] == 16000000
        assert plan["cash_outside_nominal_slots_fen"] == 4000000
        assert plan["net_return"] is plan["drawdown"] is None
        assert plan["economic_paths_started"] == report["economic_paths"] == 0
        assert not plan["execution_authority"] and not plan["performance_evidence"]
        binding.check()
        verify_entries(root, config["inputs"])
        return atomic_seal(
            out / "proof.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "report_fingerprint": report["fingerprint"],
                "plan_fingerprint": plan["fingerprint"],
                "checked_rows": len(rows),
                "method": "pairwise scalar ranks, Fraction raw prices, decrement grid; same agent",
                "all_source_and_unknown_values_reconciled": True,
                "seconds": time.monotonic() - started,
                "peak_rss_bytes": peak_rss_bytes(),
                "execution_authority": False,
                "performance_evidence": False,
            },
        )


if __name__ == "__main__":
    print(prove(Path.cwd())["fingerprint"])
