"""Independent verification of the S4 admission package (round 2).

Re-derives every admitted fact from the sealed sources and the local
canonical evidence with separate arithmetic and direct file reads; it does
not import or call the production admission functions. Binds the exact
package/report hashes recorded in completed.json, checks per-instrument
contexts and statuses, and reads the raw suspension/response evidence
itself.

Usage:
  uv run python scripts/s4_admission_independent_verify.py \
      --package-dir data/products/s4_first_replay_admission_v2 \
      --source-dir <sealed launch dir> --canonical-dir <canonical dir>
"""

import argparse
import hashlib
import json
import pathlib
from fractions import Fraction

import pandas as pd

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--package-dir", required=True, type=pathlib.Path)
PARSER.add_argument("--source-dir", required=True, type=pathlib.Path)
PARSER.add_argument("--canonical-dir", required=True, type=pathlib.Path)
ARGS = PARSER.parse_args()

PACKAGE_DIR = ARGS.package_dir
SOURCE_DIR = ARGS.source_dir
CANONICAL_DIR = ARGS.canonical_dir


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


checks: dict[str, dict] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    checks[name] = {"ok": bool(ok), "detail": detail}


completed = json.loads((PACKAGE_DIR / "completed.json").read_text())
package_path = PACKAGE_DIR / "input_package.json"
report_path = PACKAGE_DIR / "preflight_report.json"
check(
    "completed_hashes_match_current_files",
    completed.get("status") == "completed"
    and completed.get("input_package_sha256") == sha(package_path)
    and completed.get("preflight_report_sha256") == sha(report_path),
    "the proof belongs to exactly these files",
)
package = json.loads(package_path.read_text())
report = json.loads(report_path.read_text())
plan = json.loads((SOURCE_DIR / "s4_first_entry_plan/plan.json").read_text())
recon = json.loads(
    (SOURCE_DIR / "s4_entry_raw_precision/reconciliation.json").read_text()
)
check(
    "embedded_source_fingerprints",
    plan["fingerprint"]
    == "6359d58599328340b43a84215ffa2f15725944171edea0b0f2786cf0cadfce92"
    and recon["fingerprint"]
    == "a9e30b671ace89d59b272d5558295a1b1ee68f75e93c79309bd310d8951c3e10",
    "frozen plan/reconciliation identities",
)

# Volumes: re-derive with Fractions from the sealed reconciliation rows.
sealed_vols = {}
for row in recon["rows"]:
    if row.get("purpose") != "execution_volume_precision":
        continue
    assert Fraction(row["raw_number_text"]["vol"]) * 100 == row["raw_normalized"]["vol"]
    assert (
        Fraction(row["raw_number_text"]["amount"]) * 1000 * 100
        == row["raw_normalized"]["amount"]
    )
    sealed_vols[row["instrument_id"]] = row["raw_normalized"]["vol"]
sealed_ctx = {
    row["instrument_id"]: row["contexts"]["baseline"]["session_volume_shares"]
    for row in plan["rows"]
}
by_id = {item["instrument_id"]: item for item in package["instruments"]}
volume_ok, volume_notes = True, []
for code, item in by_id.items():
    expected = sealed_vols.get(code, sealed_ctx[code])
    actual = item["context"]["session_volume_shares"]
    if actual != expected:
        volume_ok = False
        volume_notes.append(f"{code}: package {actual} vs expected {expected}")
    if code in sealed_vols and sealed_ctx[code] is not None:
        volume_ok = False
        volume_notes.append(f"{code}: replaced a non-None sealed value")
check(
    "per_instrument_volumes_match_expected_source",
    volume_ok and len(by_id) == 20,
    "; ".join(volume_notes) or "20/20 agree with reconciliation-or-sealed values",
)

