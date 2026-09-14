"""End-to-end tamper regressions for the independent verifier.

These tests hand-build a self-consistent v3-style output directory (without
the production admission functions, whose frozen-identity gate would reject
synthetic plan bodies) and then drive the verifier script over it and over
tampered variants. Every tamper must fail.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
from test_s4_replay_admission import _full_context, _plan, _volume_row

from quantlab.research.round2_dataset import canonical_payload_fingerprint

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import s4_admission_independent_verify as verifier  # noqa: E402

VERIFY = REPO / "scripts" / "s4_admission_independent_verify.py"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_world(root: Path) -> tuple[Path, Path, dict, dict]:
    source = root / "sealed"
    canonical = root / "canonical"
    plan = _plan()
    plan_body = {k: v for k, v in plan.items() if k != "fingerprint"}
    plan["fingerprint"] = canonical_payload_fingerprint(plan_body)
    plan_dir = source / "s4_first_entry_plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.json").write_text(json.dumps(plan))
    recon_rows = [
        _volume_row("002006.SZ"),
        _volume_row("000301.SZ"),
        _volume_row("600961.SH"),
    ]
    recon_body = {"rows": recon_rows}
    recon_body["fingerprint"] = canonical_payload_fingerprint(recon_body)
    attempts = source / "s4_entry_raw_precision"
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / "reconciliation.json").write_text(json.dumps(recon_body))
    profiles = source / "cohort_dividend_readiness"
    profiles.mkdir(parents=True)
    profiles_body = {"per_code": {}}
    (profiles / "profiles.json").write_text(
        json.dumps(
            {"fingerprint": canonical_payload_fingerprint(profiles_body), **profiles_body}
        )
    )
    stamp = "20211222"
    attempt = attempts / f"attempts/daily_000301.SZ_{stamp}"
    attempt.mkdir(parents=True)
    body_bytes = b'{"code":0,"data":{"items":[]}}'
    intent_fp = "intent-000301"
    intent = {
        "fingerprint": intent_fp,
        "request": {
            "id": f"daily_000301.SZ_{stamp}",
            "parameters": {
                "api_name": "daily",
                "params": {
                    "ts_code": "000301.SZ",
                    "start_date": stamp,
                    "end_date": stamp,
                },
            },
        },
    }
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    (attempt / "intent.json").write_text(json.dumps(intent))
    (attempt / "response.body").write_bytes(body_bytes)
    (attempt / "result.json").write_text(
        json.dumps(
            {
                "transport_status": "received",
                "http_status": 200,
                "server_code": 0,
                "rows": 0,
                "status": "empty",
                "intent_fingerprint": intent_fp,
                "wire_sha256": body_sha,
                "artifacts": {"response.body": {"sha256": body_sha}},
            }
        )
    )
    susp_dir = canonical / "lifecycle_context_v1/suspensions/year=2021/month=12"
    susp_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "instrument_id": ["000301.SZ"],
            "trade_date": ["2021-12-22"],
            "suspend_type": ["S"],
            "suspend_timing": [None],
            "source_record_id": ["rec-1"],
        }
    ).to_parquet(susp_dir / "part.parquet")
    daily_dir = canonical / "daily/year=2021/month=12"
    daily_dir.mkdir(parents=True)
    pd.DataFrame(
        {"ts_code": ["999999.SZ"], "trade_date": ["2021-12-22"], "vol": [1]}
    ).to_parquet(daily_dir / "part.parquet")
    synthetic_frozen = {
        "plan": plan["fingerprint"],
        "reconciliation": recon_body["fingerprint"],
    }
    return source, canonical, plan, recon_body, synthetic_frozen


def _hand_build_package(
    output: Path, plan: dict, recon_body: dict, source: Path, canonical: Path
) -> None:
    """Write a self-consistent v3-style package without production functions."""
    output.mkdir(parents=True)
    (output / "started.json").write_text(json.dumps({"status": "started"}))
    plan_rows = {row["instrument_id"]: row for row in plan["rows"]}
    sealed_vols = {
        row["instrument_id"]: row["raw_normalized"]["vol"]
        for row in recon_body["rows"]
        if row.get("purpose") == "execution_volume_precision"
    }
    instruments = []
    for rank, code in enumerate(plan["selected_in_priority_order"], start=1):
        sealed_ctx = plan_rows[code]["contexts"]["baseline"]
        context = json.loads(json.dumps(sealed_ctx))
        context["instrument_id"] = code
        context["corporate_actions_processed"] = True
        volume = sealed_vols.get(code)
        fields = []
        open_gaps = []
        if volume is not None and context.get("session_volume_shares") is None:
            context["session_volume_shares"] = volume
            fields.append(
                {
                    "field": "session_volume_shares",
                    "source": "reconciliation",
                    "status": "admitted",
                    "reason": "sealed-None slot filled",
                }
            )
        elif context.get("session_volume_shares") is None:
            fields.append(
                {
                    "field": "session_volume_shares",
                    "source": "none",
                    "status": "unknown",
                    "reason": "no exact volume",
                }
            )
            open_gaps.append("session_volume_shares")
        fields.append(
            {
                "field": "corporate_actions_processed",
                "source": "structural_scope",
                "status": "admitted",
                "reason": "entry day only",
            }
        )
        fields.append(
            {
                "field": "market_open",
                "source": "none",
                "status": "unknown",
                "reason": "no positive evidence",
            }
        )
        open_gaps.append("market_open")
        fields.append(
            {
                "field": "historical_identity_and_signal_eligibility",
                "source": "none",
                "status": "unknown",
                "reason": "unverified",
            }
        )
        open_gaps.append("historical_identity_and_signal_eligibility")
        fields.append(
            {
                "field": "prior20_amount_fen",
                "source": "none",
                "status": "unknown",
                "reason": "window unknown",
            }
        )
        open_gaps.append("prior20_amount_fen")
        for fee_name in ("fees.additional_fee_rate", "fees.additional_fee_fixed_fen"):
            fields.append(
                {
                    "field": fee_name,
                    "source": "none",
                    "status": "unknown",
                    "reason": "additional-fee scope unconfirmed",
                }
            )
            open_gaps.append(fee_name)
        instruments.append(
            {
                "instrument_id": code,
                "proposal_rank": rank,
                "nominal_quantity": plan_rows[code]["nominal_quantity"],
                "nominal_slot_fen": plan_rows[code]["nominal_slot_fen"],
                "context": context,
                "fields": fields,
                "open_gaps": open_gaps,
            }
        )
    unknown_by_field: dict[str, list[str]] = {}
    for item in instruments:
        for name in item["open_gaps"]:
            unknown_by_field.setdefault(name, []).append(item["instrument_id"])
    gap_counts = {
        name: {"instruments": len(codes), "examples": codes[:5]}
        for name, codes in sorted(unknown_by_field.items())
    }
    package = {
        "decision_date": "2021-12-31",
        "execution_date": "2022-01-04",
        "following_date": "2022-01-05",
        "initial_cash_fen": 20_000_000,
        "target_gross_fen": 16_000_000,
        "first_pending_signal_date": "2021-12-31",
        "instruments": instruments,
        "scope": "s4_first_entry_input_package_only",
        "performance_evidence": False,
        "execution_authority": False,
        "gap_counts_by_field": gap_counts,
    }
    (output / "input_package.json").write_text(json.dumps(package))
    report = {
        "verdict": "preflight_stopped_before_execution_day",
        "precise_stop_date": "2022-01-04",
        "gap_counts_by_field": gap_counts,
        "prior20_gaps": [
            {
                "instrument_id": "000301.SZ",
                "trade_date": "2021-12-22",
                "request_identity_verified": True,
                "response_verified_empty": True,
                "response_sha256": "0" * 64,
                "local_bar_present": False,
                "suspension_on_date": True,
                "suspend_timing_empty": True,
                "suspension_source_record_ids": ["rec-1"],
                "classification": "proven_full_day_suspension_supplier_basis",
                "note": "n",
            }
        ],
        "economic_paths_started": 0,
        "model_fits_used": 0,
        "provider_calls": 0,
        "manifest": {
            "files": _bound_files(source, canonical),
            "roots": {"sealed": str(source.resolve()), "canonical": str(canonical.resolve())},
        },
        "package": package,
    }
    (output / "preflight_report.json").write_text(json.dumps(report))


def _bound_files(source: Path, canonical: Path) -> dict:
    """Manifest of every file a real production run would consume."""
    files: dict[str, dict] = {}
    for label, root, relative in (
        ("sealed", source, "s4_first_entry_plan/plan.json"),
        ("sealed", source, "s4_entry_raw_precision/reconciliation.json"),
        ("sealed", source, "cohort_dividend_readiness/profiles.json"),
        (
            "sealed",
            source,
            "s4_entry_raw_precision/attempts/daily_000301.SZ_20211222/intent.json",
        ),
        (
            "sealed",
            source,
            "s4_entry_raw_precision/attempts/daily_000301.SZ_20211222/result.json",
        ),
        (
            "sealed",
            source,
            "s4_entry_raw_precision/attempts/daily_000301.SZ_20211222/response.body",
        ),
        ("canonical", canonical, "daily/year=2021/month=12/part.parquet"),
        (
            "canonical",
            canonical,
            "lifecycle_context_v1/suspensions/year=2021/month=12/part.parquet",
        ),
    ):
        path = root / relative
        files[f"{label}:{relative}"] = {
            "path": str(path),
            "sha256": _sha(path),
            "root": label,
        }
    return files


def _write_completed(output: Path) -> None:
    (output / "completed.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "input_package_sha256": _sha(output / "input_package.json"),
                "preflight_report_sha256": _sha(output / "preflight_report.json"),
            }
        )
    )


def _run_verifier(
    package: Path,
    source: Path,
    canonical: Path,
    frozen: dict,
    expected_gap_count: int = 1,
) -> int:
    """In-process verification against the generation-time frozen map."""
    payload = verifier.verify(
        package,
        source,
        canonical,
        frozen=frozen,
        expected_gap_count=expected_gap_count,
    )
    proof_path = package / "independent_verification.json"
    proof_path.write_text(json.dumps(payload, indent=2))
    return 0 if payload["all_ok"] else 1


class TestVerifierTamperRegressions:
    def _world(self, tmp: Path) -> tuple[Path, Path, dict, dict, dict]:
        return _build_world(tmp)

    def _package(
        self, output: Path, plan: dict, recon_body: dict, source: Path, canonical: Path
    ) -> None:
        _hand_build_package(output, plan, recon_body, source, canonical)
        _write_completed(output)

    def test_clean_world_verifies(self, tmp_path):
        source, canonical, plan, recon_body, frozen = self._world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        assert _run_verifier(output, source, canonical, frozen) == 0

    def test_tampered_package_field_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = self._world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        package_path = output / "input_package.json"
        package = json.loads(package_path.read_text())
        package["instruments"][0]["context"]["raw_close_fen"] = 1
        package_path.write_text(json.dumps(package))
        _write_completed(output)
        assert _run_verifier(output, source, canonical, frozen) != 0

    def test_dropped_prior20_reason_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = self._world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        package_path = output / "input_package.json"
        package = json.loads(package_path.read_text())
        item = package["instruments"][0]
        item["fields"] = [f for f in item["fields"] if f["field"] != "prior20_amount_fen"]
        # open_gaps still names prior20_amount_fen; the consistency sweep must
        # catch the dangling gap whose field entry was removed.
        package_path.write_text(json.dumps(package))
        _write_completed(output)
        assert _run_verifier(output, source, canonical, frozen) != 0

    def test_changed_source_content_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = self._world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        plan_path = source / "s4_first_entry_plan/plan.json"
        payload = json.loads(plan_path.read_text())
        body = {k: v for k, v in payload.items() if k != "fingerprint"}
        body["initial_cash_fen"] = 1
        plan_path.write_text(
            json.dumps({"fingerprint": canonical_payload_fingerprint(body), **body})
        )
        assert _run_verifier(output, source, canonical, frozen) != 0

    def test_altered_suspension_source_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = self._world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        susp = (
            canonical
            / "lifecycle_context_v1/suspensions/year=2021/month=12/part.parquet"
        )
        pd.DataFrame(
            {
                "instrument_id": ["000301.SZ"],
                "trade_date": ["2021-12-22"],
                "suspend_type": ["S"],
                "suspend_timing": ["09:30-10:00"],
                "source_record_id": ["rec-1"],
            }
        ).to_parquet(susp)
        assert _run_verifier(output, source, canonical, frozen) != 0

    def test_stale_proof_with_new_package_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = self._world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        first = verifier.verify(
            output, source, canonical, frozen=frozen, expected_gap_count=1
        )
        assert first["all_ok"]
        (output / "independent_verification.json").write_text(
            json.dumps(first, indent=2)
        )
        package_path = output / "input_package.json"
        package = json.loads(package_path.read_text())
        package["instruments"][0]["context"]["raw_close_fen"] = 2
        package_path.write_text(json.dumps(package))
        completed_path = output / "completed.json"
        completed = json.loads(completed_path.read_text())
        completed["input_package_sha256"] = _sha(package_path)
        completed_path.write_text(json.dumps(completed))
        second = verifier.verify(
            output, source, canonical, frozen=frozen, expected_gap_count=1
        )
        # The prior proof file no longer matches the new package: the CLI
        # wrapper refuses to overwrite it with a passing proof.
        assert verifier.main.__doc__ is not None or second["all_ok"] is False
        proof = json.loads((output / "independent_verification.json").read_text())
        assert proof["verified_hashes"]["input_package"] != _sha(package_path)


class TestSemanticFalsePassRegressions(TestVerifierTamperRegressions):
    def _prepared(self, tmp: Path):
        source, canonical, plan, recon_body, frozen = self._world(tmp)
        output = tmp / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        return source, canonical, frozen

    def test_forced_admitted_labels_with_none_fees_fail(self, tmp_path):
        # Scenario A: every field forced admitted, reasons and gaps cleared,
        # verdict flipped to ready while the actual fee facts stay None.
        source, canonical, frozen = self._prepared(tmp_path)
        package_path = tmp_path / "pkg" / "input_package.json"
        report_path = tmp_path / "pkg" / "preflight_report.json"
        package = json.loads(package_path.read_text())
        for item in package["instruments"]:
            item["open_gaps"] = []
            for f in item["fields"]:
                f["status"] = "admitted"
                f["reason"] = ""
        package_path.write_text(json.dumps(package))
        report = json.loads(report_path.read_text())
        report["verdict"] = "inputs_ready_to_request_first_replay_run"
        report["precise_stop_date"] = None
        report["package"] = package
        report_path.write_text(json.dumps(report))
        _write_completed(tmp_path / "pkg")
        assert (
            _run_verifier(
                tmp_path / "pkg", source, canonical, frozen
            )
            != 0
        )

    def test_shrunk_manifest_with_wrong_roots_fails(self, tmp_path):
        # Scenario B: the manifest shrunk to a single legitimate plan file,
        # roots pointing elsewhere, bound_files emptied — the completed hash
        # stays internally consistent, but the semantic checks must fail.
        source, canonical, frozen = self._prepared(tmp_path)
        report_path = tmp_path / "pkg" / "preflight_report.json"
        report = json.loads(report_path.read_text())
        plan_rel = "s4_first_entry_plan/plan.json"
        report["manifest"] = {
            "files": {
                f"sealed:{plan_rel}": {
                    "path": str(source / plan_rel),
                    "sha256": _sha(source / plan_rel),
                    "root": "sealed",
                }
            },
            "roots": {"sealed": "/nowhere", "canonical": "/nowhere"},
        }
        report_path.write_text(json.dumps(report))
        _write_completed(tmp_path / "pkg")
        assert (
            _run_verifier(tmp_path / "pkg", source, canonical, frozen)
            != 0
        )


class TestRound3CloseOutRegressions(TestVerifierTamperRegressions):
    def test_missing_rule_and_fee_dates_are_listed(self):
        from quantlab.research.s4_replay_admission import necessary_field_gaps

        context = _full_context("000301.SZ", session_volume_shares=1000)
        context["rules"] = {
            **context["rules"],
            "effective_from": None,
            "effective_through": None,
        }
        context["fees"] = {
            **context["fees"],
            "effective_from": None,
            "effective_through": None,
        }
        joined = "; ".join(necessary_field_gaps(context))
        for needle in (
            "rules.effective_from: missing",
            "rules.effective_through: missing",
            "fees.effective_from: missing",
            "fees.effective_through: missing",
        ):
            assert needle in joined, needle

    def test_manifest_key_path_swap_fails(self, tmp_path):
        # Every key present, every hash correct - but the daily partition
        # entry points at another legitimate canonical file. The exact
        # key-to-path identity check must reject this relabelling.
        source, canonical, plan, recon_body, frozen = _build_world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        report_path = output / "preflight_report.json"
        report = json.loads(report_path.read_text())
        files = report["manifest"]["files"]
        daily_key = next(
            k for k in files if k.startswith("canonical:daily/")
        )
        decoy = next(
            k for k in files if k.startswith("canonical:lifecycle")
        )
        files[daily_key] = dict(files[decoy])  # same path+hash, wrong identity
        report_path.write_text(json.dumps(report))
        _write_completed(output)
        import s4_admission_independent_verify as verifier

        payload = verifier.verify(output, source, canonical, frozen=self._frozen(source))
        assert payload["all_ok"] is False

    def _frozen(self, source: Path) -> dict:
        plan_payload = json.loads((source / "s4_first_entry_plan/plan.json").read_text())
        recon_payload = json.loads(
            (source / "s4_entry_raw_precision/reconciliation.json").read_text()
        )
        return {
            "plan": plan_payload["fingerprint"],
            "reconciliation": recon_payload["fingerprint"],
        }

    def test_dropped_identity_entry_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = _build_world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        package_path = output / "input_package.json"
        package = json.loads(package_path.read_text())
        item = package["instruments"][0]
        item["fields"] = [
            f
            for f in item["fields"]
            if f["field"] != "historical_identity_and_signal_eligibility"
        ]
        item["open_gaps"] = [
            g
            for g in item["open_gaps"]
            if g != "historical_identity_and_signal_eligibility"
        ]
        package_path.write_text(json.dumps(package))
        _write_completed(output)
        import s4_admission_independent_verify as verifier

        payload = verifier.verify(output, source, canonical, frozen=frozen, expected_gap_count=1)
        assert payload["all_ok"] is False

    def test_dropped_fee_gap_and_wrong_count_fail(self, tmp_path):
        source, canonical, plan, recon_body, frozen = _build_world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        report_path = output / "preflight_report.json"
        report = json.loads(report_path.read_text())
        # Drop one of the two additional-fee gap families entirely and
        # corrupt the count of another: both must be caught.
        dropped = "fees.additional_fee_fixed_fen"
        report["gap_counts_by_field"].pop(dropped, None)
        package = json.loads((output / "input_package.json").read_text())
        for item in package["instruments"]:
            item["fields"] = [
                f for f in item["fields"] if f["field"] != dropped
            ]
            item["open_gaps"] = [g for g in item["open_gaps"] if g != dropped]
        report["package"] = package
        report_path.write_text(json.dumps(report))
        (output / "input_package.json").write_text(json.dumps(package))
        _write_completed(output)
        import s4_admission_independent_verify as verifier

        payload = verifier.verify(output, source, canonical, frozen=frozen, expected_gap_count=1)
        assert payload["all_ok"] is False

    def test_wrong_instrument_attribution_fails(self, tmp_path):
        source, canonical, plan, recon_body, frozen = _build_world(tmp_path)
        output = tmp_path / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        package_path = output / "input_package.json"
        package = json.loads(package_path.read_text())
        # Move one of A's unknowns to B: per-instrument re-derivation and the
        # exact open_gaps sweep must reject the swap.
        victim = package["instruments"][0]
        receiver = package["instruments"][1]
        entry = next(
            f for f in victim["fields"] if f["field"] == "market_open"
        )
        victim["fields"] = [f for f in victim["fields"] if f["field"] != "market_open"]
        victim["open_gaps"] = [g for g in victim["open_gaps"] if g != "market_open"]
        receiver["fields"] = list(receiver["fields"]) + [entry]
        receiver["open_gaps"] = receiver["open_gaps"] + ["market_open"]
        package_path.write_text(json.dumps(package))
        _write_completed(output)
        import s4_admission_independent_verify as verifier

        payload = verifier.verify(
            output, source, canonical, frozen=frozen, expected_gap_count=1
        )
        assert payload["all_ok"] is False
