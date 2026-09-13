"""Independent verification of the S4 admission package (round 3).

Re-derives every admitted fact from the sealed sources and the local
canonical evidence with its own arithmetic and direct file reads; it does
not call the production admission functions. Checks are independent:
embedded fingerprints are recomputed over the file bodies, per-instrument
contexts are diffed against the sealed plan allowing only the approved
deltas, fields/open_gaps/statuses must agree with the actual context
values, and each prior20 classification is re-derived from the raw
suspension parquet, the daily partition and the response evidence.

Usage:
  uv run python scripts/s4_admission_independent_verify.py \
      --package-dir <dir> --source-dir <dir> --canonical-dir <dir>

Exit code 0 only when every check passes; the proof JSON lands in the
package directory and records the exact hashes it verified plus the
verifier code hash.
"""

import argparse
import hashlib
import json
import pathlib
from fractions import Fraction

import pandas as pd

FROZEN = {
    "plan": "6359d58599328340b43a84215ffa2f15725944171edea0b0f2786cf0cadfce92",
    "reconciliation": "a9e30b671ace89d59b272d5558295a1b1ee68f75e93c79309bd310d8951c3e10",
}

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--package-dir", required=True, type=pathlib.Path)
PARSER.add_argument("--source-dir", required=True, type=pathlib.Path)
PARSER.add_argument("--canonical-dir", required=True, type=pathlib.Path)


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_fingerprint(payload: dict) -> str:
    """Independent reimplementation of the repo's provider-payload hashing.

    The sealed convention hashes the whole payload (fingerprint key removed
    by the caller) with ensure_ascii=False, sort_keys=True and json's
    default separators.
    """
    body = {k: v for k, v in payload.items() if k != "fingerprint"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify(
    package_dir: pathlib.Path,
    source_dir: pathlib.Path,
    canonical_dir: pathlib.Path,
    frozen: dict | None = None,
    expected_gap_count: int | None = 6,
) -> dict:
    frozen = frozen or FROZEN
    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    package_path = package_dir / "input_package.json"
    report_path = package_dir / "preflight_report.json"
    completed_path = package_dir / "completed.json"
    package = json.loads(package_path.read_text())
    report = json.loads(report_path.read_text())
    completed = json.loads(completed_path.read_text())

    # 0. The proof binds exactly these files (no stale proof with new content).
    check(
        "completed_binds_current_files",
        completed.get("input_package_sha256") == sha_bytes(package_path.read_bytes())
        and completed.get("preflight_report_sha256")
        == sha_bytes(report_path.read_bytes()),
        "completed.json hashes match the package and report on disk",
    )

    # 1. Sealed sources: recompute the embedded fingerprints from the bytes.
    plan_path = source_dir / "s4_first_entry_plan/plan.json"
    recon_path = source_dir / "s4_entry_raw_precision/reconciliation.json"
    plan = json.loads(plan_path.read_text())
    recon = json.loads(recon_path.read_text())
    check(
        "sealed_fingerprints_recomputed",
        canonical_fingerprint(plan) == plan["fingerprint"] == frozen["plan"]
        and canonical_fingerprint(recon)
        == recon["fingerprint"]
        == frozen["reconciliation"],
        "recomputed over the file bodies and equal to the frozen identities",
    )

    # 2. Volumes re-derived with Fractions; per-instrument context diff allows
    # only the seven None->exact fills and the entry-day corporate flag.
    sealed_vols = {}
    for row in recon["rows"]:
        if row.get("purpose") != "execution_volume_precision":
            continue
        assert Fraction(row["raw_number_text"]["vol"]) * 100 == row["raw_normalized"]["vol"]
        sealed_vols[row["instrument_id"]] = row["raw_normalized"]["vol"]
    plan_rows = {row["instrument_id"]: row for row in plan["rows"]}
    selected = plan["selected_in_priority_order"]
    by_id = {item["instrument_id"]: item for item in package["instruments"]}
    diff_failures = []
    for code in selected:
        sealed_ctx = plan_rows[code]["contexts"]["baseline"]
        pkg_ctx = by_id[code]["context"]
        for key, sealed_value in sealed_ctx.items():
            pkg_value = pkg_ctx.get(key)
            if key == "session_volume_shares":
                expected = sealed_vols.get(code, sealed_value)
                if pkg_value != expected:
                    diff_failures.append(f"{code}.{key}: {pkg_value!r} != {expected!r}")
            elif key == "corporate_actions_processed":
                if pkg_value is not True or sealed_value not in (None, True):
                    diff_failures.append(f"{code}.{key}: {pkg_value!r}")
            elif pkg_value != sealed_value:
                diff_failures.append(f"{code}.{key}: {pkg_value!r} != {sealed_value!r}")
        for key in pkg_ctx:
            if key not in sealed_ctx:
                diff_failures.append(f"{code}.{key}: field not in the sealed context")
    check(
        "per_instrument_context_diff_only_approved_deltas",
        not diff_failures and len(by_id) == 20,
        "; ".join(diff_failures[:6]) or "20/20 contexts differ only in approved slots",
    )

    # 3. Ranking, sizes, capital.
    rank_ok = all(
        by_id[code]["proposal_rank"] == plan_rows[code]["proposal_rank"]
        and by_id[code]["nominal_quantity"] == plan_rows[code]["nominal_quantity"]
        and by_id[code]["nominal_slot_fen"] == plan_rows[code]["nominal_slot_fen"]
        for code in selected
    )
    check(
        "ranking_sizes_capital",
        rank_ok and package["initial_cash_fen"] == 20_000_000,
        "20/20 ranks, quantities, slots and the frozen capital",
    )

    # 4. Per-instrument fields/open_gaps/status consistency with real values.
    consistency_failures = []
    for code, item in by_id.items():
        fields = {f["field"]: f for f in item["fields"]}
        for gap in item["open_gaps"]:
            entry = fields.get(gap)
            if entry is None or entry["status"] != "unknown":
                consistency_failures.append(
                    f"{code}: open gap {gap} is not an unknown field"
                )
        for name, entry in fields.items():
            if entry["status"] == "unknown" and name not in item["open_gaps"]:
                consistency_failures.append(
                    f"{code}: unknown field {name} missing from open_gaps"
                )
        corporate = fields.get("corporate_actions_processed")
        if corporate is not None:
            value_ok = (
                item["context"]["corporate_actions_processed"] is True
                and corporate["status"] == "admitted"
            ) or (
                item["context"]["corporate_actions_processed"] is None
                and corporate["status"] == "unknown"
            )
            if not value_ok:
                consistency_failures.append(f"{code}: corporate flag/status disagree")
    check(
        "per_instrument_fields_gaps_values_consistent",
        not consistency_failures,
        "; ".join(consistency_failures[:6]) or "all 20 agree field-by-field",
    )

    # 5. prior20 classifications re-derived from raw evidence.
    gap_failures = []
    for gap in report["prior20_gaps"]:
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        year, month, _ = trade_date.split("-")
        stamp = trade_date.replace("-", "")
        susp_dir = (
            canonical_dir / f"lifecycle_context_v1/suspensions/year={year}/month={month}"
        )
        day = pd.DataFrame()
        if susp_dir.is_dir():
            frame = pd.concat(
                pd.read_parquet(p) for p in sorted(susp_dir.glob("*.parquet"))
            )
            day = frame[
                (frame["instrument_id"] == code)
                & (frame["trade_date"].astype(str).str[:10] == trade_date)
            ]
        timing = [
            None if pd.isna(v) or str(v).strip() in ("", "None") else str(v)
            for v in day.get("suspend_timing", [])
        ]
        full_day_local = (
            len(day) > 0
            and (day["suspend_type"].astype(str) == "S").all()
            and all(t is None for t in timing)
        )
        attempt = source_dir / f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        response_empty = False
        try:
            intent = json.loads((attempt / "intent.json").read_text())
            result = json.loads((attempt / "result.json").read_text())
            body = (attempt / "response.body").read_bytes()
            inner = intent["request"]["parameters"]["params"]
            payload = json.loads(body)
            response_empty = (
                result.get("intent_fingerprint") == intent.get("fingerprint")
                and inner.get("ts_code") == code
                and inner.get("start_date") == stamp
                and result.get("rows") == 0
                and result.get("status") == "empty"
                and payload.get("code") == 0
                and payload.get("data", {}).get("items") == []
            )
        except Exception:
            response_empty = False
        expected = (
            "proven_full_day_suspension_supplier_basis"
            if response_empty and full_day_local and gap.get("local_bar_present") is False
            else "other"
        )
        if gap["classification"] != expected or gap["suspension_on_date"] is not True:
            gap_failures.append(f"{code} {trade_date}: {gap['classification']}")
    count_ok = expected_gap_count is None or len(report["prior20_gaps"]) == expected_gap_count
    check(
        "prior20_reclassified_from_raw_evidence",
        not gap_failures and count_ok,
        "; ".join(gap_failures)
        or f"{len(report['prior20_gaps'])} dates re-derive from intent/body/suspension files",
    )

    # 6. Budgets and signal date.
    check(
        "zero_budgets_and_signal_date",
        report["economic_paths_started"] == 0
        and report["provider_calls"] == 0
        and package["first_pending_signal_date"] == "2021-12-31",
        "zero paths and calls; the 2021-12-31 signal date preserved",
    )

    return {
        "all_ok": all(v["ok"] for v in checks.values()),
        "package_dir": str(package_dir),
        "verified_hashes": {
            "input_package": sha_bytes(package_path.read_bytes()),
            "preflight_report": sha_bytes(report_path.read_bytes()),
            "completed": sha_bytes(completed_path.read_bytes()),
            "plan": sha_bytes(plan_path.read_bytes()),
            "reconciliation": sha_bytes(recon_path.read_bytes()),
        },
        "verifier_code_sha256": sha_bytes(pathlib.Path(__file__).read_bytes()),
        "checks": checks,
    }


def main() -> int:
    args = PARSER.parse_args()
    payload = verify(args.package_dir, args.source_dir, args.canonical_dir)
    proof_path = args.package_dir / "independent_verification.json"
    if proof_path.exists():
        previous = json.loads(proof_path.read_text())
        if previous.get("verified_hashes", {}).get("input_package") not in (
            None,
            payload["verified_hashes"]["input_package"],
        ):
            # A stale proof must never vouch for different package content.
            proof_path.write_text(
                json.dumps(
                    {
                        "all_ok": False,
                        "error": "stale proof: this directory was verified with "
                        "different package content",
                    },
                    indent=2,
                )
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
    proof_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
