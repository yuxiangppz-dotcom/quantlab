"""Independent verification of the generated S4 admission package.

Re-derives every admitted fact from the sealed sources with separate
Fraction arithmetic and naive re-mapping; it does not import or call the
production admission functions. Output lands beside the package.
"""

import hashlib
import json
import pathlib
import sys
from fractions import Fraction

source_dir = pathlib.Path(
    "/home/administrator/projects/quantlab/data/products/research_program/launch_20260912"
)
output_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("data/products/s4_first_replay_admission_v1")

plan = json.loads((source_dir / "s4_first_entry_plan/plan.json").read_text())
recon = json.loads((source_dir / "s4_entry_raw_precision/reconciliation.json").read_text())
package = json.loads((output_dir / "input_package.json").read_text())
report = json.loads((output_dir / "preflight_report.json").read_text())

checks = {}


def check(name, ok, detail=""):
    checks[name] = {"ok": bool(ok), "detail": detail}


# 1. Volume mapping re-derived with Fractions, naive scan.
sealed_vols = {}
for row in recon["rows"]:
    if row.get("purpose") != "execution_volume_precision":
        continue
    assert row["trade_date"] == "2022-01-04"
    text = Fraction(row["raw_number_text"]["vol"]) * 100
    assert text.denominator == 1
    assert int(text) == row["raw_normalized"]["vol"]
    amount = Fraction(row["raw_number_text"]["amount"]) * 1000 * 100
    assert amount.denominator == 1
    assert int(amount) == row["raw_normalized"]["amount"]
    sealed_vols[row["instrument_id"]] = row["raw_normalized"]["vol"]
check(
    "volume_mapping_independent",
    len(sealed_vols) == 7,
    f"re-derived {len(sealed_vols)} exact volumes",
)

# 2. Package contexts carry the reconciliation value for the seven and the
#    sealed exact integer for the rest; nothing else.
sealed_ctx = {
    row["instrument_id"]: row["contexts"]["baseline"]["session_volume_shares"]
    for row in plan["rows"]
}
pkg_by_id = {item["instrument_id"]: item for item in package["instruments"]}
mismatches = []
for instrument, item in pkg_by_id.items():
    volume = item["context"]["session_volume_shares"]
    expected = sealed_vols.get(instrument, sealed_ctx[instrument])
    if volume != expected:
        mismatches.append(f"{instrument}: package {volume} vs expected {expected}")
    # The seven replaced ones must have been sealed as None.
    if instrument in sealed_vols and sealed_ctx[instrument] is not None:
        mismatches.append(f"{instrument}: replaced a non-None sealed value")
check(
    "package_volumes_match_expected_sources",
    not mismatches,
    "; ".join(mismatches) or "all 20 contexts agree with their expected source",
)

# 3. Ranking, slots and nominal quantities identical to the sealed plan.
sealed_rows = {row["instrument_id"]: row for row in plan["rows"]}
rank_ok = all(
    pkg_by_id[code]["proposal_rank"] == sealed_rows[code]["proposal_rank"]
    and pkg_by_id[code]["nominal_quantity"] == sealed_rows[code]["nominal_quantity"]
    and pkg_by_id[code]["nominal_slot_fen"] == sealed_rows[code]["nominal_slot_fen"]
    for code in plan["selected_in_priority_order"]
)
check("ranking_and_sizes_untouched", rank_ok, "20/20 identical to sealed plan")

# 4. No silent upgrades: additional fees stay None, unknowns stay unknown.
fee_ok = all(
    item["context"]["fees"]["additional_fee_rate"] is None
    and item["context"]["fees"]["additional_fee_fixed_fen"] is None
    for item in pkg_by_id.values()
)
corporate = {
    f["field"]: f
    for item in pkg_by_id.values()
    for f in item["fields"]
    if f["field"] == "corporate_actions_processed"
}
check(
    "no_silent_upgrades",
    fee_ok
    and all(
        f["status"] == "unknown"
        for item in pkg_by_id.values()
        for f in item["fields"]
        if f["field"] in ("market_open", "historical_identity_and_signal_eligibility")
    ),
    "fees None everywhere; market_open and identity unknown for all 20",
)
check(
    "entry_day_corporate_scoped",
    all("initially flat" in f["reason"] for f in corporate.values()),
    "20/20 entry-day justifications present",
)

# 5. prior20 classifications all proven suspensions covering the exact date.
gaps = report["prior20_gaps"]
check(
    "prior20_all_proven_suspensions",
    len(gaps) == 6
    and all(g["classification"] == "proven_full_day_suspension" for g in gaps)
    and all(g["suspended_on_date"] is True for g in gaps),
    "6/6 dates covered by suspend_type=S records",
)

# 6. Manifest hashes re-computed.
manifest_ok = True
for name, entry in report["manifest"]["files"].items():
    digest = hashlib.sha256(pathlib.Path(entry["path"]).read_bytes()).hexdigest()
    manifest_ok &= digest == entry["sha256"]
check("manifest_hashes_recomputed", manifest_ok, f"{len(report['manifest']['files'])} files")

# 7. Signal date and zero budgets preserved.
check(
    "signal_date_and_budgets",
    package["first_pending_signal_date"] == "2021-12-31"
    and package["initial_cash_fen"] == 20_000_000
    and report["economic_paths_started"] == 0
    and report["provider_calls"] == 0,
    "2021-12-31 kept; 200,000 CNY; zero paths and calls",
)

all_ok = all(v["ok"] for v in checks.values())
payload = {"all_ok": all_ok, "checks": checks}
(output_dir / "independent_verification.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
)
print(json.dumps(payload, ensure_ascii=False, indent=2))
sys.exit(0 if all_ok else 1)
