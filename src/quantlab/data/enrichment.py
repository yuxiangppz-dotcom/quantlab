"""Bounded, point-observed data enrichment for the Daily product."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from quantlab.data.models import DataValidationError, SecurityCodeChange, parse_instrument_id
from quantlab.data.provider import DataProvider
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage

_CODE_HISTORY = Path(__file__).resolve().parents[3] / "config/security_code_changes.csv"


@dataclass(frozen=True)
class EnrichmentDatasetResult:
    endpoint: str
    status: str
    rows: int
    path: str | None
    limitation: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _daily_v1_security_ids(storage: ParquetStorage) -> set[str]:
    """Match the frozen SH/SZ A-share product scope (no B shares or Beijing)."""
    return {
        item.instrument_id
        for item in storage.load_securities()
        if item.market in {"SH", "SZ"} and not item.symbol.startswith(("900", "200"))
    }


def sync_daily_price_limits(
    provider: DataProvider,
    storage: ParquetStorage,
    trade_date: date,
    *,
    security_code_changes: list[SecurityCodeChange] | None = None,
) -> EnrichmentDatasetResult:
    if storage.daily_price_limit_exists(trade_date):
        rows = storage.load_daily_price_limits_by_date(trade_date)
        return EnrichmentDatasetResult(
            "stk_limit", "reused", len(rows), str(storage.daily_price_limit_path(trade_date))
        )
    rows = provider.get_daily_price_limits_by_date(trade_date)
    security_ids = _daily_v1_security_ids(storage)
    changes = (
        load_security_code_changes(_CODE_HISTORY)
        if security_code_changes is None
        else security_code_changes
    )
    # Code validity is already established by the shared history authority.
    # Preserve the old identifier rather than rewriting it to today's code.
    inactive_code_ids: set[str] = set()
    for change in changes:
        old_symbol, old_market = parse_instrument_id(change.old_instrument_id)
        if old_market not in {"SH", "SZ"} or old_symbol.startswith(("900", "200")):
            continue
        if change.original_list_date <= trade_date < change.effective_date:
            security_ids.add(change.old_instrument_id)
        else:
            security_ids.discard(change.old_instrument_id)
            inactive_code_ids.add(change.old_instrument_id)
        if trade_date < change.effective_date:
            security_ids.discard(change.new_instrument_id)
            inactive_code_ids.add(change.new_instrument_id)
    rows = [item for item in rows if item.instrument_id in security_ids]
    if not rows:
        raise DataValidationError(f"stk_limit has no canonical A-share rows for {trade_date}")
    if any(
        item.trade_date != trade_date
        or not math.isfinite(item.up_limit)
        or not math.isfinite(item.down_limit)
        or item.up_limit <= 0
        or item.down_limit <= 0
        or item.down_limit > item.up_limit
        for item in rows
    ):
        raise DataValidationError(f"stk_limit contains invalid rows for {trade_date}")
    daily_ids = {
        item.instrument_id
        for item in storage.load_daily_bars_by_date(trade_date)
        if item.instrument_id.endswith((".SH", ".SZ"))
        and not item.instrument_id.startswith(("900", "200"))
        and item.instrument_id not in inactive_code_ids
    }
    unknown = daily_ids - security_ids
    if unknown:
        raise DataValidationError(
            f"stk_limit daily scope has {len(unknown)} unverified historical A-share identifiers "
            f"on {trade_date}"
        )
    limit_ids = {item.instrument_id for item in rows}
    missing = daily_ids - limit_ids
    if missing:
        raise DataValidationError(
            f"stk_limit misses {len(missing)} instruments with a daily bar on {trade_date}"
        )
    path = storage.save_daily_price_limits_by_date(rows, trade_date)
    return EnrichmentDatasetResult("stk_limit", "synced", len(rows), str(path))


def sync_financial_indicator_observation(
    provider: DataProvider, storage: ParquetStorage, period_end: date
) -> EnrichmentDatasetResult:
    rows = provider.get_financial_indicators_by_period(period_end)
    security_ids = _daily_v1_security_ids(storage)
    rows = [item for item in rows if item.instrument_id in security_ids]
    if not rows:
        return EnrichmentDatasetResult(
            "fina_indicator_vip",
            "available_no_rows",
            0,
            None,
            "No A-share records were returned for the requested reporting period.",
        )
    if any(item.period_end != period_end for item in rows):
        raise DataValidationError("financial indicator response crossed reporting periods")
    if len({item.source_record_id for item in rows}) != len(rows):
        raise DataValidationError("financial indicator response has duplicate source versions")
    path = storage.save_financial_indicator_observations(rows)
    return EnrichmentDatasetResult(
        "fina_indicator_vip",
        "observed_prospective_only",
        len(rows),
        str(path),
        "Revision timestamps are absent; rows are not eligible for retrospective PIT research.",
    )


def sync_dividend_observations(
    provider: DataProvider, storage: ParquetStorage, instrument_ids: tuple[str, ...]
) -> EnrichmentDatasetResult:
    allowed = {item.instrument_id for item in storage.load_securities()}
    requested = tuple(sorted(set(instrument_ids)))
    unknown = [instrument for instrument in requested if instrument not in allowed]
    if unknown:
        raise DataValidationError(
            f"dividend instruments are outside canonical securities: {unknown}"
        )
    rows = []
    for instrument in requested:
        received = provider.get_dividends(instrument)
        if any(item.instrument_id != instrument for item in received):
            raise DataValidationError("dividend response escaped requested instrument scope")
        rows.extend(received)
    if not rows:
        return EnrichmentDatasetResult(
            "dividend",
            "available_no_rows",
            0,
            None,
            "No dividend records were returned for the explicitly requested instruments.",
        )
    if len({item.source_record_id for item in rows}) != len(rows):
        raise DataValidationError("dividend response has duplicate source records")
    path = storage.save_dividend_observations(rows)
    return EnrichmentDatasetResult(
        "dividend",
        "observed_context_only",
        len(rows),
        str(path),
        "Context/warning only; no account cash or share postings are inferred.",
    )


def inspect_enrichment_status(storage: ParquetStorage, as_of: date | None = None) -> dict:
    price_paths = sorted(storage.base_dir.glob("daily_price_limit/year=*/month=*/*.parquet"))
    financial_paths = sorted(
        storage.base_dir.glob("financial_indicator/observed_on=*/snapshot=*.parquet")
    )
    dividend_paths = sorted(storage.base_dir.glob("dividend/observed_on=*/snapshot=*.parquet"))
    financial = storage.load_financial_indicator_observations() if financial_paths else []
    dividends = storage.load_dividend_observations() if dividend_paths else []
    eligible_financial = [
        item for item in financial if as_of is None or item.available_from <= as_of
    ]
    eligible_dividends = [
        item for item in dividends if as_of is None or item.available_from <= as_of
    ]
    return {
        "stk_limit": {
            "status": "available" if price_paths else "not_loaded",
            "partitions": len(price_paths),
            "latest_date": price_paths[-1].stem if price_paths else None,
        },
        "fina_indicator_vip": {
            "status": "prospective_only" if financial_paths else "not_loaded",
            "snapshots": len(financial_paths),
            "observed_versions": len(financial),
            "eligible_versions_as_of": len(eligible_financial),
            "latest_available_from": (
                max(item.available_from for item in financial).isoformat() if financial else None
            ),
            "latest_period_end": (
                max(item.period_end for item in eligible_financial).isoformat()
                if eligible_financial
                else None
            ),
            "strict_historical_pit": False,
        },
        "dividend": {
            "status": "context_only" if dividend_paths else "not_loaded",
            "snapshots": len(dividend_paths),
            "observed_events": len(dividends),
            "eligible_events_as_of": len(eligible_dividends),
            "latest_available_from": (
                max(item.available_from for item in dividends).isoformat() if dividends else None
            ),
            "account_postings": False,
        },
    }


def dividend_context_warnings(
    storage: ParquetStorage,
    instrument_ids: set[str],
    *,
    as_of: date,
    days_before: int = 30,
    days_after: int = 60,
) -> list[dict]:
    """Return observed corporate-action context near an account date.

    This is display-only evidence. It never posts cash or shares and uses only
    records that QuantLab had observed by ``as_of``.
    """
    from datetime import timedelta

    lower = as_of - timedelta(days=days_before)
    upper = as_of + timedelta(days=days_after)
    rows = []
    for item in storage.load_dividend_observations():
        if item.instrument_id not in instrument_ids or item.available_from > as_of:
            continue
        event_dates = {
            "record_date": item.record_date,
            "ex_date": item.ex_date,
            "pay_date": item.pay_date,
            "share_listing_date": item.share_listing_date,
        }
        nearby = {
            name: value.isoformat()
            for name, value in event_dates.items()
            if value is not None and lower <= value <= upper
        }
        if nearby:
            rows.append(
                {
                    "instrument_id": item.instrument_id,
                    "process_status": item.process_status,
                    "available_from": item.available_from.isoformat(),
                    **nearby,
                    "warning": "CORPORATE_ACTION_CONTEXT_REQUIRES_ACCOUNT_RECONCILIATION",
                }
            )
    return sorted(rows, key=lambda row: (row["instrument_id"], str(row)))
