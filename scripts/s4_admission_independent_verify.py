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


def path_is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


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
    entries = package["instruments"]
    check(
        "exactly_20_unique_instruments_in_frozen_order",
        len(entries) == 20
        and len({item["instrument_id"] for item in entries}) == 20
        and [item["instrument_id"] for item in entries] == list(selected),
        f"{len(entries)} entries",
    )
    by_id = {item["instrument_id"]: item for item in entries}
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

    # 4b. Re-derive per-field status and the overall verdict from the actual
    # context values: labels agreeing with each other is not enough — they
    # must agree with the facts. Required reasons must be present.
    semantic_failures = []
    unknown_fields_by_instrument: dict[str, list[str]] = {}
    for code, item in by_id.items():
        context = item["context"]
        fields = {f["field"]: f for f in item["fields"]}
        expected_unknown: list[str] = []
        fees = context.get("fees") or {}
        if fees.get("additional_fee_rate") is None:
            expected_unknown.append("fees.additional_fee_rate")
        if fees.get("additional_fee_fixed_fen") is None:
            expected_unknown.append("fees.additional_fee_fixed_fen")
        if context.get("market_open") is None:
            expected_unknown.append("market_open")
        if context.get("prior20_amount_fen") is None:
            expected_unknown.append("prior20_amount_fen")
        volume = context.get("session_volume_shares")
        if volume is None:
            expected_unknown.append("session_volume_shares")
        # Historical identity is contractually required for every
        # instrument and can never be satisfied by a context value.
        expected_unknown.append("historical_identity_and_signal_eligibility")
        for name in expected_unknown:
            entry = fields.get(name)
            if entry is None:
                semantic_failures.append(
                    f"{code}: {name} is factually unknown but not reported"
                )
                continue
            if entry.get("status") != "unknown":
                semantic_failures.append(
                    f"{code}: {name} is factually unknown but not reported"
                )
            elif not str(entry.get("reason") or "").strip():
                semantic_failures.append(f"{code}: {name} unknown without reason")
        corporate_entry = fields.get("corporate_actions_processed")
        corporate_value = context.get("corporate_actions_processed")
        if corporate_entry is None or corporate_value is not True:
            semantic_failures.append(f"{code}: entry-day corporate flag must be true")
        elif corporate_entry.get("status") != "admitted" or not str(
            corporate_entry.get("reason") or ""
        ).strip():
            semantic_failures.append(
                f"{code}: corporate admission lacks a written justification"
            )
        for name, entry in fields.items():
            if entry.get("status") == "admitted" and not str(
                entry.get("reason") or ""
            ).strip():
                semantic_failures.append(f"{code}: admitted {name} without reason")
        unknown_fields_by_instrument[code] = [
            name
            for name, entry in fields.items()
            if entry.get("status") == "unknown"
        ]
    check(
        "per_field_status_matches_actual_values",
        not semantic_failures,
        "; ".join(semantic_failures[:6]) or "20/20 field statuses re-derive",
    )

    # 4c. The verdict must be re-derived from the per-instrument results.
    recomputed_gaps = sorted(
        {
            name
            for code, names in unknown_fields_by_instrument.items()
            for name in names
        }
    )
    # Per-instrument open_gaps must equal the re-derived unknown set exactly
    # (this also catches wrong-attribution: a gap moved to another code).
    attribution_failures = []
    for code, item in by_id.items():
        expected = sorted(unknown_fields_by_instrument.get(code, []))
        if sorted(item["open_gaps"]) != expected:
            attribution_failures.append(
                f"{code}: open_gaps {sorted(item['open_gaps'])} != {expected}"
            )
    check(
        "per_instrument_open_gaps_match_rederivation",
        not attribution_failures,
        "; ".join(attribution_failures[:4]) or "20/20 open_gaps re-derive exactly",
    )
    reported_counts = report.get("gap_counts_by_field", {})
    count_failures = []
    # Invert the per-instrument unknown sets into per-field instrument lists.
    unknown_by_field: dict[str, list[str]] = {}
    for code, names in unknown_fields_by_instrument.items():
        for name in names:
            unknown_by_field.setdefault(name, []).append(code)
    rederived_counts = {
        name: len(codes)
        for name, codes in unknown_by_field.items()
    }
    for name in sorted(set(rederived_counts) | set(reported_counts)):
        expected_count = rederived_counts.get(name, 0)
        reported_entry = reported_counts.get(name)
        reported_n = reported_entry.get("instruments") if reported_entry else 0
        if reported_n != expected_count:
            count_failures.append(
                f"{name}: reported {reported_n} vs re-derived {expected_count}"
            )
    check(
        "gap_summary_matches_rederivation",
        not count_failures,
        "; ".join(count_failures[:6]) or "gap summary matches exactly",
    )
    expected_verdict = (
        "inputs_ready_to_request_first_replay_run"
        if not recomputed_gaps
        else "preflight_stopped_before_execution_day"
    )
    check(
        "verdict_rederived_from_facts",
        report.get("verdict") == expected_verdict
        and (report.get("precise_stop_date") == "2022-01-04" if recomputed_gaps else True),
        f"independently re-derived verdict {expected_verdict}",
    )

    # 4d. The manifest must cover every source the frozen contract requires:
    # the three base artifacts, the raw attempt triples for every gap date,
    # and the daily/suspension partitions backing each coverage claim.
    required_sources = {
        "sealed:s4_first_entry_plan/plan.json",
        "sealed:s4_entry_raw_precision/reconciliation.json",
        "sealed:cohort_dividend_readiness/profiles.json",
    }
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        stamp = trade_date.replace("-", "")
        year, month, _ = trade_date.split("-")
        base = f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        required_sources.add(f"sealed:{base}/intent.json")
        required_sources.add(f"sealed:{base}/result.json")
        required_sources.add(f"sealed:{base}/response.body")
        required_sources.add(
            f"canonical:daily/year={year}/month={month}"
        )
        required_sources.add(
            f"canonical:lifecycle_context_v1/suspensions/year={year}/month={month}"
        )
    manifest_files = report.get("manifest", {}).get("files", {})
    missing_required = sorted(
        req
        for req in required_sources
        if not any(key.startswith(req) for key in manifest_files)
    )
    if missing_required:
        check(
            "manifest_covers_required_sources",
            False,
            "; ".join(missing_required[:4]) or "covered",
        )
    roots = report.get("manifest", {}).get("roots", {})
    roots_ok = roots.get("sealed") == str(source_dir.resolve()) and roots.get(
        "canonical"
    ) == str(canonical_dir.resolve())
    check(
        "manifest_roots_match_passed_directories",
        roots_ok,
        str(roots) if not roots_ok else "sealed/canonical roots match",
    )

    # 5. prior20 classifications re-derived from raw evidence, including the
    # daily coverage: a report claiming confirmed no-bar must be backed by a
    # daily partition that actually contains the target date.
    manifest_files = report.get("manifest", {}).get("files", {})
    manifest_ok = True
    manifest_notes = []
    declared_roots = report.get("manifest", {}).get("roots", {})
    for key, entry in manifest_files.items():
        if ":" not in key:
            manifest_ok = False
            manifest_notes.append(f"{key}: key is not root:relative")
            continue
        root_label, relative = key.split(":", 1)
        declared_root = declared_roots.get(root_label)
        if declared_root is None:
            manifest_ok = False
            manifest_notes.append(f"{key}: unknown root label")
            continue
        declared_path = pathlib.Path(entry.get("path", ""))
        expected_path = pathlib.Path(declared_root) / relative
        # The key must name exactly this file under exactly this root: a
        # relabelled or redirected path is rejected even if the hash matches.
        if declared_path != expected_path:
            manifest_ok = False
            manifest_notes.append(
                f"{key}: path {entry.get('path')!r} is not {str(expected_path)!r}"
            )
            continue
        if not path_is_under(declared_path, pathlib.Path(declared_root)):
            manifest_ok = False
            manifest_notes.append(f"{key}: path escapes its declared root")
            continue
        if not declared_path.is_file() or sha_bytes(
            declared_path.read_bytes()
        ) != entry.get("sha256"):
            manifest_ok = False
            manifest_notes.append(f"{key}: missing or changed")
    check(
        "manifest_files_recomputed",
        manifest_ok and bool(manifest_files),
        "; ".join(manifest_notes[:4]) or f"{len(manifest_files)} bound files verified",
    )
    daily_failures = []
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        year, month, _ = trade_date.split("-")
        month_dir = canonical_dir / f"daily/year={year}/month={month}"
        parts = sorted(month_dir.glob("*.parquet")) if month_dir.is_dir() else []
        if not parts:
            if gap.get("local_bar_present") is not None:
                daily_failures.append(
                    f"{code} {trade_date}: no daily partitions but a bar claim"
                )
            continue
        frame = pd.concat(pd.read_parquet(p) for p in parts)
        date_col = "trade_date" if "trade_date" in frame.columns else "session"
        date_rows = frame[frame[date_col].astype(str).str[:10] == trade_date]
        if date_rows.empty:
            if gap.get("local_bar_present") is not None:
                daily_failures.append(
                    f"{code} {trade_date}: target date absent locally but a "
                    "bar claim exists"
                )
            continue
        code_col = "ts_code" if "ts_code" in frame.columns else "instrument_id"
        actual_bar = bool(
            len(date_rows[date_rows[code_col] == code]) > 0
        )
        if actual_bar != gap.get("local_bar_present"):
            daily_failures.append(
                f"{code} {trade_date}: actual bar {actual_bar} vs report "
                f"{gap.get('local_bar_present')}"
            )
    check(
        "daily_coverage_independently_rederived",
        not daily_failures,
        "; ".join(daily_failures[:4]) or "bar claims match the local daily data",
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

    # 5b. The report must embed exactly this package.
    check(
        "report_embeds_the_package",
        report.get("package") == package,
        "report.package equals input_package.json",
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
    proof_path = args.package_dir / "independent_verification.json"
    if proof_path.exists():
        # An existing proof is never overwritten, not even by a failing run:
        # rerun verification in a fresh output directory instead.
        print(
            json.dumps(
                {
                    "all_ok": False,
                    "error": (
                        "refusing to overwrite an existing proof; use a fresh "
                        "output directory"
                    ),
                },
                indent=2,
            )
        )
        return 2
    payload = verify(args.package_dir, args.source_dir, args.canonical_dir)
    proof_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
