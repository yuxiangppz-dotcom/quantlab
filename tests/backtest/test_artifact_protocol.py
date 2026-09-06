"""Formal artifact commit protocol (v0.1.3): fail-closed completion marker.

Root cause reproduced from the v0.1.2 failure scene
(`20260906T135141.incomplete/COMPLETED.json` with ``formal_run_valid=true``
and ``verifier="inline_verify_pending_promotion"``): the publisher wrote the
success marker BEFORE running verification, so a process killed during
verification left a formal-looking marker inside a staging directory.

Protocol under test:

1. staging exports the full payload;
2. manifest written;
3. preflight verification on staging (completion marker NOT required — and
   any completion marker inside a staging directory is INVALID);
4. atomic promotion (rename to the final run id directory);
5. completion marker written atomically INSIDE the final directory;
6. formal verification on the final directory (full metadata binding).

Every interruption point must be fail-closed: the formal verifier returns
non-zero and no artifact claiming ``formal_run_valid=true`` is acceptable
unless every byte-level check re-derives it.
"""

import json
import shutil
from datetime import date

import pandas as pd
import pytest

from quantlab.backtest.artifacts import (
    COMPLETION_MARKER,
    INCOMPLETE_MARKER,
    ArtifactPublisher,
    sha256_file,
    verify_formal_artifact,
)
from quantlab.backtest.experiment import export_group
from quantlab.backtest.models import (
    BacktestConfig,
    BacktestResult,
    BookSnapshot,
    DailyBacktestRecord,
    PositionRecord,
    RebalanceRecord,
)

SCHEMA = "performance_baseline_benchmark_correctness_v0_1_3"
HEAD = "2b050c205089f995ffdd00c988e701c15389b112"
RUN_ID = "20260105T000000"

PRIMARY_GROUPS = (
    "primary_strategy_recovery_assumption_1",
    "primary_strategy_recovery_assumption_0",
    "equal_weight_v1_control_recovery_assumption_1",
    "equal_weight_v1_control_recovery_assumption_0",
)
ALL_GROUPS = PRIMARY_GROUPS + (
    "A_legacy_baseline",
    "A_legacy_baseline_diagnostic",
    "C_legacy_admission_v2",
    "C_legacy_admission_v2_diagnostic",
    "A_v1_baseline",
    "A_v1_baseline_diagnostic",
    "C_v1_admission_v2",
    "C_v1_admission_v2_diagnostic",
    "legacy_settlement_recovery_1",
    "legacy_settlement_recovery_0",
)
GROUP_FILE_FAMILY = (
    "daily_records.csv",
    "daily_books.csv",
    "daily_positions.csv",
    "rebalance_log.csv",
    "trade_details.csv",
    "lifecycle_events.csv",
    "risk_policy_audit.csv",
    "forced_exit_attempts.csv",
    "successful_forced_exits.csv",
    "pending_no_price.csv",
    "prevented_entry_refill.csv",
    "blocked_before_exit.csv",
    "failed_attempts.json",
)
TOP_LEVEL = [
    "summary.json",
    "manifest.json",
    "delisting_facts.json",
    "delisting_audit.csv",
    "shadow_admission.csv",
    "code_lineage_audit.json",
]


