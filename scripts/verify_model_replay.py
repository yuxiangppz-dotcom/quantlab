"""Independent cash/share/fee/NAV reconciliation; does not call replay arithmetic."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd


def nearest(value):
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def tax_rate(acquired, day):
    stamp = pd.Timestamp(acquired)
    if pd.Timestamp(day) <= stamp + pd.DateOffset(months=1):
        return Decimal(".2")
    if pd.Timestamp(day) <= stamp + pd.DateOffset(years=1):
        return Decimal(".1")
    return Decimal(0)


def metrics(values):
    returns = [b / a - 1 for a, b in zip(values[:-1], values[1:], strict=True)]
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
    peak, dd = values[0], 0
    for value in values:
        peak = max(peak, value)
        dd = min(dd, value / peak - 1)
    return {
        "total_return": values[-1] / values[0] - 1,
        "cagr_252": (values[-1] / values[0]) ** (252 / len(returns)) - 1,
        "max_drawdown": dd,
        "sharpe_rf0_252": mean / math.sqrt(variance) * math.sqrt(252),
    }


def verify(directory, canonical):
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed_scenario", "incomplete path cannot receive verification"
    sources = json.loads((directory / "source_manifest.json").read_text())
    for path, entry in sources.items():
        raw = Path(path).read_bytes()
        assert len(raw) == entry["bytes"] and hashlib.sha256(raw).hexdigest() == entry["sha256"], (
            path
        )
    ledger = pd.read_csv(directory / "ledger.csv")
    assert (
        hashlib.sha256((directory / "ledger.csv").read_bytes()).hexdigest()
        == result["ledger_sha256"]
    )
    assert len(ledger) == result["committed_days"]
    assert ledger.date.is_unique and ledger.date.is_monotonic_increasing
    previous_cash, previous_shares, prior_claims = 20000000, Counter(), {}
    last_observations, checked_trades = {}, 0
    for row in ledger.to_dict("records"):
        day = row["date"]
        snapshot = json.loads((directory / "days" / f"{day}.json").read_text())
        for key, value in row.items():
            if key != "gross_exposure":
                assert snapshot["record"][key] == value, (day, key)
        bar_path = canonical / "daily" / f"year={day[:4]}/month={day[5:7]}/{day}.parquet"
        bars = pd.read_parquet(bar_path).set_index("instrument_id").to_dict("index")
        share_changes = Counter()
        cash_flow = 0
        for attempt in snapshot["attempts"]:
            quantity = attempt["quantity"]
            if not quantity:
                assert (
                    attempt["notional_fen"]
                    == attempt["commission_fen"]
                    == attempt["stamp_fen"]
                    == 0
                )
                continue
            code, buy = attempt["instrument_id"], attempt["side"] == "buy"
            close = Decimal(str(bars[code]["close"])) * 100
            price = int(
                (close * (Decimal("1.0005") if buy else Decimal(".9995"))).to_integral_value(
                    rounding=ROUND_CEILING if buy else ROUND_FLOOR
                )
            )
            assert price == attempt["price_fen"]
            assert quantity * price == attempt["notional_fen"]
            commission = max(500, nearest(Decimal(quantity * price) * Decimal(".000086")))
            rate = Decimal(0) if buy else Decimal(".001" if day < "2023-08-28" else ".0005")
            stamp = nearest(Decimal(quantity * price) * rate)
            assert (commission, stamp) == (attempt["commission_fen"], attempt["stamp_fen"])
            cash_flow += (-1 if buy else 1) * quantity * price - commission - stamp
            share_changes[code] += (1 if buy else -1) * quantity
            checked_trades += 1
        for event in snapshot["corporate"]:
            if event["kind"] == "shares_listed":
                share_changes[event["event"].split(":")[0]] += event["quantity"]
        claims = {(c["event"]["event_id"], c["lot_id"]): c for c in snapshot["claims"]}
        assert len(claims) == len(snapshot["claims"])
        receivable, reserve = 0, 0
        for key, claim in claims.items():
            old = prior_claims.get(key, {})
            if claim["paid"] and not old.get("paid", False):
                cash_flow += claim["gross_fen"]
            cash_flow -= claim["withheld_fen"] - old.get("withheld_fen", 0)
            if claim["activated"]:
                assert claim["event"]["ex"] <= day
                receivable += 0 if claim["paid"] else claim["gross_fen"]
                reserve += (
                    nearest(
                        Decimal(claim["sold_tax"])
                        + Decimal(claim["remaining_tax_basis"]) * tax_rate(claim["acquired"], day)
                    )
                    - claim["withheld_fen"]
                )
            if claim["paid"] and claim["gross_fen"]:
                assert claim["event"]["pay"] <= day
        assert previous_cash + cash_flow == row["cash_fen"], (day, "cash conservation")
        current = Counter()
        for lot in snapshot["book"]["lots"]:
            current[lot["instrument_id"]] += lot["quantity"]
        for code in set(current) | set(previous_shares) | set(share_changes):
            assert previous_shares[code] + share_changes[code] == current[code], (
                day,
                code,
                "shares",
            )
        stale = {x["instrument_id"]: x for x in snapshot["stale_marks"]}
        position = 0
        for code, quantity in current.items():
            if code in stale:
                mark = stale[code]
                if mark["method"].startswith("delisted_zero_"):
                    price = 0
                else:
                    assert (mark["observed_date"], mark["price_fen"]) == last_observations[code]
                    price = mark["price_fen"]
            else:
                price = int(Decimal(str(bars[code]["close"])) * 100)
                last_observations[code] = (day, price)
            position += quantity * price
        assert position == row["position_fen"]
        assert (receivable, reserve) == (
            row["dividend_receivable_fen"],
            row["dividend_tax_reserve_fen"],
        )
        assert row["equity_fen"] == row["cash_fen"] + position + receivable - reserve
        previous_cash, previous_shares, prior_claims = row["cash_fen"], current, claims
    computed = metrics(ledger.equity_fen.to_list())
    for key, value in computed.items():
        assert math.isclose(value, result["metrics"][key], rel_tol=1e-11, abs_tol=1e-12), key
    return {
        "all_ok": True,
        "days": len(ledger),
        "executed_attempts_checked": checked_trades,
        "source_files_checked": len(sources),
        "independent_metrics": computed,
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--canonical-dir", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.run_dir, args.canonical_dir)
    with (args.run_dir / "independent_verification.json").open("x") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result))
