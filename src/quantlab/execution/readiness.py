"""Evidence-driven execution readiness audit without PnL or fill claims."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from quantlab.artifacts import sha256_file
from quantlab.data.security_history import load_security_code_changes
from quantlab.execution.rules import PITRuleBook, default_a_share_rule_book


class ReadinessStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    NOT_MODELED = "not_modeled"
    NOT_APPLICABLE = "not_applicable"


READINESS_CHECK_IDS = (
    "artifact_protocol",
    "target_portfolio_handoff",
    "order_and_ledger_contracts",
    "canonical_calendar",
    "security_master_identity",
    "instrument_code_lineage",
    "raw_daily_bar_fields",
    "stock_st_partition_coverage",
    "suspension_partition_coverage",
    "pit_trading_rule_resolution",
    "t_plus_one_sellability",
    "price_limit_and_cage_readiness",
    "statutory_and_broker_fee_readiness",
    "corporate_action_share_ledger",
    "auction_minute_orderbook_fill_evidence",
    "paper_broker_gateway",
    "live_operational_controls",
)
FRAMEWORK_CHECK_IDS = frozenset({
    "artifact_protocol",
    "target_portfolio_handoff",
    "order_and_ledger_contracts",
    "t_plus_one_sellability",
})

# v0.2 extends the canonical inventory with the audited order-path
# capabilities; the v0.1 inventory stays frozen for the v0.1 artifacts.
_SUBMISSION_CHECKS_V0_2 = (
    "production_submission_path_reachable",
    "account_aware_order_planning",
    "atomic_cash_reservation",
    "atomic_share_reservation",
    "stale_assessment_rejection",
    "day_trade_date_binding",
    "calendar_derived_t_plus_one",
)
READINESS_CHECK_IDS_V0_2 = (
    READINESS_CHECK_IDS[0],
    READINESS_CHECK_IDS[1],
    READINESS_CHECK_IDS[2],
    *_SUBMISSION_CHECKS_V0_2,
    *READINESS_CHECK_IDS[3:],
)
FRAMEWORK_CHECK_IDS_V0_2 = FRAMEWORK_CHECK_IDS | set(_SUBMISSION_CHECKS_V0_2)


def readiness_check_ids(schema: str) -> tuple[str, ...]:
    """Canonical check inventory for one readiness schema version."""
    if schema == "execution_readiness_v0_2":
        return READINESS_CHECK_IDS_V0_2
    return READINESS_CHECK_IDS


def framework_check_ids(schema: str) -> frozenset[str]:
    if schema == "execution_readiness_v0_2":
        return FRAMEWORK_CHECK_IDS_V0_2
    return FRAMEWORK_CHECK_IDS


@dataclass(frozen=True)
class ReadinessCheck:
    check_id: str
    category: str
    status: ReadinessStatus
    finding: str
    evidence: dict[str, Any]
    limitation: str
    critical_for: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "status": self.status.value,
            "finding": self.finding,
            "evidence": self.evidence,
            "limitation": self.limitation,
            "critical_for": list(self.critical_for),
        }


@dataclass(frozen=True)
class ExecutionReadinessReport:
    period_start: date
    period_end: date
    checks: tuple[ReadinessCheck, ...]
    framework_valid: bool
    historical_execution_ready: bool
    paper_execution_ready: bool
    live_execution_ready: bool
    schema: str = "execution_readiness_v0_1"

    def __post_init__(self) -> None:
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate readiness check_id")
        if tuple(ids) != readiness_check_ids(self.schema):
            raise ValueError("readiness check inventory or order is non-canonical")
        if self.live_execution_ready and not self.paper_execution_ready:
            raise ValueError("live readiness cannot exceed paper readiness")

    @property
    def gates(self) -> dict[str, bool]:
        return {
            "framework_valid": self.framework_valid,
            "historical_execution_ready": self.historical_execution_ready,
            "paper_execution_ready": self.paper_execution_ready,
            "live_execution_ready": self.live_execution_ready,
        }

    @property
    def status_counts(self) -> dict[str, int]:
        return {
            status.value: sum(check.status is status for check in self.checks)
            for status in ReadinessStatus
        }


_RAW_BAR_FIELDS = {
    "instrument_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
}
_ST_FIELDS = {
    "instrument_id",
    "trade_date",
    "name",
    "status",
    "type_name",
    "source_record_id",
}
_SUSPENSION_FIELDS = {
    "instrument_id",
    "trade_date",
    "suspend_type",
    "suspend_timing",
    "source_record_id",
}
_SECURITY_FIELDS = {
    "instrument_id",
    "symbol",
    "name",
    "exchange",
    "market",
    "board",
    "list_status",
    "list_date",
    "delist_date",
}


def _all_dates(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


def _content_snapshot(
    paths: list[Path],
    *,
    root: Path,
    expected_fields: set[str],
) -> dict[str, Any]:
    digest = hashlib.sha256()
    existing = [path for path in paths if path.exists()]
    missing = [path for path in paths if not path.exists()]
    schema_invalid: list[Path] = []
    total_bytes = 0
    for path in paths:
        relative = str(path.relative_to(root))
        digest.update(relative.encode())
        if not path.exists():
            digest.update(b"MISSING")
            continue
        file_digest = sha256_file(path)
        digest.update(file_digest.encode())
        total_bytes += path.stat().st_size
        if not expected_fields <= set(pq.read_schema(path).names):
            schema_invalid.append(path)

    schema_samples: list[dict[str, Any]] = []
    for path in tuple(existing[:1] + existing[-1:]):
        fields = set(pq.read_schema(path).names)
        schema_samples.append({
            "path": str(path.relative_to(root)),
            "required_fields_present": expected_fields <= fields,
            "fields": sorted(fields),
        })
    return {
        "expected_files": len(paths),
        "existing_files": len(existing),
        "missing_files": len(missing),
        "missing_examples": [str(path.relative_to(root)) for path in missing[:10]],
        "schema_invalid_files": len(schema_invalid),
        "schema_invalid_examples": [
            str(path.relative_to(root)) for path in schema_invalid[:10]
        ],
        "total_bytes": total_bytes,
        "combined_content_sha256": digest.hexdigest(),
        "schema_samples": schema_samples,
        "all_schemas_valid": bool(existing) and not schema_invalid,
    }


def audit_partition_rows(
    root: str | Path,
    family: str,
    open_dates: list[date],
    *,
    expected_fields: set[str],
    family_kind: str,
) -> dict[str, Any]:
    """Row-level audit of every declared partition in one family.

    Per partition: the partition path date must equal every row's
    ``trade_date``; primary keys must be unique; required fields must be
    non-null and finite; daily bars must satisfy valid OHLC relations and
    non-negative volume/amount. Findings and anomaly samples are returned
    for the evidence file.
    """
    root_path = Path(root)
    audited_partitions = 0
    audited_rows = 0
    date_mismatches = 0
    duplicate_primary_keys = 0
    null_or_nonfinite = 0
    ohlc_violations = 0
    negative_volume_amount = 0
    anomaly_samples: list[dict[str, Any]] = []

    def _sample(kind: str, path: Path, mask: pd.Series) -> None:
        if len(anomaly_samples) >= 10:
            return
        offending = path_frame.loc[mask]
        for _, row in offending.head(2).iterrows():
            anomaly_samples.append({
                "partition": str(path.relative_to(root_path)),
                "kind": kind,
                "row": {
                    key: (
                        value.isoformat() if hasattr(value, "isoformat") else value
                    )
                    for key, value in list(row.items())[:12]
                },
            })

    for value in open_dates:
        path = (
            root_path / family / f"year={value.year}" / f"month={value.month:02d}"
            / f"{value.isoformat()}.parquet"
        )
        if not path.exists():
            continue
        path_frame = pd.read_parquet(path)
        audited_partitions += 1
        if path_frame.empty:
            continue
        audited_rows += len(path_frame)
        path_frame = path_frame.copy()
        normalized = pd.to_datetime(path_frame["trade_date"]).dt.date
        date_mask = normalized != value
        date_mismatches += int(date_mask.sum())
        _sample("trade_date_mismatch", path, date_mask)

        key_series = (
            path_frame["instrument_id"].astype(str)
            + "|"
            + normalized.astype(str)
            if family_kind == "daily"
            else path_frame["instrument_id"].astype(str)
        )
        duplicate_mask = key_series.duplicated()
        duplicate_primary_keys += int(duplicate_mask.sum())
        _sample("duplicate_primary_key", path, duplicate_mask)

        null_mask = path_frame[list(expected_fields)].isna().any(axis=1)
        null_or_nonfinite += int(null_mask.sum())
        _sample("null_required_field", path, null_mask)

        if family_kind == "daily":
            low, high = path_frame["low"], path_frame["high"]
            open_, close_ = path_frame["open"], path_frame["close"]
            ohlc_mask = (
                (low <= 0)
                | (low > open_)
                | (low > close_)
                | (open_ > high)
                | (close_ > high)
            )
            ohlc_violations += int(ohlc_mask.sum())
            _sample("ohlc_relation", path, ohlc_mask)
            volume_mask = (path_frame["volume"] < 0) | (path_frame["amount"] < 0)
            negative_volume_amount += int(volume_mask.sum())
            _sample("negative_volume_or_amount", path, volume_mask)

    total_anomalies = (
        date_mismatches + duplicate_primary_keys + null_or_nonfinite
        + ohlc_violations + negative_volume_amount
    )
    return {
        "row_level_audit": True,
        "audited_partitions": audited_partitions,
        "audited_rows": audited_rows,
        "date_mismatches": date_mismatches,
        "duplicate_primary_keys": duplicate_primary_keys,
        "null_or_nonfinite_required_fields": null_or_nonfinite,
        "ohlc_relation_violations": ohlc_violations,
        "negative_volume_or_amount": negative_volume_amount,
        "total_anomalies": total_anomalies,
        "anomaly_samples": anomaly_samples,
        "all_rows_clean": total_anomalies == 0,
    }


def inspect_execution_inputs(
    canonical_root: str | Path,
    code_changes_path: str | Path,
    period_start: date,
    period_end: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inspect the declared period and cryptographically snapshot every partition."""
    root = Path(canonical_root)
    code_changes_path = Path(code_changes_path)
    calendar_path = root / "calendar" / "calendar.parquet"
    securities_path = root / "securities" / "securities.parquet"
    if not calendar_path.exists() or not securities_path.exists():
        raise RuntimeError("canonical calendar and security master are required")

    calendar = pd.read_parquet(calendar_path)
    required_calendar = {"exchange", "trade_date", "is_open"}
    if not required_calendar <= set(calendar.columns):
        raise RuntimeError("canonical calendar is missing required fields")
    calendar = calendar.copy()
    calendar["trade_date"] = pd.to_datetime(calendar["trade_date"]).dt.date
    scoped = calendar[
        calendar["trade_date"].between(period_start, period_end)
        & calendar["exchange"].isin(("SSE", "SZSE"))
    ]
    duplicate_calendar_rows = int(
        scoped.duplicated(["exchange", "trade_date"]).sum()
    )
    date_sets = {
        exchange: set(scoped.loc[scoped["exchange"] == exchange, "trade_date"])
        for exchange in ("SSE", "SZSE")
    }
    open_sets = {
        exchange: set(scoped.loc[
            (scoped["exchange"] == exchange) & scoped["is_open"], "trade_date"
        ])
        for exchange in ("SSE", "SZSE")
    }
    expected_dates = _all_dates(period_start, period_end)
    open_dates = sorted(open_sets["SSE"] | open_sets["SZSE"])

    def partition_paths(family: str) -> list[Path]:
        return [
            root / family / f"year={value.year}" / f"month={value.month:02d}"
            / f"{value.isoformat()}.parquet"
            for value in open_dates
        ]

    daily = _content_snapshot(
        partition_paths("daily"), root=root, expected_fields=_RAW_BAR_FIELDS
    )
    stock_st = _content_snapshot(
        partition_paths("lifecycle_context_v1/stock_st"),
        root=root,
        expected_fields=_ST_FIELDS,
    )
    suspensions = _content_snapshot(
        partition_paths("lifecycle_context_v1/suspensions"),
        root=root,
        expected_fields=_SUSPENSION_FIELDS,
    )
    daily["row_audit"] = audit_partition_rows(
        root, "daily", open_dates,
        expected_fields=_RAW_BAR_FIELDS, family_kind="daily",
    )
    stock_st["row_audit"] = audit_partition_rows(
        root, "lifecycle_context_v1/stock_st", open_dates,
        expected_fields=_ST_FIELDS, family_kind="context",
    )
    suspensions["row_audit"] = audit_partition_rows(
        root, "lifecycle_context_v1/suspensions", open_dates,
        expected_fields=_SUSPENSION_FIELDS, family_kind="context",
    )
    security_fields = set(pq.read_schema(securities_path).names)
    securities = pd.read_parquet(securities_path, columns=["instrument_id"])
    code_changes = load_security_code_changes(code_changes_path)
    evidence = {
        "calendar": {
            "period_calendar_days": len(expected_dates),
            "sse_calendar_days": len(date_sets["SSE"]),
            "szse_calendar_days": len(date_sets["SZSE"]),
            "all_calendar_days_present": (
                date_sets["SSE"] == expected_dates
                and date_sets["SZSE"] == expected_dates
            ),
            "sse_open_sessions": len(open_sets["SSE"]),
            "szse_open_sessions": len(open_sets["SZSE"]),
            "open_sessions_aligned": open_sets["SSE"] == open_sets["SZSE"],
            "duplicate_exchange_date_rows": duplicate_calendar_rows,
            "open_session_dates": [value.isoformat() for value in open_dates],
        },
        "security_master": {
            "rows": len(securities),
            "required_fields_present": _SECURITY_FIELDS <= security_fields,
            "fields": sorted(security_fields),
            "board_history_effective_dated": False,
        },
        "code_lineage": {
            "records": len(code_changes),
            "source": str(code_changes_path),
            "declared_complete_registry": False,
        },
        "daily": daily,
        "stock_st": stock_st,
        "suspensions": suspensions,
    }
    inventory = {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "calendar": {
            "path": str(calendar_path.relative_to(root)),
            "bytes": calendar_path.stat().st_size,
            "sha256": sha256_file(calendar_path),
        },
        "securities": {
            "path": str(securities_path.relative_to(root)),
            "bytes": securities_path.stat().st_size,
            "sha256": sha256_file(securities_path),
        },
        "code_lineage": {
            "path": str(code_changes_path),
            "bytes": code_changes_path.stat().st_size,
            "sha256": sha256_file(code_changes_path),
        },
        "partition_families": {
            name: {
                key: value
                for key, value in snapshot.items()
                if key not in {
                    "schema_samples",
                    "missing_examples",
                    "schema_invalid_examples",
                }
            }
            for name, snapshot in (
                ("daily", daily),
                ("stock_st", stock_st),
                ("suspensions", suspensions),
            )
        },
        "hash_scope": "all expected period partitions, byte SHA-256",
    }
    return evidence, inventory