def _config() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def _result() -> BacktestResult:
    d = date(2026, 1, 5)
    positions = tuple(
        PositionRecord(
            instrument_id=f"I{j}", value=0.1, weight=0.1, last_price=1.0,
            last_mark_date=d, missing_price=False,
        )
        for j in range(3)
    )
    books = tuple(
        BookSnapshot(
            trade_date=d, book=book, nav=1.0, daily_return=0.0, cash=0.5,
            market_pnl=0.0, fee=0.0, gross_exposure=0.5, net_exposure=0.5,
            cash_weight=0.5, holdings_count=3, positions=positions,
        )
        for book in ("gross", "net")
    )
    zeros = {k: 0.0 for k in (
        "buy_notional_ratio", "sell_notional_ratio", "traded_notional_ratio",
        "turnover", "transaction_cost", "pre_trade_gross_exposure",
        "post_trade_gross_exposure", "allocation_deviation",
        "gross_book_buy_notional_ratio", "gross_book_sell_notional_ratio",
        "gross_book_traded_notional_ratio", "gross_book_turnover",
        "gross_book_transaction_cost", "gross_book_pre_trade_gross_exposure",
        "gross_book_post_trade_gross_exposure",
        "gross_book_allocation_deviation",
    )}
    return BacktestResult(
        run_mode="strict",
        status="completed",
        requested_period_start=d,
        requested_period_end=d,
        simulated_period_start=d,
        simulated_period_end=d,
        valid_through=d,
        diagnostic_from=None,
        first_blocking_event=None,
        records=[
            DailyBacktestRecord(
                trade_date=d, nav_gross=1.0, nav_net=1.0,
                daily_return_gross=0.0, daily_return_net=0.0,
                gross_exposure=0.5, net_exposure=0.5, cash_weight=0.5,
                turnover=0.0, traded_notional_ratio=0.0, transaction_cost=0.0,
                holdings_count=3, gross_book_gross_exposure=0.5,
                gross_book_net_exposure=0.5, gross_book_cash_weight=0.5,
                gross_book_turnover=0.0, gross_book_traded_notional_ratio=0.0,
                gross_book_holdings_count=3,
            )
        ],
        rebalances=[
            RebalanceRecord(
                signal_date=d, execution_date=d, target_count=3,
                nonzero_trade_count=0, unavailable_target_count=0, frozen_count=0,
                restricted_binding_count=0,
                gross_book_restricted_binding_count=0, **zeros,
            )
        ],
        books=list(books),
        trades=[],
        skipped_executions=[],
        lifecycle_events=[],
        failed_attempts=[],
        solver_root_residual=0.0,
        accounting_checks=[],
        accounting_error=None,
        accounting_error_date=None,
        accounting_error_book=None,
    )


def _registry():
    return {
        "groups": {g: list(GROUP_FILE_FAMILY) for g in PRIMARY_GROUPS},
        "top_level": list(TOP_LEVEL),
    }


def _full_registry():
    """The canonical full registry the production CLI enforces."""
    return {
        "groups": {g: list(GROUP_FILE_FAMILY) for g in ALL_GROUPS},
        "top_level": list(TOP_LEVEL),
    }


def _fill_payload(staging, groups=PRIMARY_GROUPS) -> None:
    for group in groups:
        export_group(staging, group, _result())
    (staging / "summary.json").write_text(json.dumps({
        "code_version": HEAD, "experiment_schema": SCHEMA, "run_id": RUN_ID,
    }))
    (staging / "manifest.json").write_text("{}")
    (staging / "delisting_facts.json").write_text("{}")
    (staging / "code_lineage_audit.json").write_text("[]")
    pd.DataFrame({"instrument_id": []}).to_csv(
        staging / "delisting_audit.csv", index=False
    )
    pd.DataFrame({"instrument_id": []}).to_csv(
        staging / "shadow_admission.csv", index=False
    )


def _publisher(tmp_path) -> ArtifactPublisher:
    return ArtifactPublisher(
        tmp_path, RUN_ID, expected_registry=_registry(), head=HEAD, schema=SCHEMA,
    )


def _formal_kwargs():
    return dict(
        expected_run_id=RUN_ID, expected_head=HEAD, expected_schema=SCHEMA,
        formal=True,
    )


def _preflight_kwargs():
    return dict(
        expected_run_id=RUN_ID, expected_head=HEAD, expected_schema=SCHEMA,
        formal=False,
    )


def _full_publish(tmp_path) -> ArtifactPublisher:
    """Staging with the complete payload AND the artifact manifest written —
    the on-disk state between manifest write and preflight verification."""
    staging = tmp_path / f"{RUN_ID}.incomplete"
    staging.mkdir()
    _fill_payload(staging)
    publisher = _publisher(tmp_path)
    manifest = publisher.build_artifact_manifest()
    (staging / "artifact_manifest.json").write_text(json.dumps(manifest))
    return publisher


# ------------------------------------------------- the 135141-shaped failure --