# Ranking, slots, nominal quantities, capital, per-instrument identity/dates.
plan_rows = {row["instrument_id"]: row for row in plan["rows"]}
rank_ok = all(
    by_id[code]["proposal_rank"] == plan_rows[code]["proposal_rank"]
    and by_id[code]["nominal_quantity"] == plan_rows[code]["nominal_quantity"]
    and by_id[code]["nominal_slot_fen"] == plan_rows[code]["nominal_slot_fen"]
    and by_id[code]["context"]["instrument_id"] == code
    and by_id[code]["context"]["execution_date"] == "2022-01-04"
    for code in plan["selected_in_priority_order"]
)
check(
    "ranking_sizes_dates_per_instrument",
    rank_ok and package["initial_cash_fen"] == 20_000_000,
    "20/20 ranks, slots, quantities, securities and dates identical to the sealed plan",
)

# Corporate flag: the actual context boolean, per instrument — not the reason.
corporate_ok = all(
    item["context"]["corporate_actions_processed"] is True
    and any(
        f["field"] == "corporate_actions_processed" and f["status"] == "admitted"
        for f in item["fields"]
    )
    for item in by_id.values()
)
check(
    "corporate_context_values_admitted",
    corporate_ok,
    "20/20 derived contexts carry true and report it",
)

# Unknown facts stay unknown: additional fees None, market_open None.
unknown_ok = all(
    item["context"]["fees"]["additional_fee_rate"] is None
    and item["context"]["fees"]["additional_fee_fixed_fen"] is None
    and item["context"]["market_open"] is None
    for item in by_id.values()
)
check("unknowns_not_upgraded", unknown_ok, "fees None and market_open None for all 20")

# prior20: read the suspension parquet and the raw response evidence directly.
gap_ok = True
gap_notes = []
for gap in report["prior20_gaps"]:
    code, trade_date = gap["instrument_id"], gap["trade_date"]
    year, month, _ = trade_date.split("-")
    stamp = trade_date.replace("-", "")
    susp_dir = (
        CANONICAL_DIR / f"lifecycle_context_v1/suspensions/year={year}/month={month}"
    )
    frame = pd.concat(pd.read_parquet(p) for p in sorted(susp_dir.glob("*.parquet")))
    day = frame[
        (frame["instrument_id"] == code)
        & (frame["trade_date"].astype(str).str[:10] == trade_date)
    ]
    timing = [
        None if pd.isna(v) or str(v).strip() in ("", "None") else str(v)
        for v in day["suspend_timing"]
    ]
    direct_full_day = (
        len(day) > 0
        and (day["suspend_type"].astype(str) == "S").all()
        and all(t is None for t in timing)
    )
    attempt = (
        SOURCE_DIR
        / f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}/result.json"
    )
    result = json.loads(attempt.read_text())
    response_empty = (
        result.get("transport_status") == "received"
        and result.get("http_status") == 200
        and result.get("rows") == 0
        and result.get("status") == "empty"
    )
    expected_class = (
        "proven_full_day_suspension_supplier_basis"
        if direct_full_day and response_empty
        else "other"
    )
    if gap["classification"] != expected_class or gap["suspension_on_date"] is not True:
        gap_ok = False
        gap_notes.append(f"{code} {trade_date}: {gap['classification']}")
check(
    "prior20_independently_reclassified",
    gap_ok and len(report["prior20_gaps"]) == 6,
    "; ".join(gap_notes) or "6/6 dates re-derive to full-day supplier suspensions",
)

# Budgets and signal date.
check(
    "zero_budgets_and_signal_date",
    report["economic_paths_started"] == 0
    and report["provider_calls"] == 0
    and package["first_pending_signal_date"] == "2021-12-31",
    "zero paths and calls; the 2021-12-31 signal date is preserved",
)

all_ok = all(v["ok"] for v in checks.values())
payload = {"all_ok": all_ok, "package_dir": str(PACKAGE_DIR), "checks": checks}
(PACKAGE_DIR / "independent_verification.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
)
print(json.dumps(payload, ensure_ascii=False, indent=2))
raise SystemExit(0 if all_ok else 1)
