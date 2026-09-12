"""Source-preserving component terms, not entitlements, cash postings or PIT features."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.cohort_dividends import pushed_head
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries

CONFIG = "config/corporate_minimum_terms_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/corporate_minimum_terms"
RESOURCES = {
    "max_generated_bytes": 64 * 1024**2,
    "max_rss_bytes": 2 * 1024**3,
    "reserve_host_D_bytes": 8 * 1024**3,
    "max_wakeup_seconds": 600,
    "next_partition_time_reserve_seconds": 60,
}
REQUIRED_CONTEXT = (
    "unique_economic_event",
    "complete_event_coverage",
    "entitled_record_date_holdings",
    "calendar_and_actual_availability",
    "tax_and_cash_posting_rules",
    "original_disclosure_if_used_as_signal",
)


@dataclass(frozen=True)
class MinimumCorporateTerms:
    observation_id: str
    content_id: str
    instrument_id: str
    response_fingerprint: str
    observed_at: str
    availability_bound_source: str
    source_stage: str | None
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    share_listing_date: date | None
    cash_before_tax: Decimal | None
    total_stock_ratio: Decimal | None
    bonus_ratio: Decimal | None
    conversion_ratio: Decimal | None
    cash_state: str
    quantity_state: str
    breakdown_state: str
    common_blockers: tuple[str, ...]
    cash_blockers: tuple[str, ...]
    quantity_blockers: tuple[str, ...]
    required_context: tuple[str, ...]
    cashflow_eligible: bool = field(default=False, init=False)
    historical_pit_certified: bool = field(default=False, init=False)
    execution_authority: bool = field(default=False, init=False)

    @property
    def cash_fields_complete(self):
        return not self.common_blockers and not self.cash_blockers

    @property
    def quantity_fields_complete(self):
        return not self.common_blockers and not self.quantity_blockers

    def payload(self):
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, Decimal):
                result[key] = str(value)
            elif type(value) is date:
                result[key] = value.isoformat()
            elif isinstance(value, tuple):
                result[key] = list(value)
        result.update(
            cash_fields_complete=self.cash_fields_complete,
            quantity_fields_complete=self.quantity_fields_complete,
            numerical_date_bundle_complete=(
                self.cash_fields_complete and self.quantity_fields_complete
            ),
        )
        return result


def minimum_terms(row):
    for key in ("cashflow_eligible", "historical_pit_certified"):
        if row[key] is not False:
            raise DataValidationError("source observation cannot grant historical authority")
    flags = json.loads(row["quality_flags"])
    common = {f for f in flags if f.startswith("malformed_") or f == "unknown_status"}
    if not row["implementation_candidate"] or row["normalized_status"] != "实施":
        common.add("not_implementation")
    if row["candidate_conflict"] or row["possible_event_conflict"]:
        common.add("unresolved_source_conflict")
    amounts = []
    for key in ("cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"):
        value = row[key]
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
        ):
            raise DataValidationError("terms require previously parsed nonnegative measures")
        amounts.append(None if value is None else Decimal(str(value)))
    cash, total, bonus, conversion = amounts
    days = []
    for key in ("record_date", "ex_date", "pay_date", "div_listdate"):
        value = row[key]
        if value is None:
            days.append(None)
        else:
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError
                days.append(parsed)
            except (TypeError, ValueError) as exc:
                raise DataValidationError("term date was not normalized by source adapter") from exc
    record, ex_day, pay, listing = days
    if record is None or ex_day is None:
        common.add("record_or_ex_unknown")
    elif record >= ex_day:
        common.add("record_not_before_ex")
    cash_issues, quantity_issues = set(), set()
    cash_state = "unknown" if cash is None else "known_zero" if cash == 0 else "known_positive"
    if cash is None:
        cash_issues.add("cash_before_tax_unknown")
    elif cash > 0 and pay is None:
        cash_issues.add("positive_cash_pay_date_unknown")
    if pay and ex_day and pay < ex_day:
        cash_issues.add("payment_before_ex")
    breakdown = "total_unknown"
    quantity_state = "unknown"
    if total is None:
        quantity_issues.add("total_stock_ratio_unknown")
    else:
        partial = [v for v in (bonus, conversion) if v is not None]
        inconsistent = any(v > total for v in partial) or sum(
            partial, Decimal(0)
        ) > total + Decimal("1e-8")
        if len(partial) == 2:
            inconsistent |= abs(total - bonus - conversion) > Decimal("1e-8")
        if inconsistent:
            breakdown = "inconsistent"
            quantity_issues.add("observed_stock_components_inconsistent")
        elif total == 0:
            breakdown = "not_required_for_zero_total"
        elif len(partial) == 2:
            breakdown = "complete_observed"
        else:
            breakdown = "partial_unknown"
        quantity_state = "known_no_quantity_change" if total == 0 else "known_positive_ratio"
        if total > 0 and listing is None:
            quantity_issues.add("positive_stock_listing_date_unknown")
    if listing and ex_day and listing < ex_day:
        quantity_issues.add("listing_before_ex")
    context = list(REQUIRED_CONTEXT)
    if total and total > 0:
        context.append("fractional_share_allocation_and_sellable_date")
        if breakdown != "complete_observed":
            context.append("stock_distribution_tax_breakdown")
    return MinimumCorporateTerms(
        row["observation_id"],
        row["content_id"],
        row["instrument_id"],
        row["response_fingerprint"],
        row["observed_at"],
        row["availability_bound_source"],
        row["normalized_status"],
        record,
        ex_day,
        pay,
        listing,
        cash,
        total,
        bonus,
        conversion,
        cash_state,
        quantity_state,
        breakdown,
        tuple(sorted(common)),
        tuple(sorted(cash_issues)),
        tuple(sorted(quantity_issues)),
        tuple(context),
    )


def summary(rows):
    return {
        "occurrences": len(rows),
        "codes": len({r["instrument_id"] for r in rows}),
        "cash_state": dict(Counter(r["cash_state"] for r in rows)),
        "quantity_state": dict(Counter(r["quantity_state"] for r in rows)),
        "breakdown_state": dict(Counter(r["breakdown_state"] for r in rows)),
        **{
            k: sum(r[k] for r in rows)
            for k in (
                "cash_fields_complete",
                "quantity_fields_complete",
                "numerical_date_bundle_complete",
            )
        },
        "blockers": dict(
            Counter(
                b
                for r in rows
                for key in ("common_blockers", "cash_blockers", "quantity_blockers")
                for b in r[key]
            )
        ),
    }


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "corporate_minimum_terms_v1",
        "output": OUTPUT,
        "window": ["2020-01-01", "2024-12-31"],
        "occurrences": 945,
        "implementation_occurrences": 924,
        "provider_requests": 0,
        "economic_paths": 0,
        "model_fits": 0,
        "execution_authority": False,
        "resources": RESOURCES,
    }
    if any(config.get(k) != v or type(config.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed minimum-term contract")
    if set(config["paths"].values()) != set(config["inputs"]) or len(config["inputs"]) != 7:
        raise DataValidationError("minimum-term source scope changed")
    verify_entries(root, config["inputs"])
    report = sealed_read(root / config["paths"]["parent_report"])
    proof = sealed_read(root / config["paths"]["parent_proof"])
    if (
        report["fingerprint"] != "fc738aa6eaad461d91e3acb6929ba965431779c8ebbbf4fbc30ea112708a5ddb"
        or proof["report_fingerprint"] != report["fingerprint"]
        or proof["all_checks_passed"] is not True
    ):
        raise DataValidationError("minimum terms require the sealed cohort proof")
    return config


def run(root):
    config = load_contract(root)
    head, config_sha = pushed_head(root), _sha(root / CONFIG)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        out.mkdir(parents=True, exist_ok=False)
        budget = Budget(out, RESOURCES)
        atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "config_sha256": config_sha,
                "actual_attempt": 1,
            },
        )
        try:
            with budget.watchdog():
                budget.check()
                parent = pq.read_table(
                    root / config["paths"]["occurrences"], use_threads=False
                ).to_pylist()
                source_rows = [r for r in parent if r["observed_date_scope"] == "in_window"]
                if (
                    len(source_rows) != 945
                    or sum(r["implementation_candidate"] for r in source_rows) != 924
                ):
                    raise DataValidationError("fixed minimum-term population changed")
                rows = [minimum_terms(r).payload() for r in source_rows]
                pq.write_table(
                    pa.Table.from_pylist(rows), out / "terms.parquet", compression="zstd"
                )
                load_contract(root)
                if pushed_head(root) != head or _sha(root / CONFIG) != config_sha:
                    raise DataValidationError("minimum-term source changed during run")
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "config_sha256": config_sha,
                        "all": summary(rows),
                        "implementation": summary([r for r in rows if r["source_stage"] == "实施"]),
                        "artifact": {
                            "sha256": _sha(out / "terms.parquet"),
                            "bytes": (out / "terms.parquet").stat().st_size,
                        },
                        "provider_requests": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "execution_authority": False,
                        "cashflow_eligible": False,
                        "historical_pit_certified": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "artifact_bytes_before_report": budget.check(),
                        },
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {"at": datetime.now(UTC).isoformat(), "error_type": type(exc).__name__},
            )
            raise
