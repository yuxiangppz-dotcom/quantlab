"""Tests for the S4 first-replay local evidence admission."""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction

import pandas as pd
import pytest

from quantlab.research.s4_replay_admission import (
    EXECUTION_DATE,
    admit_exact_volumes,
    build_input_package,
    build_manifest,
    build_preflight_report,
    check_prior20_gaps,
    sha256_file,
)


def _volume_row(
    instrument: str,
    *,
    text_vol: str = "82381.74",
    shares: int = 8238174,
    trade_date: str = EXECUTION_DATE,
    status: str = "nonempty",
    purpose: str = "execution_volume_precision",
    amount_text: str = "220407.301",
    amount_fen: int = 22040730100,
    ohlc_ok: bool = True,
) -> dict:
    return {
        "instrument_id": instrument,
        "trade_date": trade_date,
        "purpose": purpose,
        "source_status": status,
        "raw_number_text": {"vol": text_vol, "amount": amount_text},
        "raw_normalized": {"vol": shares, "amount": amount_fen},
        "raw_ohlc_consistent": ohlc_ok,
    }


def _plan_row(instrument: str, rank: int, *, volume: int | None, rule_increment: int) -> dict:
    return {
        "instrument_id": instrument,
        "proposal_rank": rank,
        "selected_raw_proposal": True,
        "nominal_quantity": 8000,
        "nominal_slot_fen": 800000,
        "contexts": {
            "baseline": {
                "execution_date": EXECUTION_DATE,
                "calendar_verified": True,
                "market_open": None,
                "corporate_actions_processed": None,
                "session_volume_shares": volume,
                "prior20_amount_fen": None,
                "rules": {"buy_increment": rule_increment, "scenario": "closing_limit"},
                "fees": {
                    "commission_rate": "0.000086",
                    "minimum_commission_fen": 500,
                    "additional_fee_rate": None,
                    "additional_fee_fixed_fen": None,
                },
            }
        },
    }


def _plan() -> dict:
    selected = ("000301.SZ", "002006.SZ")
    rows = [
        _plan_row("000301.SZ", 1, volume=None, rule_increment=100),
        _plan_row("002006.SZ", 2, volume=None, rule_increment=200),
    ]
    return {
        "selected_in_priority_order": list(selected),
        "rows": rows,
        "initial_cash_fen": 20_000_000,
        "target_gross_fen": 16_000_000,
    }


class TestVolumeAdmission:
    def test_seven_exact_volumes_admit_with_unit_mapping(self):
        rows = [
            _volume_row("002006.SZ", text_vol="82381.74", shares=8238174),
            _volume_row(
                "600961.SH", text_vol="76160.04", shares=7616004,
                amount_text="71633.836", amount_fen=7163383600,
            ),
        ]
        admitted = admit_exact_volumes(rows, ("002006.SZ", "600961.SH"))
        assert admitted == {"002006.SZ": 8238174, "600961.SH": 7616004}
        # Independent arithmetic: exact Fractions, no float.
        assert Fraction("82381.74") * 100 == 8238174
        assert Fraction("71633.836") * 1000 * 100 == 7163383600

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"trade_date": "2022-01-05"}, "dated"),
            ({"status": "empty"}, "not a nonempty source"),
        ],
    )
    def test_mismatched_rows_reject(self, kwargs, match):
        row = _volume_row("002006.SZ", **kwargs)
        with pytest.raises(ValueError, match=match):
            admit_exact_volumes([row], ("002006.SZ",))

    def test_unit_mapping_mismatch_rejects(self):
        with pytest.raises(ValueError, match="volume unit mapping"):
            admit_exact_volumes(
                [_volume_row("002006.SZ", text_vol="82381.74", shares=8238175)],
                ("002006.SZ",),
            )

    def test_amount_mapping_mismatch_rejects(self):
        with pytest.raises(ValueError, match="amount unit mapping"):
            admit_exact_volumes(
                [_volume_row("002006.SZ", amount_text="220407.301", amount_fen=1)],
                ("002006.SZ",),
            )

    def test_ohlc_inconsistency_rejects(self):
        with pytest.raises(ValueError, match="OHLC"):
            admit_exact_volumes(
                [_volume_row("002006.SZ", ohlc_ok=False)], ("002006.SZ",)
            )

    def test_non_proposal_rejects(self):
        with pytest.raises(ValueError, match="non-proposal"):
            admit_exact_volumes([_volume_row("999999.SZ")], ("002006.SZ",))

    def test_empty_admission_rejects(self):
        with pytest.raises(ValueError, match="no execution-volume rows"):
            admit_exact_volumes([], ("002006.SZ",))


