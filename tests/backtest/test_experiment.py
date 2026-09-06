"""Atomic-artifact and bounded-memory export tests (v0.1.2 closure).

Covers the three v0.1.1 failures: summary written before exports, unbounded
``position_rows`` lists, and non-atomic per-file writes that can leave a
formal-looking artifact directory after a mid-export crash.
"""

import json
from datetime import date

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

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)

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


def _config() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def _result(n_sessions: int = 2, positions_per_day: int = 3) -> BacktestResult:
    records, books, rebalances = [], [], []
    for i in range(n_sessions):
        d = date(2026, 1, 5 + i)
        records.append(
            DailyBacktestRecord(
                trade_date=d,
                nav_gross=1.0 + 0.01 * i,
                nav_net=1.0 + 0.01 * i,
                daily_return_gross=0.0,
                daily_return_net=0.0,
                gross_exposure=0.5,
                net_exposure=0.5,
                cash_weight=0.5,
                turnover=0.0,
                traded_notional_ratio=0.0,
                transaction_cost=0.0,
                holdings_count=positions_per_day,
                gross_book_gross_exposure=0.5,
                gross_book_net_exposure=0.5,
                gross_book_cash_weight=0.5,
                gross_book_turnover=0.0,
                gross_book_traded_notional_ratio=0.0,
                gross_book_holdings_count=positions_per_day,
            )
        )
        for book in ("gross", "net"):
            books.append(
                BookSnapshot(
                    trade_date=d,
                    book=book,
                    nav=1.0,
                    daily_return=0.0,
                    cash=0.5,
                    market_pnl=0.0,
                    fee=0.0,
                    gross_exposure=0.5,
                    net_exposure=0.5,
                    cash_weight=0.5,
                    holdings_count=positions_per_day,
                    positions=tuple(
                        PositionRecord(
                            instrument_id=f"I{j}",
                            value=0.1,
                            weight=0.1,
                            last_price=1.0,
                            last_mark_date=d,
                            missing_price=False,
                        )
                        for j in range(positions_per_day)
                    ),
                )
            )
        rebalance_fields = dict(
            signal_date=d,
            execution_date=d,
            target_count=positions_per_day,
            nonzero_trade_count=0,
            unavailable_target_count=0,
            frozen_count=0,
            restricted_binding_count=0,
            gross_book_restricted_binding_count=0,
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
        rebalances.append(RebalanceRecord(**rebalance_fields, **zeros))
    return BacktestResult(
        run_mode="strict",
        status="completed",
        requested_period_start=date(2026, 1, 5),
        requested_period_end=date(2026, 1, 6),
        simulated_period_start=date(2026, 1, 5),
        simulated_period_end=date(2026, 1, 6),
        valid_through=date(2026, 1, 6),
        diagnostic_from=None,
        first_blocking_event=None,
        records=records,
        rebalances=rebalances,
        books=books,
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


# ------------------------------------------------------ bounded, atomic CSVs --


def test_export_group_writes_complete_family_with_row_counts(tmp_path) -> None:
    result = _result(n_sessions=2, positions_per_day=3)
    manifest = export_group(tmp_path, "g", result)
    for kind in GROUP_FILE_FAMILY:
        assert (tmp_path / f"g_{kind}").exists(), kind
    assert manifest["complete"] is True
    assert manifest["files"]["g_daily_positions.csv"]["rows"] == 2 * 2 * 3
    assert manifest["files"]["g_daily_books.csv"]["rows"] == 2 * 2
    assert manifest["files"]["g_daily_records.csv"]["rows"] == 2
    # headers stable even for empty extracts
    for kind in (
        "trade_details.csv", "lifecycle_events.csv", "risk_policy_audit.csv",
        "forced_exit_attempts.csv", "successful_forced_exits.csv",
        "pending_no_price.csv", "prevented_entry_refill.csv",
        "blocked_before_exit.csv",
    ):
        header = (tmp_path / f"g_{kind}").read_text().splitlines()[0]
        assert header, kind
    assert json.loads((tmp_path / "g_failed_attempts.json").read_text()) == []


def test_export_group_positions_streamed_in_chunks(tmp_path) -> None:
    """The exporter must never materialize the full position row list.

    A chunk cap far below the position count still produces a complete,
    correct file.
    """
    result = _result(n_sessions=2, positions_per_day=50)
    manifest = export_group(tmp_path, "g", result, chunk_rows=7)
    lines = (tmp_path / "g_daily_positions.csv").read_text().splitlines()
    assert len(lines) == 1 + 2 * 2 * 50  # header + rows
    assert manifest["files"]["g_daily_positions.csv"]["rows"] == 200
    assert (tmp_path / "g_daily_positions.csv.tmp").exists() is False


def test_export_group_no_partial_final_file_on_mid_stream_error(tmp_path) -> None:
    """A crash mid-positions must not leave a half-written final CSV."""
    result = _result(n_sessions=3, positions_per_day=10)

    def exploding_rows():
        count = 0
        for b in result.books:
            for p in b.positions:
                count += 1
                if count > 25:
                    raise RuntimeError("boom mid-stream")
                yield {
                    "trade_date": b.trade_date, "book": b.book,
                    "instrument_id": p.instrument_id, "value": p.value,
                    "weight": p.weight, "last_price": p.last_price,
                    "last_mark_date": p.last_mark_date,
                    "missing_price": p.missing_price,
                }

    with pytest.raises(RuntimeError, match="boom"):
        export_group(tmp_path, "g", result, position_rows_iter=exploding_rows())
    # no half-written final file, and the temp sidecar was cleaned up
    assert (tmp_path / "g_daily_positions.csv").exists() is False
    assert (tmp_path / "g_daily_positions.csv.tmp").exists() is False


# ------------------------------------------------ directory-level publication --


PRIMARY_GROUPS = (
    "primary_strategy_recovery_assumption_1",
    "primary_strategy_recovery_assumption_0",
    "equal_weight_v1_control_recovery_assumption_1",
    "equal_weight_v1_control_recovery_assumption_0",
)


def _registry(tmp_path=None, groups=PRIMARY_GROUPS):
    return {
        "groups": {g: list(GROUP_FILE_FAMILY) for g in groups},
        "top_level": ["summary.json", "manifest.json"],
    }


def _write_completion_marker(staging) -> None:
    """Write the marker the way the publisher does (manifest hash bound)."""
    manifest_path = staging / "artifact_manifest.json"
    import json as _json
    from datetime import datetime as _dt

    _json_tmp = {
        "status": "complete",
        "formal_run_valid": True,
        "head": _json.loads((staging / "summary.json").read_text())["code_version"],
        "artifact_manifest_sha256": sha256_file(manifest_path),
        "completed_at": _dt.now().isoformat(),
        "verifier": "test",
    }
    (staging / COMPLETION_MARKER).write_text(_json.dumps(_json_tmp))


def _fill_group(tmp_path, prefix) -> None:
    result = _result()
    export_group(tmp_path, prefix, result)


def test_publisher_promotes_staging_only_after_verification(tmp_path) -> None:
    staging = tmp_path / "20260105T000000.incomplete"
    staging.mkdir()
    for group in PRIMARY_GROUPS:
        _fill_group(staging, group)
    (staging / "summary.json").write_text(json.dumps({"code_version": "head0"}))
    (staging / "manifest.json").write_text("{}")

    publisher = ArtifactPublisher(
        tmp_path, "20260105T000000",
        expected_registry=_registry(), head="head0",
    )
    final = publisher.publish()
    assert final == tmp_path / "20260105T000000"
    assert final.exists() and staging.exists() is False
    completed = json.loads((final / "COMPLETED.json").read_text())
    assert completed["formal_run_valid"] is True
    assert completed["head"] == "head0"
    assert (final / "artifact_manifest.json").exists()


def test_publisher_failure_keeps_incomplete_and_never_promotes(tmp_path) -> None:
    staging = tmp_path / "20260105T000000.incomplete"
    staging.mkdir()
    _fill_group(staging, "primary_strategy_recovery_assumption_1")
    publisher = ArtifactPublisher(
        tmp_path, "20260105T000000",
        expected_registry=_registry(), head="head0",
    )
    with pytest.raises(
        RuntimeError, match="equal_weight_v1_control_recovery_assumption_0"
    ):
        publisher.publish()
    assert (tmp_path / "20260105T000000").exists() is False
    assert json.loads((staging / INCOMPLETE_MARKER).read_text())["status"] == "incomplete"
    assert (staging / "COMPLETED.json").exists() is False


def test_verifier_fail_hard_on_hash_header_or_row_mismatch(tmp_path) -> None:
    staging = tmp_path / "20260105T000000.incomplete"
    staging.mkdir()
    for group in PRIMARY_GROUPS:
        _fill_group(staging, group)
    (staging / "summary.json").write_text(json.dumps({"code_version": "head0"}))
    (staging / "manifest.json").write_text("{}")
    publisher = ArtifactPublisher(
        tmp_path, "20260105T000000",
        expected_registry=_registry(), head="head0",
    )
    manifest = publisher.build_artifact_manifest()
    (staging / "artifact_manifest.json").write_text(json.dumps(manifest))
    _write_completion_marker(staging)

    csv = staging / "primary_strategy_recovery_assumption_1_daily_records.csv"

    # tamper with a file AFTER the manifest was written -> hash mismatch
    csv.write_text(csv.read_text() + "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n")
    with pytest.raises(RuntimeError, match="sha256"):
        verify_formal_artifact(staging, _registry(), expected_head="head0")

    # restore, then corrupt a header -> header mismatch
    _fill_group(staging, "primary_strategy_recovery_assumption_1")
    csv.write_text("wrong,header\n")
    with pytest.raises(RuntimeError, match="header"):
        verify_formal_artifact(staging, _registry(), expected_head="head0")

    # restore, then truncate rows -> row count mismatch
    _fill_group(staging, "primary_strategy_recovery_assumption_1")
    lines = csv.read_text().splitlines()
    csv.write_text("\n".join(lines[:-1]) + "\n")
    with pytest.raises(RuntimeError, match="row count"):
        verify_formal_artifact(staging, _registry(), expected_head="head0")


def test_verifier_requires_both_control_bounds_and_completion(tmp_path) -> None:
    staging = tmp_path / "20260105T000000.incomplete"
    staging.mkdir()
    registry = _registry()
    for g in registry["groups"]:
        _fill_group(staging, g)
    (staging / "summary.json").write_text(json.dumps({"code_version": "head0"}))
    (staging / "manifest.json").write_text("{}")
    publisher = ArtifactPublisher(
        tmp_path, "20260105T000000", expected_registry=registry, head="head0",
    )
    manifest = publisher.build_artifact_manifest()
    (staging / "artifact_manifest.json").write_text(json.dumps(manifest))
    _write_completion_marker(staging)
    result = verify_formal_artifact(staging, registry, expected_head="head0")
    assert result["complete"] is True

    # a control bound missing from the registry must fail the verification
    partial_registry = _registry(
        groups=(
            "primary_strategy_recovery_assumption_1",
            "primary_strategy_recovery_assumption_0",
            "equal_weight_v1_control_recovery_assumption_1",
        ),
    )
    with pytest.raises(RuntimeError, match="recovery"):
        verify_formal_artifact(staging, partial_registry, expected_head="head0")


def test_verifier_rejects_head_or_schema_mismatch(tmp_path) -> None:
    staging = tmp_path / "20260105T000000.incomplete"
    staging.mkdir()
    for group in PRIMARY_GROUPS:
        _fill_group(staging, group)
    summary = {
        "code_version": "headOTHER",
        "experiment_schema": "performance_baseline_benchmark_correctness_v0_1_2",
    }
    (staging / "summary.json").write_text(json.dumps(summary))
    (staging / "manifest.json").write_text("{}")
    publisher = ArtifactPublisher(
        tmp_path, "20260105T000000",
        expected_registry=_registry(), head="head0",
    )
    manifest = publisher.build_artifact_manifest(summary)
    (staging / "artifact_manifest.json").write_text(json.dumps(manifest))
    _write_completion_marker(staging)
    with pytest.raises(RuntimeError, match="HEAD"):
        verify_formal_artifact(
            staging, _registry(), expected_head="head0",
            expected_schema="performance_baseline_benchmark_correctness_v0_1_2",
        )
