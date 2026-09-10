"""Bounded, point-observed data enrichment for the Daily product."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

from quantlab.data.models import DataValidationError
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage


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
    provider: DataProvider, storage: ParquetStorage, trade_date: date
) -> EnrichmentDatasetResult:
    if storage.daily_price_limit_exists(trade_date):
        rows = storage.load_daily_price_limits_by_date(trade_date)
        return EnrichmentDatasetResult(
            "stk_limit", "reused", len(rows), str(storage.daily_price_limit_path(trade_date))
        )
    rows = provider.get_daily_price_limits_by_date(trade_date)
    security_ids = _daily_v1_security_ids(storage)
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
        if item.instrument_id in security_ids
    }
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


def inspect_enrichment_status(storage: ParquetStorage) -> dict:
    price_paths = sorted(storage.base_dir.glob("daily_price_limit/year=*/month=*/*.parquet"))
    financial_paths = sorted(
        storage.base_dir.glob("financial_indicator/observed_on=*/snapshot=*.parquet")
    )
    dividend_paths = sorted(storage.base_dir.glob("dividend/observed_on=*/snapshot=*.parquet"))
    return {
        "stk_limit": {
            "status": "available" if price_paths else "not_loaded",
            "partitions": len(price_paths),
            "latest_date": price_paths[-1].stem if price_paths else None,
        },
        "fina_indicator_vip": {
            "status": "prospective_only" if financial_paths else "not_loaded",
            "snapshots": len(financial_paths),
            "strict_historical_pit": False,
        },
        "dividend": {
            "status": "context_only" if dividend_paths else "not_loaded",
            "snapshots": len(dividend_paths),
            "account_postings": False,
        },
    }