class TestPackageBuild:
    def test_volumes_map_without_touching_ranking_or_nominal_sizes(self):
        plan = _plan()
        package, instruments = build_input_package(plan, {"002006.SZ": 8238174})
        assert package["first_pending_signal_date"] == "2021-12-31"
        by_id = {item.instrument_id: item for item in instruments}
        # The admitted volume lands only in the execution context.
        assert by_id["002006.SZ"].context["session_volume_shares"] == 8238174
        assert by_id["000301.SZ"].context["session_volume_shares"] is None
        # Ranking, slots and nominal quantities come from the sealed plan and
        # are untouched by execution-day data.
        assert [i.proposal_rank for i in instruments] == [1, 2]
        assert all(i.nominal_quantity == 8000 for i in instruments)
        other = build_input_package(plan, {})[1]
        assert [i.nominal_quantity for i in other] == [8000, 8000]
        assert [i.proposal_rank for i in other] == [1, 2]
        # Per-instrument rules survive untouched.
        assert by_id["002006.SZ"].context["rules"]["buy_increment"] == 200
        assert by_id["000301.SZ"].context["rules"]["buy_increment"] == 100

    def test_unknown_stays_unknown_and_no_silent_upgrade(self):
        plan = _plan()
        _, instruments = build_input_package(plan, {})
        for item in instruments:
            statuses = {f.field: f.status for f in item.fields}
            assert statuses["market_open"] == "unknown"
            assert statuses["historical_identity_and_signal_eligibility"] == "unknown"
            assert statuses["additional_fees"] == "unknown"
            assert statuses["prior20_amount_fen"] == "unknown"
            assert statuses["corporate_actions_processed"] == "admitted"
        package, _ = build_input_package(plan, {"002006.SZ": 8238174})
        baseline = package["instruments"][1]["context"]
        # Only the volume changed; the unknown fee fields stay None.
        assert baseline["fees"]["additional_fee_rate"] is None
        assert baseline["fees"]["additional_fee_fixed_fen"] is None

    def test_sealed_exact_integer_volume_is_admitted_as_sealed(self):
        plan = _plan()
        plan["rows"][0]["contexts"]["baseline"]["session_volume_shares"] = 37562365
        _, instruments = build_input_package(plan, {})
        field = {
            f.field: f for f in instruments[0].fields
        }["session_volume_shares"]
        assert field.status == "admitted"
        assert "plan.json context" in field.source
        # The value itself is untouched.
        assert instruments[0].context["session_volume_shares"] == 37562365

    def test_entry_day_corporate_admission_is_scoped_and_justified(self):
        _, instruments = build_input_package(_plan(), {})
        field = {
            f.field: f for f in instruments[0].fields
        }["corporate_actions_processed"]
        assert field.status == "admitted"
        assert "initially flat" in field.reason
        assert "never future holding periods" in field.reason

    def test_preflight_report_keeps_zero_budgets_and_stop_date(self):
        plan = _plan()
        package, instruments = build_input_package(plan, {"002006.SZ": 8238174})
        report = build_preflight_report(package, instruments, (), {"files": {}})
        assert report["verdict"] == "preflight_stopped_before_execution_day"
        assert report["precise_stop_date"] == EXECUTION_DATE
        assert report["economic_paths_started"] == 0
        assert report["provider_calls"] == 0
        gaps = report["gap_counts_by_field"]
        assert gaps["additional_fees"]["instruments"] == 2
        assert gaps["prior20_amount_fen"]["instruments"] == 2
        # Every instrument reports all of its gaps, not just the first one.
        assert gaps["session_volume_shares"]["instruments"] == 1
        assert gaps["session_volume_shares"]["examples"] == ["000301.SZ"]


class TestNoSideEffects:
    def test_manifest_hashes_and_preflight_writes_only_output(self, tmp_path):
        source = tmp_path / "sealed"
        (source / "s4_first_entry_plan").mkdir(parents=True)
        (source / "s4_entry_raw_precision").mkdir()
        (source / "cohort_dividend_readiness").mkdir()
        plan = _plan()
        plan["fingerprint"] = "abc123"
        plan_path = source / "s4_first_entry_plan/plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        recon = {"fingerprint": "def456", "rows": []}
        recon_path = source / "s4_entry_raw_precision/reconciliation.json"
        recon_path.write_text(json.dumps(recon), encoding="utf-8")
        profiles_path = source / "cohort_dividend_readiness/profiles.json"
        profiles_path.write_text(json.dumps({"fingerprint": "ghi789"}), encoding="utf-8")
        before = {p: sha256_file(p) for p in (plan_path, recon_path, profiles_path)}

        manifest = build_manifest(source)
        assert manifest["files"]["plan"]["embedded_fingerprint"] == "abc123"
        assert (
            manifest["files"]["plan"]["sha256"]
            == hashlib.sha256(plan_path.read_bytes()).hexdigest()
        )
        # Sealed files byte-identical after all admission work.
        after = {p: sha256_file(p) for p in (plan_path, recon_path, profiles_path)}
        assert before == after

    def test_prior20_gaps_classified_from_local_evidence(self, tmp_path):
        source = tmp_path / "sealed"
        attempt = source / "s4_entry_raw_precision/attempts/daily_000301.SZ_20211222"
        attempt.mkdir(parents=True)
        (attempt / "response.json").write_text('{"items": []}', encoding="utf-8")
        canonical = tmp_path / "canonical"
        month = canonical / "daily/year=2021/month=12"
        month.mkdir(parents=True)
        pd.DataFrame(
            {"ts_code": ["000301.SZ"], "trade_date": ["2021-12-22"], "vol": [123]}
        ).to_parquet(month / "part.parquet")
        gaps = check_prior20_gaps(
            [("000301.SZ", "2021-12-22")], source, canonical
        )
        gap = gaps[0]
        assert gap.local_bar_present is True
        assert gap.suspension_record_present is None
        assert gap.classification == "local_bar_present_supplier_empty"
        assert gap.raw_attempt_sha256 is not None

    def test_prior20_absence_of_everything_stays_undetermined(self, tmp_path):
        source = tmp_path / "sealed"
        source.mkdir(parents=True)
        canonical = tmp_path / "canonical"
        (canonical / "daily/year=2021/month=12").mkdir(parents=True)
        pd.DataFrame(
            {"ts_code": ["999999.SZ"], "trade_date": ["2021-12-22"], "vol": [1]}
        ).to_parquet(canonical / "daily/year=2021/month=12/part.parquet")
        gaps = check_prior20_gaps(
            [("000777.SZ", "2021-12-07")], source, canonical
        )
        gap = gaps[0]
        assert gap.local_bar_present is False
        assert gap.classification == "undetermined"
        assert "stays unknown" in gap.note
