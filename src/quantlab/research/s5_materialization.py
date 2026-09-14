"""Point-in-time feature materialization for the frozen S5 research kernel.

This module deliberately stops at research observations.  It never reads a
provider, writes Canonical data, evaluates future returns, promotes a strategy,
or creates an order.  Callers remain responsible for supplying trustworthy
source identities and point-in-time membership / universe evidence.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_sector_bottom import S5SectorObservation, S5StockObservation

_MATERIALIZER_SCHEMA = "quantlab_s5_materialization_v1"
_REQUIRED_VOL_CLOSES = 272


@dataclass(frozen=True)
class S5SeriesPoint:
    """One numeric observation in a caller-supplied research series.

    Values are intentionally validated only when their dates are consumed.  A
    future bad value must not make an earlier materialization depend on future
    information.
    """

    trade_date: date
    value: float


@dataclass(frozen=True)
class S5ResearchSeries:
    """Provider-neutral immutable series plus its source identity."""

    series_id: str
    source_id: str
    points: tuple[S5SeriesPoint, ...]

    def __post_init__(self) -> None:
        _require_text("series_id", self.series_id)
        _require_text("source_id", self.source_id)


@dataclass(frozen=True)
class S5MembershipEvidence:
    """One industry-membership fact with an inclusive effective interval."""

    instrument_id: str
    sector_id: str
    effective_from: date
    effective_to: date | None
    source_id: str
    pit_verified: bool

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _require_text("sector_id", self.sector_id)
        _require_text("source_id", self.source_id)
        if type(self.pit_verified) is not bool:
            raise ValueError("pit_verified must be bool")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("membership effective_to cannot precede effective_from")


@dataclass(frozen=True)
class S5EligibilityEvidence:
    """Research-universe eligibility as known for one exact decision date."""

    instrument_id: str
    as_of: date
    eligible: bool | None
    source_id: str
    pit_verified: bool

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _require_text("source_id", self.source_id)
        if self.eligible is not None and type(self.eligible) is not bool:
            raise ValueError("eligible must be bool or None")
        if type(self.pit_verified) is not bool:
            raise ValueError("pit_verified must be bool")


@dataclass(frozen=True)
class S5SectorSeriesInput:
    sector_id: str
    closes: S5ResearchSeries

    def __post_init__(self) -> None:
        _require_text("sector_id", self.sector_id)
        if self.closes.series_id != self.sector_id:
            raise ValueError("sector close series_id must equal sector_id")


@dataclass(frozen=True)
class S5StockSeriesInput:
    instrument_id: str
    sector_id: str
    closes: S5ResearchSeries
    volumes: S5ResearchSeries
    membership_evidence: tuple[S5MembershipEvidence, ...]
    eligibility_evidence: S5EligibilityEvidence | None

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _require_text("sector_id", self.sector_id)
        if self.closes.series_id != self.instrument_id:
            raise ValueError("stock close series_id must equal instrument_id")
        if self.volumes.series_id != self.instrument_id:
            raise ValueError("stock volume series_id must equal instrument_id")
        for evidence in self.membership_evidence:
            if evidence.instrument_id != self.instrument_id:
                raise ValueError("membership evidence instrument_id mismatch")
        if (
            self.eligibility_evidence is not None
            and self.eligibility_evidence.instrument_id != self.instrument_id
        ):
            raise ValueError("eligibility evidence instrument_id mismatch")


@dataclass(frozen=True)
class S5MaterializationIssue:
    entity_type: str
    entity_id: str
    field: str
    reason: str


@dataclass(frozen=True)
class S5Materialization:
    """Immutable S5 observations plus provenance for one decision date."""

    as_of: date
    sector_observations: tuple[S5SectorObservation, ...]
    stock_observations: tuple[S5StockObservation, ...]
    issues: tuple[S5MaterializationIssue, ...]
    fingerprint: str
    research_only: bool = True
    performance_claim: bool = False
    broker_order_authority: bool = False


def materialize_s5_inputs(
    *,
    as_of: date,
    sessions: tuple[date, ...],
    benchmark_closes: S5ResearchSeries,
    sectors: tuple[S5SectorSeriesInput, ...],
    stocks: tuple[S5StockSeriesInput, ...],
) -> S5Materialization:
    """Materialize the exact S5 v1 features from supplied PIT evidence."""

    _validate_sessions(as_of, sessions)
    used_sessions = sessions[-_REQUIRED_VOL_CLOSES:]
    benchmark = _series_map(benchmark_closes, used_sessions)

    sector_by_id: dict[str, S5SectorSeriesInput] = {}
    for sector in sectors:
        if sector.sector_id in sector_by_id:
            raise ValueError(f"duplicate sector_id: {sector.sector_id}")
        sector_by_id[sector.sector_id] = sector

    stock_by_id: dict[str, S5StockSeriesInput] = {}
    for stock in stocks:
        if stock.instrument_id in stock_by_id:
            raise ValueError(f"duplicate instrument_id: {stock.instrument_id}")
        stock_by_id[stock.instrument_id] = stock

    issues: list[S5MaterializationIssue] = []
    sector_observations: list[S5SectorObservation] = []
    sector_maps: dict[str, dict[date, float]] = {}

    for sector_id in sorted(sector_by_id):
        item = sector_by_id[sector_id]
        closes = _series_map(item.closes, used_sessions)
        sector_maps[sector_id] = closes

        drawdown, reason = _drawdown_120(sessions, closes)
        _append_issue(issues, "sector", sector_id, "drawdown_from_120d_high", reason)

        new_low_rate, reason = _new_low_rate_20(sessions, closes)
        _append_issue(issues, "sector", sector_id, "new_low_rate_20", reason)

        vol_percentile, reason = _volatility_percentile_252(sessions, closes)
        _append_issue(
            issues,
            "sector",
            sector_id,
            "volatility_percentile_252",
            reason,
        )

        sector_return, sector_return_reason = _simple_return(sessions, closes, 20)
        benchmark_return, benchmark_reason = _simple_return(sessions, benchmark, 20)
        relative_return: float | None
        relative_reason: str | None
        if sector_return is None:
            relative_return = None
            relative_reason = f"sector_{sector_return_reason}"
        elif benchmark_return is None:
            relative_return = None
            relative_reason = f"benchmark_{benchmark_reason}"
        else:
            relative_return = sector_return - benchmark_return
            relative_reason = None
        _append_issue(issues, "sector", sector_id, "relative_return_20", relative_reason)

        complete = all(
            value is not None
            for value in (drawdown, new_low_rate, vol_percentile, relative_return)
        )
        sector_observations.append(
            S5SectorObservation(
                sector_id=sector_id,
                as_of=as_of,
                drawdown_from_120d_high=drawdown,
                new_low_rate_20=new_low_rate,
                volatility_percentile_252=vol_percentile,
                relative_return_20=relative_return,
                history_complete=complete,
            )
        )

    stock_observations: list[S5StockObservation] = []
    for instrument_id in sorted(stock_by_id):
        item = stock_by_id[instrument_id]
        closes = _series_map(item.closes, used_sessions)
        volumes = _series_map(item.volumes, used_sessions)
        sector_closes = sector_maps.get(item.sector_id)

        membership = _resolve_membership(
            instrument_id=instrument_id,
            expected_sector_id=item.sector_id,
            as_of=as_of,
            evidence=item.membership_evidence,
        )
        if membership is None:
            _append_issue(
                issues,
                "stock",
                instrument_id,
                "membership_verified",
                "membership_unknown_or_conflicting",
            )

        eligibility = _resolve_eligibility(
            instrument_id=instrument_id,
            as_of=as_of,
            evidence=item.eligibility_evidence,
        )
        if eligibility is None:
            _append_issue(
                issues,
                "stock",
                instrument_id,
                "research_eligible",
                "eligibility_unknown_or_not_pit_verified",
            )

        stock_return, stock_return_reason = _simple_return(sessions, closes, 20)
        sector_return: float | None = None
        sector_return_reason: str | None = "sector_series_missing"
        if sector_closes is not None:
            sector_return, sector_return_reason = _simple_return(
                sessions, sector_closes, 20
            )
        relative_stock: float | None
        relative_stock_reason: str | None
        if stock_return is None:
            relative_stock = None
            relative_stock_reason = f"stock_{stock_return_reason}"
        elif sector_return is None:
            relative_stock = None
            relative_stock_reason = f"sector_{sector_return_reason}"
        else:
            relative_stock = stock_return - sector_return
            relative_stock_reason = None
        _append_issue(
            issues,
            "stock",
            instrument_id,
            "relative_return_20_vs_sector",
            relative_stock_reason,
        )

        close_to_ma20, reason = _close_to_ma20(sessions, closes)
        _append_issue(issues, "stock", instrument_id, "close_to_ma20_ratio", reason)

        ma20_slope, reason = _ma20_slope_5(sessions, closes)
        _append_issue(issues, "stock", instrument_id, "ma20_slope_5", reason)

        volume_ratio, reason = _up_down_volume_ratio_20(sessions, closes, volumes)
        _append_issue(
            issues,
            "stock",
            instrument_id,
            "up_down_volume_ratio_20",
            reason,
        )

        stock_observations.append(
            S5StockObservation(
                instrument_id=instrument_id,
                sector_id=item.sector_id,
                as_of=as_of,
                membership_verified=membership,
                relative_return_20_vs_sector=relative_stock,
                close_to_ma20_ratio=close_to_ma20,
                ma20_slope_5=ma20_slope,
                up_down_volume_ratio_20=volume_ratio,
                research_eligible=eligibility,
            )
        )

    fingerprint = _materialization_fingerprint(
        as_of=as_of,
        sessions=sessions,
        used_sessions=used_sessions,
        benchmark=benchmark_closes,
        sectors=tuple(sector_by_id[key] for key in sorted(sector_by_id)),
        stocks=tuple(stock_by_id[key] for key in sorted(stock_by_id)),
    )

    return S5Materialization(
        as_of=as_of,
        sector_observations=tuple(sector_observations),
        stock_observations=tuple(stock_observations),
        issues=tuple(
            sorted(
                issues,
                key=lambda issue: (
                    issue.entity_type,
                    issue.entity_id,
                    issue.field,
                    issue.reason,
                ),
            )
        ),
        fingerprint=fingerprint,
    )


def _drawdown_120(
    sessions: tuple[date, ...],
    closes: dict[date, float],
) -> tuple[float | None, str | None]:
    window, reason = _positive_window(sessions, closes, 120)
    if window is None:
        return None, reason
    return window[-1] / max(window) - 1.0, None


def _new_low_rate_20(
    sessions: tuple[date, ...],
    closes: dict[date, float],
) -> tuple[float | None, str | None]:
    if len(sessions) < 79:
        return None, "insufficient_history"
    needed = sessions[-79:]
    values, reason = _positive_dates(needed, closes)
    if values is None:
        return None, reason
    hits = 0
    for offset in range(19, 79):
        trailing = values[offset - 59 : offset + 1]
        if values[offset] <= min(trailing):
            hits += 1
    return hits / 20.0, None


def _volatility_percentile_252(
    sessions: tuple[date, ...],
    closes: dict[date, float],
) -> tuple[float | None, str | None]:
    if len(sessions) < _REQUIRED_VOL_CLOSES:
        return None, "insufficient_history"
    needed = sessions[-_REQUIRED_VOL_CLOSES:]
    prices, reason = _positive_dates(needed, closes)
    if prices is None:
        return None, reason
    returns = [prices[index] / prices[index - 1] - 1.0 for index in range(1, len(prices))]
    volatilities = [
        statistics.stdev(returns[end - 20 : end])
        for end in range(20, len(returns) + 1)
    ]
    if len(volatilities) != 252:
        raise AssertionError("S5 volatility history must contain exactly 252 endpoints")
    current = volatilities[-1]
    return sum(value <= current for value in volatilities) / 252.0, None


def _simple_return(
    sessions: tuple[date, ...],
    closes: dict[date, float],
    horizon: int,
) -> tuple[float | None, str | None]:
    required = horizon + 1
    window, reason = _positive_window(sessions, closes, required)
    if window is None:
        return None, reason
    return window[-1] / window[0] - 1.0, None


def _close_to_ma20(
    sessions: tuple[date, ...],
    closes: dict[date, float],
) -> tuple[float | None, str | None]:
    window, reason = _positive_window(sessions, closes, 20)
    if window is None:
        return None, reason
    return window[-1] / statistics.fmean(window), None


def _ma20_slope_5(
    sessions: tuple[date, ...],
    closes: dict[date, float],
) -> tuple[float | None, str | None]:
    window, reason = _positive_window(sessions, closes, 25)
    if window is None:
        return None, reason
    current = statistics.fmean(window[-20:])
    five_sessions_ago = statistics.fmean(window[:20])
    return current / five_sessions_ago - 1.0, None


def _up_down_volume_ratio_20(
    sessions: tuple[date, ...],
    closes: dict[date, float],
    volumes: dict[date, float],
) -> tuple[float | None, str | None]:
    if len(sessions) < 21:
        return None, "insufficient_history"
    dates = sessions[-21:]
    prices, reason = _positive_dates(dates, closes)
    if prices is None:
        return None, reason

    endpoint_dates = dates[1:]
    endpoint_volumes: list[float] = []
    for trade_date in endpoint_dates:
        value = volumes.get(trade_date)
        if value is None:
            return None, f"missing_value:{trade_date.isoformat()}"
        if not math.isfinite(value) or value < 0.0:
            return None, f"invalid_volume:{trade_date.isoformat()}"
        endpoint_volumes.append(value)

    positive: list[float] = []
    negative: list[float] = []
    for index, volume in enumerate(endpoint_volumes, start=1):
        daily_return = prices[index] / prices[index - 1] - 1.0
        if daily_return > 0.0:
            positive.append(volume)
        elif daily_return < 0.0:
            negative.append(volume)

    if not positive or not negative:
        return None, "one_sided_return_window"
    negative_mean = statistics.fmean(negative)
    if negative_mean <= 0.0:
        return None, "nonpositive_down_day_mean_volume"
    return statistics.fmean(positive) / negative_mean, None


def _resolve_membership(
    *,
    instrument_id: str,
    expected_sector_id: str,
    as_of: date,
    evidence: tuple[S5MembershipEvidence, ...],
) -> bool | None:
    active = [
        item
        for item in evidence
        if item.instrument_id == instrument_id
        and item.effective_from <= as_of
        and (item.effective_to is None or as_of <= item.effective_to)
    ]
    if not active or any(not item.pit_verified for item in active):
        return None
    sectors = {item.sector_id for item in active}
    if len(sectors) != 1:
        return None
    return next(iter(sectors)) == expected_sector_id


def _resolve_eligibility(
    *,
    instrument_id: str,
    as_of: date,
    evidence: S5EligibilityEvidence | None,
) -> bool | None:
    if evidence is None:
        return None
    if evidence.instrument_id != instrument_id:
        raise ValueError("eligibility evidence instrument_id mismatch")
    if evidence.as_of != as_of or not evidence.pit_verified:
        return None
    return evidence.eligible


def _validate_sessions(as_of: date, sessions: tuple[date, ...]) -> None:
    if not sessions:
        raise ValueError("sessions cannot be empty")
    if sessions[-1] != as_of:
        raise ValueError("sessions must end exactly at as_of")
    if any(left >= right for left, right in zip(sessions, sessions[1:], strict=False)):
        raise ValueError("sessions must be strictly increasing")


def _series_map(
    series: S5ResearchSeries,
    used_sessions: tuple[date, ...],
) -> dict[date, float]:
    allowed = set(used_sessions)
    result: dict[date, float] = {}
    for point in series.points:
        if point.trade_date not in allowed:
            continue
        if point.trade_date in result:
            raise ValueError(
                f"duplicate series date for {series.series_id}: {point.trade_date}"
            )
        result[point.trade_date] = point.value
    return result


def _positive_window(
    sessions: tuple[date, ...],
    values: dict[date, float],
    length: int,
) -> tuple[list[float] | None, str | None]:
    if len(sessions) < length:
        return None, "insufficient_history"
    return _positive_dates(sessions[-length:], values)


def _positive_dates(
    dates: tuple[date, ...] | list[date],
    values: dict[date, float],
) -> tuple[list[float] | None, str | None]:
    result: list[float] = []
    for trade_date in dates:
        value = values.get(trade_date)
        if value is None:
            return None, f"missing_value:{trade_date.isoformat()}"
        if not math.isfinite(value) or value <= 0.0:
            return None, f"invalid_price:{trade_date.isoformat()}"
        result.append(value)
    return result, None


def _materialization_fingerprint(
    *,
    as_of: date,
    sessions: tuple[date, ...],
    used_sessions: tuple[date, ...],
    benchmark: S5ResearchSeries,
    sectors: tuple[S5SectorSeriesInput, ...],
    stocks: tuple[S5StockSeriesInput, ...],
) -> str:
    session_set = set(used_sessions)

    def series_payload(series: S5ResearchSeries) -> dict[str, object]:
        points = sorted(
            (
                point.trade_date.isoformat(),
                point.value,
            )
            for point in series.points
            if point.trade_date in session_set
        )
        return {
            "series_id": series.series_id,
            "source_id": series.source_id,
            "used_date_range": (
                [points[0][0], points[-1][0]] if points else None
            ),
            "points": points,
        }

    sector_payload = [
        {
            "sector_id": item.sector_id,
            "closes": series_payload(item.closes),
        }
        for item in sectors
    ]

    stock_payload = []
    for item in stocks:
        membership = sorted(
            (
                evidence.instrument_id,
                evidence.sector_id,
                evidence.effective_from.isoformat(),
                evidence.effective_to.isoformat()
                if evidence.effective_to is not None
                else None,
                evidence.source_id,
                evidence.pit_verified,
            )
            for evidence in item.membership_evidence
            if evidence.effective_from <= as_of
        )
        eligibility = item.eligibility_evidence
        eligibility_payload: object = None
        if eligibility is not None and eligibility.as_of <= as_of:
            eligibility_payload = {
                "instrument_id": eligibility.instrument_id,
                "as_of": eligibility.as_of.isoformat(),
                "eligible": eligibility.eligible,
                "source_id": eligibility.source_id,
                "pit_verified": eligibility.pit_verified,
            }
        stock_payload.append(
            {
                "instrument_id": item.instrument_id,
                "sector_id": item.sector_id,
                "closes": series_payload(item.closes),
                "volumes": series_payload(item.volumes),
                "membership": membership,
                "eligibility": eligibility_payload,
            }
        )

    payload = {
        "schema": _MATERIALIZER_SCHEMA,
        "as_of": as_of.isoformat(),
        "sessions": [session.isoformat() for session in sessions],
        "used_sessions": [session.isoformat() for session in used_sessions],
        "window_contract": {
            "drawdown_sessions": 120,
            "new_low_endpoints": 20,
            "new_low_trailing_sessions": 60,
            "realized_vol_return_sessions": 20,
            "vol_history_endpoints": 252,
            "vol_ddof": 1,
            "return_horizon_sessions": 20,
            "ma_sessions": 20,
            "ma_slope_lag_sessions": 5,
            "volume_ratio_return_sessions": 20,
        },
        "benchmark": series_payload(benchmark),
        "sectors": sector_payload,
        "stocks": stock_payload,
    }
    return canonical_payload_fingerprint(payload)


def _append_issue(
    issues: list[S5MaterializationIssue],
    entity_type: str,
    entity_id: str,
    field: str,
    reason: str | None,
) -> None:
    if reason is None:
        return
    issues.append(S5MaterializationIssue(entity_type, entity_id, field, reason))


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