def test_incomplete_dir_with_success_marker_is_always_rejected(tmp_path) -> None:
    """Regression for 20260906T135141.incomplete: a staging directory that
    carries a completion marker claiming formal_run_valid=true must be
    rejected by the formal verifier — regardless of marker content."""
    staging = tmp_path / f"{RUN_ID}.incomplete"
    staging.mkdir()
    _fill_payload(staging)
    manifest = _publisher(tmp_path).build_artifact_manifest()
    (staging / "artifact_manifest.json").write_text(json.dumps(manifest))
    # exactly the v0.1.2 failure scene: success marker inside staging
    (staging / COMPLETION_MARKER).write_text(json.dumps({
        "status": "complete",
        "formal_run_valid": True,
        "run_id": RUN_ID,
        "head": HEAD,
        "schema": SCHEMA,
        "artifact_manifest_sha256": sha256_file(staging / "artifact_manifest.json"),
        "completed_at": "2026-09-06T13:56:22",
        "verifier": "inline_verify_pending_promotion",
    }))

    with pytest.raises(RuntimeError):
        verify_formal_artifact(staging, _registry(), **_formal_kwargs())
    with pytest.raises(RuntimeError):
        verify_formal_artifact(staging, _registry(), **_preflight_kwargs())


def test_preflight_does_not_require_completion_marker(tmp_path) -> None:
    publisher = _full_publish(tmp_path)
    result = verify_formal_artifact(
        publisher.staging, _registry(), **_preflight_kwargs()
    )
    assert result["complete"] is True
    assert not (publisher.staging / COMPLETION_MARKER).exists()


# ------------------------------------------------------ publication machine --


def test_publish_promotes_then_writes_marker_then_formal_verifies(tmp_path) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    assert final == tmp_path / RUN_ID
    assert publisher.staging.exists() is False
    marker = json.loads((final / COMPLETION_MARKER).read_text())
    assert marker["status"] == "complete"
    assert marker["formal_run_valid"] is True
    assert marker["run_id"] == RUN_ID and marker["head"] == HEAD
    assert marker["artifact_manifest_sha256"] == sha256_file(final / "artifact_manifest.json")
    # evidence must be a completed record, never a pending string
    assert isinstance(marker["preflight"], dict)
    assert marker["preflight"]["complete"] is True
    result = verify_formal_artifact(final, _registry(), **_formal_kwargs())
    assert result["complete"] is True


def test_marker_is_the_commit_point_missing_marker_fails(tmp_path) -> None:
    """Promotion without the completion marker stays fail-closed."""
    publisher = _full_publish(tmp_path)
    # simulate: promotion happened, marker write never did
    shutil.copytree(publisher.staging, tmp_path / RUN_ID)
    with pytest.raises(RuntimeError, match="COMPLETED"):
        verify_formal_artifact(tmp_path / RUN_ID, _registry(), **_formal_kwargs())


# ------------------------------------------------------- fault injections --


def test_fault_export_crash_is_fail_closed(tmp_path, monkeypatch) -> None:
    """1. payload export dies mid-way (one group file never written)."""
    staging = tmp_path / f"{RUN_ID}.incomplete"
    staging.mkdir()
    for group in PRIMARY_GROUPS[:-1]:
        export_group(staging, group, _result())
    (staging / "summary.json").write_text(json.dumps({"code_version": HEAD}))
    (staging / "manifest.json").write_text("{}")
    publisher = _publisher(tmp_path)
    with pytest.raises(RuntimeError):
        publisher.publish()
    assert (tmp_path / RUN_ID).exists() is False
    assert json.loads((staging / INCOMPLETE_MARKER).read_text())["status"] == "incomplete"
    assert (staging / COMPLETION_MARKER).exists() is False
    with pytest.raises(RuntimeError):
        verify_formal_artifact(staging, _registry(), **_formal_kwargs())


def test_fault_before_manifest_is_fail_closed(tmp_path) -> None:
    """2. crash between payload export and manifest write."""
    staging = tmp_path / f"{RUN_ID}.incomplete"
    staging.mkdir()
    _fill_payload(staging)
    with pytest.raises(RuntimeError):
        verify_formal_artifact(staging, _registry(), **_formal_kwargs())
    # publishing now still works (payload is complete); the pre-manifest
    # state itself was never formal
    publisher = _publisher(tmp_path)
    publisher.publish()