def _rule_coverage(
    rules: PITRuleBook,
    open_session_dates: tuple[date, ...],
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    scopes = (
        ("SSE", "MAIN"),
        ("SSE", "STAR"),
        ("SZSE", "MAIN"),
        ("SZSE", "CHINEXT"),
    )
    result: dict[str, Any] = {}
    for exchange, board in scopes:
        scoped = [
            rule for rule in rules.rules
            if rule.exchange == exchange and rule.board == board
            and rule.effective_from <= period_end and rule.effective_to >= period_start
        ]
        missing = [
            value
            for value in open_session_dates
            if rules.resolve(exchange, board, value) is None
        ]
        result[f"{exchange}:{board}"] = {
            "rule_ids": [rule.rule_id for rule in scoped],
            "period_open_sessions": len(open_session_dates),
            "resolved_open_sessions": len(open_session_dates) - len(missing),
            "missing_open_sessions": len(missing),
            "first_missing_session": missing[0].isoformat() if missing else None,
            "last_missing_session": missing[-1].isoformat() if missing else None,
            "known_gap": "2023-04-10/2024-12-31" if exchange == "SZSE" else None,
            "full_period_resolved": not missing,
        }
    return result


def build_execution_readiness_report(
    evidence: dict[str, Any],
    *,
    period_start: date,
    period_end: date,
    handoff_smoke_valid: bool,
    rule_book: PITRuleBook | None = None,
    schema: str = "execution_readiness_v0_1",
    order_path_smoke: dict[str, Any] | None = None,
) -> tuple[ExecutionReadinessReport, dict[str, Any]]:
    """Apply frozen readiness semantics to observed evidence.

    ``schema`` selects the canonical check inventory: v0.2 extends the v0.1
    inventory with the audited order-path capabilities and upgrades the
    T+1 check once its calendar-bound derivation is proven by the smoke
    evidence in ``order_path_smoke``.
    """
    rule_book = rule_book or default_a_share_rule_book()
    is_v0_2 = schema == "execution_readiness_v0_2"
    smoke = order_path_smoke or {}
    calendar = evidence["calendar"]
    calendar_ready = bool(
        calendar["all_calendar_days_present"]
        and calendar["open_sessions_aligned"]
        and calendar["duplicate_exchange_date_rows"] == 0
        and calendar["sse_open_sessions"] > 0
    )
    family_ready = {
        name: (
            snapshot["missing_files"] == 0
            and snapshot["all_schemas_valid"]
            and snapshot.get("row_audit", {}).get("all_rows_clean", False)
        )
        for name, snapshot in (
            ("daily", evidence["daily"]),
            ("stock_st", evidence["stock_st"]),
            ("suspensions", evidence["suspensions"]),
        )
    }
    open_session_dates = tuple(
        date.fromisoformat(value) for value in calendar.get("open_session_dates", ())
    )
    rule_inventory = _rule_coverage(
        rule_book, open_session_dates, period_start, period_end
    )
    calendar_public = {
        key: value for key, value in calendar.items() if key != "open_session_dates"
    }
    master_status = (
        ReadinessStatus.PARTIAL
        if evidence["security_master"]["required_fields_present"]
        else ReadinessStatus.BLOCKED
    )
    checks = (
        ReadinessCheck(
            "artifact_protocol", "framework", ReadinessStatus.READY,
            "Domain-neutral fail-closed artifact contract is available.",
            {"publication": "staging -> manifest -> preflight -> promotion -> marker"},
            "Artifact validity does not imply trading readiness.", ("framework",),
        ),
        ReadinessCheck(
            "target_portfolio_handoff", "framework",
            ReadinessStatus.READY if handoff_smoke_valid else ReadinessStatus.BLOCKED,
            "Target weights convert to an all-or-none, share-denominated instruction.",
            {"smoke_valid": handoff_smoke_valid, "price_role": "planning_only"},
            "No instruction is an order or fill; unresolved positive targets suppress it.",
            ("framework",),
        ),
        ReadinessCheck(
            "order_and_ledger_contracts", "framework", ReadinessStatus.READY,
            "Typed orders, five-dimension decisions, and append-only ledger are implemented.",
            {"money_unit": "integer_fen", "timestamp_policy": "timezone_aware"},
            "No broker gateway is connected.", ("framework",),
        ),
        *(
            (
                ReadinessCheck(
                    "production_submission_path_reachable", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("production_submission_path_reachable")
                        else ReadinessStatus.BLOCKED
                    ),
                    "The real constraint engine reaches a submission-eligible "
                    "order with fill probability still unknown.",
                    {
                        "engine": "AShareConstraintEngine",
                        "derived_status": "validated",
                        "fillability_unknown_allowed": True,
                        "gates_rechecked": sorted(smoke.get(
                            "submission_gate_matrix", {}
                        )),
                    },
                    "Submission eligibility is not fillability.",
                    ("framework",),
                ),
                ReadinessCheck(
                    "account_aware_order_planning", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("account_aware_order_planning")
                        else ReadinessStatus.BLOCKED
                    ),
                    "Account-aware planning turns instructions into audited, "
                    "lot-conforming order legs with raw limit-price evidence.",
                    {
                        "plan_id_deterministic": smoke.get(
                            "order_plan_deterministic", False
                        ),
                        "omitted_held_name_exits": smoke.get(
                            "omitted_held_name_exits", False
                        ),
                        "non_conforming_delta_blocks": smoke.get(
                            "non_conforming_delta_blocks", False
                        ),
                        "buys_funded_from_available_cash_only": smoke.get(
                            "aggregate_cash_contention_blocked", False
                        ),
                    },
                    "A plan is never a submission.",
                    ("framework",),
                ),
                ReadinessCheck(
                    "atomic_cash_reservation", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("atomic_cash_reservation")
                        else ReadinessStatus.BLOCKED
                    ),
                    "Buy submissions reserve worst-case cash atomically; "
                    "contending orders cannot double-spend.",
                    {
                        "contention_blocked": smoke.get(
                            "aggregate_cash_contention_blocked", False
                        ),
                        "partial_fill_drawdown": smoke.get(
                            "partial_fill_drawdown", False
                        ),
                        "cancel_release": smoke.get("cancel_release", False),
                    },
                    "Production fee caps stay unknown without a real fee table.",
                    ("framework",),
                ),
                ReadinessCheck(
                    "atomic_share_reservation", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("atomic_share_reservation")
                        else ReadinessStatus.BLOCKED
                    ),
                    "Sell submissions reserve sellable shares atomically; "
                    "double-booking one lot fails closed.",
                    {"contention_blocked": smoke.get(
                        "share_contention_blocked", False
                    )},
                    "Corporate actions remain unmodeled.",
                    ("framework",),
                ),
                ReadinessCheck(
                    "stale_assessment_rejection", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("stale_assessment_rejected")
                        else ReadinessStatus.BLOCKED
                    ),
                    "Constraint assessments bind an account-state fingerprint "
                    "and stale ones are rejected.",
                    {"stale_rejected": smoke.get(
                        "stale_assessment_rejected", False
                    )},
                    "Re-assessment after any account mutation is mandatory.",
                    ("framework",),
                ),
                ReadinessCheck(
                    "day_trade_date_binding", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("wrong_trade_date_rejected")
                        else ReadinessStatus.BLOCKED
                    ),
                    "DAY orders bind their intended trade date end to end; "
                    "late reports cannot drift the economic trade date.",
                    {"wrong_trade_date_rejected": smoke.get(
                        "wrong_trade_date_rejected", False
                    )},
                    "DAY only; no GTC default exists.",
                    ("framework",),
                ),
                ReadinessCheck(
                    "calendar_derived_t_plus_one", "framework",
                    (
                        ReadinessStatus.READY
                        if smoke.get("weekend_t_plus_one_exact")
                        and smoke.get("missing_next_session_fail_closed")
                        else ReadinessStatus.BLOCKED
                    ),
                    "Buy-lot sellable_from is derived and verified as exactly "
                    "calendar.next_session(trade_date).",
                    {
                        "calendar_bound": True,
                        "weekend_exact": smoke.get(
                            "weekend_t_plus_one_exact", False
                        ),
                        "holiday_exact": smoke.get(
                            "holiday_t_plus_one_exact", False
                        ),
                        "incomplete_coverage_fail_closed": smoke.get(
                            "missing_next_session_fail_closed", False
                        ),
                    },
                    "T+1 is only as good as the calendar's coverage.",
                    ("framework",),
                ),
            )
            if is_v0_2
            else ()
        ),
        ReadinessCheck(
            "canonical_calendar", "historical_data",
            ReadinessStatus.READY if calendar_ready else ReadinessStatus.BLOCKED,
            "SSE/SZSE calendar coverage and session alignment were checked.",
            calendar_public,
            "Calendar completeness is necessary but does not prove instrument tradability.",
            ("historical",),
        ),
        ReadinessCheck(
            "security_master_identity", "historical_data", master_status,
            "Security master exists, but board/identity history is not fully effective-dated.",
            evidence["security_master"],
            "Current board labels cannot be assumed valid for every historical date.",
            ("historical",),
        ),
        ReadinessCheck(
            "instrument_code_lineage", "historical_data", ReadinessStatus.PARTIAL,
            "Curated code-change records are loaded but are not declared a complete registry.",
            evidence["code_lineage"],
            "Unknown historical code changes remain possible.", ("historical",),
        ),
        ReadinessCheck(
            "raw_daily_bar_fields", "historical_data",
            ReadinessStatus.READY if family_ready["daily"] else ReadinessStatus.BLOCKED,
            "Raw daily OHLCV partitions and required planning fields were audited.",
            evidence["daily"],
            "Daily bars may plan quantities or value positions; they never prove fills.",
            ("historical",),
        ),
        ReadinessCheck(
            "stock_st_partition_coverage", "historical_data",
            ReadinessStatus.READY if family_ready["stock_st"] else ReadinessStatus.PARTIAL,
            "Versioned ST context partitions cover the audited open sessions.",
            evidence["stock_st"],
            "ST remains context-only and does not alter rules or targets.", ("historical",),
        ),
        ReadinessCheck(
            "suspension_partition_coverage", "historical_data",
            ReadinessStatus.READY if family_ready["suspensions"] else ReadinessStatus.PARTIAL,
            "Versioned suspension event partitions cover the audited open sessions.",
            evidence["suspensions"],
            "Absence of an S/R row is not certified negative evidence of accessibility.",
            ("historical",),
        ),
        ReadinessCheck(
            "pit_trading_rule_resolution", "market_rules", ReadinessStatus.PARTIAL,
            "SSE scope resolves through 2024; SZSE is intentionally unresolved after 2023-04-09.",
            rule_inventory,
            "The missing byte-verified SZSE historical rule blocks complete execution replay.",
            ("historical",),
        ),
        ReadinessCheck(
            "t_plus_one_sellability", "market_rules",
            (
                ReadinessStatus.READY
                if is_v0_2
                and smoke.get("weekend_t_plus_one_exact")
                and smoke.get("missing_next_session_fail_closed")
                else ReadinessStatus.PARTIAL
            ),
            (
                "Lot state blocks same-day sale, and buy-lot sellable_from is "
                "derived from and verified against the canonical calendar's "
                "next session (weekend, holiday, and coverage boundaries "
                "tested)."
                if is_v0_2
                else "Lot state blocks same-day sale, but next-session "
                     "derivation is caller-supplied."
            ),
            {
                "model": "position_lot",
                "same_day_sale_blocked": True,
                "calendar_bound_next_session_derivation": is_v0_2,
                "weekend_exact": smoke.get("weekend_t_plus_one_exact", False),
                "incomplete_coverage_fail_closed": smoke.get(
                    "missing_next_session_fail_closed", False
                ),
            },
            "A supplied later date is validated as later, not as exactly the next session.",
            ("framework", "historical"),
        ),
        ReadinessCheck(
            "price_limit_and_cage_readiness", "market_rules", ReadinessStatus.NOT_MODELED,
            "Historical price bands, IPO exceptions, ST bands, and price cages are absent.",
            {"daily_pre_close_available": True, "rule_engine_available": False},
            "A daily bar alone cannot reconstruct admissible intraday prices.", ("historical",),
        ),
        ReadinessCheck(
            "statutory_and_broker_fee_readiness", "fees", ReadinessStatus.BLOCKED,
            "No exact effective-dated statutory fee and broker commission schedules are loaded.",
            {"fee_evidence_contract": True, "exact_schedules_loaded": False},
            "Costs must stay unknown; zero or generic bps would be false precision.",
            ("historical", "paper", "live"),
        ),
        ReadinessCheck(
            "corporate_action_share_ledger", "accounting", ReadinessStatus.NOT_MODELED,
            "Split, dividend, rights, merger, and conversion share/cash events are not posted.",
            {"cash_and_fill_ledger": True, "corporate_action_events": False},
            "Historical share quantities cannot be carried safely across these events.",
            ("historical",),
        ),
        ReadinessCheck(
            "auction_minute_orderbook_fill_evidence", "fillability",
            ReadinessStatus.NOT_MODELED,
            "Auction, minute, quote, queue, and order-book evidence are unavailable.",
            {"daily_ohlcv": family_ready["daily"], "intraday_or_orderbook": False},
            "No historical fill or slippage claim is permitted.", ("historical",),
        ),
        ReadinessCheck(
            "paper_broker_gateway", "paper", ReadinessStatus.NOT_MODELED,
            "Broker account, market-data, order, cancel, and reconciliation adapters are absent.",
            {"connected": False},
            "Framework tests do not authorize paper order submission.", ("paper",),
        ),
        ReadinessCheck(
            "live_operational_controls", "live", ReadinessStatus.NOT_MODELED,
            "Kill switch, limits, approvals, monitoring, and broker reconciliation are absent.",
            {"paper_gate": False, "operational_controls": False},
            "Live trading is prohibited.", ("live",),
        ),
    )
    framework_ids = framework_check_ids(schema)
    framework_valid = all(
        check.status in {ReadinessStatus.READY, ReadinessStatus.PARTIAL}
        for check in checks
        if check.check_id in framework_ids
    )
    historical_ready = all(
        check.status in {ReadinessStatus.READY, ReadinessStatus.NOT_APPLICABLE}
        for check in checks
        if "historical" in check.critical_for
    )
    paper_ready = framework_valid and all(
        check.status in {ReadinessStatus.READY, ReadinessStatus.NOT_APPLICABLE}
        for check in checks
        if "paper" in check.critical_for
    )
    live_ready = paper_ready and all(
        check.status in {ReadinessStatus.READY, ReadinessStatus.NOT_APPLICABLE}
        for check in checks
        if "live" in check.critical_for
    )
    return (
        ExecutionReadinessReport(
            period_start=period_start,
            period_end=period_end,
            checks=checks,
            framework_valid=framework_valid,
            historical_execution_ready=historical_ready,
            paper_execution_ready=paper_ready,
            live_execution_ready=live_ready,
            schema=schema,
        ),
        rule_inventory,
    )
