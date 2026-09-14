"""PIT-safe feature materialization for the frozen S5-B v1 kernel."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_completion import S5BaseObservation
from quantlab.research.s5_materialization import S5ResearchSeries

_SCHEMA = "quantlab_s5b_materialization_v1"
_REQUIRED_CLOSES = 120


@dataclass(frozen=True)
class S5BaseSeriesInput:
    entity_id: str
    closes: S5ResearchSeries
    volumes: S5ResearchSeries

    def __post_init__(self) -> None:
        _require_text("entity_id", self.entity_id)
        if self.closes.series_id != self.entity_id:
            raise ValueError("close series_id must equal entity_id")
        if self.volumes.series_id != self.entity_id:
            raise ValueError("volume series_id must equal entity_id")


@dataclass(frozen=True)
class S5BaseMaterializationIssue:
    entity_id: str
    field: str
    reason: str


@dataclass(frozen=True)
class S5BaseMaterialization:
    as_of: date
    observations: tuple[S5BaseObservation, ...]
    issues: tuple[S5BaseMaterializationIssue, ...]
    fingerprint: str
    research_only: bool = True
    performance_claim: bool = False
    broker_order_authority: bool = False


def materialize_s5_base_inputs(
    *,
    as_of: date,
    sessions: tuple[date, ...],
    entities: tuple[S5BaseSeriesInput, ...],
) -> S5BaseMaterialization:
    """Materialize S5-B fields from caller-supplied series through ``as_of``."""

    _validate_sessions(as_of, sessions)
    if not entities:
        raise ValueError("entities cannot be empty")
    by_id: dict[str, S5BaseSeriesInput] = {}
    for entity in entities:
        if entity.entity_id in by_id:
            raise ValueError(f"duplicate entity_id: {entity.entity_id}")
        by_id[entity.entity_id] = entity

    issues: list[S5BaseMaterializationIssue] = []
    observations: list[S5BaseObservation] = []
    used_sessions = sessions[-_REQUIRED_CLOSES:]
    for entity_id in sorted(by_id):
        entity = by_id[entity_id]
        closes = _series_map(entity.closes, used_sessions)
        volumes = _series_map(entity.volumes, used_sessions)
        values: dict[str, object] = {}

        close_values, close_reason = _positive_values(used_sessions, closes)
        if len(sessions) < _REQUIRED_CLOSES:
            close_values, close_reason = None, "insufficient_history"
        if close_values is None:
            for field in _CLOSE_FIELDS:
                _issue(issues, entity_id, field, close_reason or "invalid_closes")
                values[field] = None
        else:
            values.update(_close_features(close_values))

        volume_dates = sessions[-20:]
        volume_values, volume_reason = _non_negative_values(volume_dates, volumes)
        if len(sessions) < 20:
            volume_values, volume_reason = None, "insufficient_history"
        if volume_values is None:
            values["volume_ratio_5_to_20"] = None
            _issue(
                issues,
                entity_id,
                "volume_ratio_5_to_20",
                volume_reason or "invalid_volumes",
            )
        else:
            denominator = statistics.fmean(volume_values)
            if denominator <= 0.0:
                values["volume_ratio_5_to_20"] = None
                _issue(issues, entity_id, "volume_ratio_5_to_20", "zero_denominator")
            else:
                values["volume_ratio_5_to_20"] = (
                    statistics.fmean(volume_values[-5:]) / denominator
                )

        volatility_ratio = values.get("volatility_ratio_10_to_prior_20")
        if volatility_ratio is None and close_values is not None:
            _issue(
                issues,
                entity_id,
                "volatility_ratio_10_to_prior_20",
                "zero_prior_volatility",
            )

        observations.append(
            S5BaseObservation(
                entity_id=entity_id,
                as_of=as_of,
                history_complete=not any(i.entity_id == entity_id for i in issues),
                **values,
            )
        )

    return S5BaseMaterialization(
        as_of=as_of,
        observations=tuple(observations),
        issues=tuple(issues),
        fingerprint=_fingerprint(as_of, sessions, used_sessions, tuple(by_id.values())),
    )


_CLOSE_FIELDS = (
    "prior_drawdown_from_120d_high",
    "recent_return_10",
    "max_drawdown_10",
    "new_low_count_10",
    "new_low_count_20",
    "max_new_low_undercut_10",
    "volatility_ratio_10_to_prior_20",
    "recovery_from_10d_low",
    "prior_low_reclaimed",
    "reclaim_sessions",
    "close_location_20",
    "close_to_ma20_ratio",
    "ma20_slope_5",
    "breakout_distance_20",
)


def _close_features(prices: list[float]) -> dict[str, object]:
    returns = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]
    prior_vol = statistics.stdev(returns[-30:-10])
    current_vol = statistics.stdev(returns[-10:])
    volatility_ratio = None if prior_vol == 0.0 else current_vol / prior_vol

    low_records: list[tuple[int, float, float]] = []
    for index in range(len(prices) - 20, len(prices)):
        prior_low = min(prices[index - 60 : index])
        if prices[index] < prior_low:
            low_records.append(
                (index, prior_low, max(0.0, 1.0 - prices[index] / prior_low))
            )
    last_ten_start = len(prices) - 10
    recent_records = [row for row in low_records if row[0] >= last_ten_start]
    if recent_records:
        low_index, reference_low, _ = recent_records[-1]
        reclaim_offsets = [
            index - low_index
            for index in range(low_index + 1, len(prices))
            if prices[index] > reference_low
        ]
        reclaimed = bool(reclaim_offsets)
        reclaim_sessions = (
            reclaim_offsets[0] if reclaimed else len(prices) - 1 - low_index
        )
    else:
        reclaimed = True
        reclaim_sessions = None

    recent_ten = prices[-10:]
    peak = recent_ten[0]
    max_drawdown = 0.0
    for price in recent_ten[1:]:
        max_drawdown = max(max_drawdown, 1.0 - price / peak)
        peak = max(peak, price)

    recent_twenty = prices[-20:]
    range_low, range_high = min(recent_twenty), max(recent_twenty)
    location = (
        0.5
        if range_high == range_low
        else (prices[-1] - range_low) / (range_high - range_low)
    )
    current_ma = statistics.fmean(prices[-20:])
    prior_ma = statistics.fmean(prices[-25:-5])
    prior_resistance = max(prices[-21:-1])

    return {
        "prior_drawdown_from_120d_high": prices[-1] / max(prices) - 1.0,
        "recent_return_10": prices[-1] / prices[-11] - 1.0,
        "max_drawdown_10": max_drawdown,
        "new_low_count_10": len(recent_records),
        "new_low_count_20": len(low_records),
        "max_new_low_undercut_10": max((r[2] for r in recent_records), default=0.0),
        "volatility_ratio_10_to_prior_20": volatility_ratio,
        "recovery_from_10d_low": prices[-1] / min(recent_ten) - 1.0,
        "prior_low_reclaimed": reclaimed,
        "reclaim_sessions": reclaim_sessions,
        "close_location_20": location,
        "close_to_ma20_ratio": prices[-1] / current_ma,
        "ma20_slope_5": current_ma / prior_ma - 1.0,
        "breakout_distance_20": prices[-1] / prior_resistance - 1.0,
    }


def _validate_sessions(as_of: date, sessions: tuple[date, ...]) -> None:
    if not sessions:
        raise ValueError("sessions cannot be empty")
    if sessions[-1] != as_of:
        raise ValueError("sessions must end exactly at as_of")
    if any(a >= b for a, b in pairwise(sessions)):
        raise ValueError("sessions must be strictly increasing")


def _series_map(
    series: S5ResearchSeries, sessions: tuple[date, ...]
) -> dict[date, float]:
    allowed = set(sessions)
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


def _positive_values(
    dates: tuple[date, ...], values: dict[date, float]
) -> tuple[list[float] | None, str | None]:
    return _values(dates, values, positive=True)


def _non_negative_values(
    dates: tuple[date, ...], values: dict[date, float]
) -> tuple[list[float] | None, str | None]:
    return _values(dates, values, positive=False)


def _values(
    dates: tuple[date, ...], values: dict[date, float], *, positive: bool
) -> tuple[list[float] | None, str | None]:
    result: list[float] = []
    for trade_date in dates:
        value = values.get(trade_date)
        if value is None:
            return None, f"missing_value:{trade_date.isoformat()}"
        if not math.isfinite(value) or (value <= 0.0 if positive else value < 0.0):
            kind = "price" if positive else "volume"
            return None, f"invalid_{kind}:{trade_date.isoformat()}"
        result.append(value)
    return result, None


def _fingerprint(
    as_of: date,
    sessions: tuple[date, ...],
    used: tuple[date, ...],
    entities: tuple[S5BaseSeriesInput, ...],
) -> str:
    allowed = set(used)

    def series_payload(series: S5ResearchSeries) -> dict[str, object]:
        return {
            "series_id": series.series_id,
            "source_id": series.source_id,
            "points": sorted(
                (p.trade_date.isoformat(), p.value)
                for p in series.points
                if p.trade_date in allowed
            ),
        }

    payload = {
        "schema": _SCHEMA,
        "as_of": as_of.isoformat(),
        "sessions": [d.isoformat() for d in sessions],
        "entities": sorted(
            (
                {
                    "entity_id": e.entity_id,
                    "closes": series_payload(e.closes),
                    "volumes": series_payload(e.volumes),
                }
                for e in entities
            ),
            key=lambda row: row["entity_id"],
        ),
    }
    return canonical_payload_fingerprint(payload)


def _issue(
    issues: list[S5BaseMaterializationIssue], entity_id: str, field: str, reason: str
) -> None:
    issues.append(S5BaseMaterializationIssue(entity_id, field, reason))


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