def test_fault_after_manifest_before_preflight_is_fail_closed(
    tmp_path, monkeypatch
) -> None:
    """3. crash after the manifest is written but before preflight."""
    publisher = _full_publish(tmp_path)
    calls = []

    real_verify = verify_formal_artifact

    def exploding_verify(*args, **kwargs):
        calls.append(1)
        raise KeyboardInterrupt("process killed after manifest write")

    monkeypatch.setattr(
        "quantlab.backtest.artifacts.verify_formal_artifact", exploding_verify
    )
    with pytest.raises(KeyboardInterrupt):
        publisher.publish()
    monkeypatch.setattr(
        "quantlab.backtest.artifacts.verify_formal_artifact", real_verify
    )
    # staged success marker must not survive (mark_incomplete invalidates it)
    assert (publisher.staging / COMPLETION_MARKER).exists() is False
    assert (publisher.staging / INCOMPLETE_MARKER).exists()
    assert (tmp_path / RUN_ID).exists() is False
    with pytest.raises(RuntimeError):
        verify_formal_artifact(publisher.staging, _registry(), **_formal_kwargs())


def test_fault_after_preflight_before_promotion_is_fail_closed(
    tmp_path, monkeypatch
) -> None:
    """4. crash between preflight and promotion."""
    publisher = _full_publish(tmp_path)
    real_replace = __import__("os").replace

    def exploding_replace(a, b):
        if str(b) == str(publisher.final):
            raise KeyboardInterrupt("killed after preflight, before promotion")
        return real_replace(a, b)

    monkeypatch.setattr("quantlab.backtest.artifacts.os.replace", exploding_replace)
    with pytest.raises(KeyboardInterrupt):
        publisher.publish()
    monkeypatch.setattr("quantlab.backtest.artifacts.os.replace", real_replace)
    assert (publisher.staging / COMPLETION_MARKER).exists() is False
    assert (publisher.staging / INCOMPLETE_MARKER).exists()
    with pytest.raises(RuntimeError):
        verify_formal_artifact(publisher.staging, _registry(), **_formal_kwargs())


def test_fault_after_promotion_before_marker_is_fail_closed(
    tmp_path, monkeypatch
) -> None:
    """5. crash after promotion but before the completion marker write."""
    publisher = _full_publish(tmp_path)
    real_write = __import__(
        "quantlab.backtest.artifacts", fromlist=["atomic_write_json"]
    ).atomic_write_json

    def exploding_write(path, payload):
        if path.name == COMPLETION_MARKER:
            raise KeyboardInterrupt("killed after promotion, before marker")
        return real_write(path, payload)

    monkeypatch.setattr(
        "quantlab.backtest.artifacts.atomic_write_json", exploding_write
    )
    with pytest.raises(KeyboardInterrupt):
        publisher.publish()
    monkeypatch.setattr(
        "quantlab.backtest.artifacts.atomic_write_json", real_write
    )
    final = tmp_path / RUN_ID
    assert final.exists() and (final / COMPLETION_MARKER).exists() is False
    with pytest.raises(RuntimeError, match="verification failed"):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


def test_fault_during_marker_write_is_fail_closed(tmp_path) -> None:
    """6. completion marker write interrupted: a .tmp sidecar without the
    final marker must never verify."""
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    # simulate the interrupted-write state: marker removed, sidecar left
    (final / COMPLETION_MARKER).unlink()
    (final / (COMPLETION_MARKER + ".tmp")).write_text('{"status": "com')
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


def test_fault_after_marker_with_payload_corruption_is_fail_closed(
    tmp_path,
) -> None:
    """7. something corrupts a payload file after the marker was written:
    the formal verifier must fail despite formal_run_valid=true — it never
    trusts the marker, it re-derives truth from the bytes."""
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    csv = final / "primary_strategy_recovery_assumption_1_daily_records.csv"
    csv.write_text(csv.read_text() + "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n")
    with pytest.raises(RuntimeError, match="sha256"):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


# ----------------------------------------------------- metadata binding --


def test_formal_verifier_binds_directory_basename(tmp_path) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    renamed = tmp_path / "renamed_run"
    final.rename(renamed)
    with pytest.raises(RuntimeError):
        verify_formal_artifact(renamed, _registry(), **_formal_kwargs())


def test_formal_verifier_rejects_wrong_external_expectations(tmp_path) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    with pytest.raises(RuntimeError):
        verify_formal_artifact(
            final, _registry(),
            expected_run_id=RUN_ID, expected_head="0" * 40,
            expected_schema=SCHEMA, formal=True,
        )
    with pytest.raises(RuntimeError):
        verify_formal_artifact(
            final, _registry(),
            expected_run_id="19990101T000000", expected_head=HEAD,
            expected_schema=SCHEMA, formal=True,
        )
    with pytest.raises(RuntimeError):
        verify_formal_artifact(
            final, _registry(),
            expected_run_id=RUN_ID, expected_head=HEAD,
            expected_schema="some_other_schema", formal=True,
        )


def test_formal_verifier_rejects_registry_tampering(tmp_path) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    registry = _registry()
    registry["groups"].pop(PRIMARY_GROUPS[-1])
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, registry, **_formal_kwargs())
    registry = _registry()
    registry["groups"]["extra_group"] = list(GROUP_FILE_FAMILY)
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, registry, **_formal_kwargs())


def test_formal_verifier_rejects_extra_or_missing_files(tmp_path) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    (final / "undeclared.bin").write_text("x")
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())
    (final / "undeclared.bin").unlink()
    (final / "primary_strategy_recovery_assumption_1_daily_records.csv").unlink()
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


def test_formal_verifier_rejects_complete_and_incomplete_together(
    tmp_path,
) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    (final / INCOMPLETE_MARKER).write_text('{"status": "incomplete"}')
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


def test_formal_verifier_rejects_pending_or_missing_marker_evidence(
    tmp_path,
) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    marker_path = final / COMPLETION_MARKER
    marker = json.loads(marker_path.read_text())
    marker["preflight"] = "inline_verify_pending_promotion"
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())
    marker = json.loads(marker_path.read_text())
    del marker["artifact_manifest_sha256"]
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())
    marker_path.write_text("{not json")
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


def test_formal_verifier_rejects_marker_manifest_hash_mismatch(
    tmp_path,
) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    marker_path = final / COMPLETION_MARKER
    marker = json.loads(marker_path.read_text())
    marker["artifact_manifest_sha256"] = "0" * 64
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(RuntimeError, match="manifest"):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


def test_formal_verifier_rejects_summary_manifest_metadata_disagreement(
    tmp_path,
) -> None:
    publisher = _full_publish(tmp_path)
    final = publisher.publish()
    summary_path = final / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["code_version"] = "0" * 40
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(RuntimeError):
        verify_formal_artifact(final, _registry(), **_formal_kwargs())


# ------------------------------------------------------------------- CLI --


def test_cli_formal_mode_requires_expected_head(tmp_path) -> None:
    """The formal CLI refuses to run without an explicit external HEAD;
    the diagnostic-only mode must be named explicitly and is not formal."""
    import subprocess
    import sys
    from pathlib import Path

    staging = tmp_path / f"{RUN_ID}.incomplete"
    staging.mkdir()
    _fill_payload(staging, groups=ALL_GROUPS)
    publisher = ArtifactPublisher(
        tmp_path, RUN_ID, expected_registry=_full_registry(), head=HEAD,
        schema=SCHEMA,
    )
    final = publisher.publish()
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "verify_formal_run.py"
    )

    without_head = subprocess.run(
        [sys.executable, str(script), str(final)],
        capture_output=True, text=True,
    )
    assert without_head.returncode != 0

    formal = subprocess.run(
        [
            sys.executable, str(script), str(final),
            "--expected-head", HEAD,
            "--expected-schema", SCHEMA,
            "--expected-run-id", RUN_ID,
        ],
        capture_output=True, text=True,
    )
    assert formal.returncode == 0, formal.stdout + formal.stderr

    # diagnostic mode is explicitly non-formal: it checks embedded metadata
    # and payload integrity against the canonical registry but binds no
    # external HEAD; a hash-tampered payload still fails it
    csv = final / "primary_strategy_recovery_assumption_1_daily_records.csv"
    original = csv.read_text()
    csv.write_text(original + "0,0\n")
    diag_tampered = subprocess.run(
        [sys.executable, str(script), str(final), "--mode", "diagnostic"],
        capture_output=True, text=True,
    )
    assert diag_tampered.returncode != 0
    csv.write_text(original)
