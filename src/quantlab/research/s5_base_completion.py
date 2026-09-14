"""Claim-neutral S5-B base-completion research state kernel.

The kernel classifies observable price/volume structure only.  It does not
identify investor accounts, infer institutional intent, read a provider, or
consume future returns.  S5-A remains a separate frozen strategy variant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class S5BaseState(StrEnum):
    """Frozen S5-B v1 states for one point-in-time observation."""

    UNKNOWN = "unknown"
    BASE_BUILDING = "base_building"
    BASE_READY = "base_ready"
    BREAKOUT_CONFIRMED = "breakout_confirmed"
    FAILED = "failed"


@dataclass(frozen=True)
class S5BaseConfig:
    """Frozen, outcome-unseen S5-B v1 thresholds."""

    max_prior_drawdown: float = -0.20
    max_undercut_depth_10: float = 0.03
    min_recent_return_10: float = -0.06
    max_recent_drawdown_10: float = 0.08
    max_volatility_ratio: float = 1.10
    max_new_low_count_ready: int = 3
    max_new_low_count_20_ready: int = 5
    max_reclaim_sessions: int = 3
    min_recovery_from_low: float = 0.04
    min_close_location_20: float = 0.55
    min_close_to_ma20_ratio: float = 1.0
    min_ma20_slope_5: float = 0.0
    min_breakout_distance_20: float = 0.0
    min_breakout_volume_ratio: float = 1.20

    def __post_init__(self) -> None:
        finite = (
            "max_prior_drawdown",
            "max_undercut_depth_10",
            "min_recent_return_10",
            "max_recent_drawdown_10",
            "max_volatility_ratio",
            "min_recovery_from_low",
            "min_close_location_20",
            "min_close_to_ma20_ratio",
            "min_ma20_slope_5",
            "min_breakout_distance_20",
            "min_breakout_volume_ratio",
        )
        for name in finite:
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if not -1.0 <= self.max_prior_drawdown <= 0.0:
            raise ValueError("max_prior_drawdown must be in [-1, 0]")
        if not 0.0 <= self.max_undercut_depth_10 <= 1.0:
            raise ValueError("max_undercut_depth_10 must be in [0, 1]")
        if not -1.0 <= self.min_recent_return_10 <= 0.0:
            raise ValueError("min_recent_return_10 must be in [-1, 0]")
        if not 0.0 <= self.max_recent_drawdown_10 <= 1.0:
            raise ValueError("max_recent_drawdown_10 must be in [0, 1]")
        if self.max_volatility_ratio < 0.0:
            raise ValueError("max_volatility_ratio must be non-negative")
        if not 0 <= self.max_new_low_count_ready <= 10:
            raise ValueError("max_new_low_count_ready must be in [0, 10]")
        if not 0 <= self.max_new_low_count_20_ready <= 20:
            raise ValueError("max_new_low_count_20_ready must be in [0, 20]")
        if not 0 <= self.max_reclaim_sessions <= 10:
            raise ValueError("max_reclaim_sessions must be in [0, 10]")
        if self.min_recovery_from_low < 0.0:
            raise ValueError("min_recovery_from_low must be non-negative")
        if not 0.0 <= self.min_close_location_20 <= 1.0:
            raise ValueError("min_close_location_20 must be in [0, 1]")
        if self.min_close_to_ma20_ratio <= 0.0:
            raise ValueError("min_close_to_ma20_ratio must be positive")
        if self.min_breakout_volume_ratio < 0.0:
            raise ValueError("min_breakout_volume_ratio must be non-negative")


@dataclass(frozen=True)
class S5BaseObservation:
    """Caller-materialized PIT features for one stock or sector."""

    entity_id: str
    as_of: date
    prior_drawdown_from_120d_high: float | None
    recent_return_10: float | None
    max_drawdown_10: float | None
    new_low_count_10: int | None
    new_low_count_20: int | None
    max_new_low_undercut_10: float | None
    volatility_ratio_10_to_prior_20: float | None
    recovery_from_10d_low: float | None
    prior_low_reclaimed: bool | None
    reclaim_sessions: int | None
    close_location_20: float | None
    close_to_ma20_ratio: float | None
    ma20_slope_5: float | None
    breakout_distance_20: float | None
    volume_ratio_5_to_20: float | None
    history_complete: bool | None

    def __post_init__(self) -> None:
        if not self.entity_id or not self.entity_id.strip():
            raise ValueError("entity_id must be non-empty")
        _optional_range(
            "prior_drawdown_from_120d_high",
            self.prior_drawdown_from_120d_high,
            -1.0,
            0.0,
        )
        _optional_finite("recent_return_10", self.recent_return_10)
        _optional_range("max_drawdown_10", self.max_drawdown_10, 0.0, 1.0)
        if self.new_low_count_10 is not None and (
            type(self.new_low_count_10) is not int
            or not 0 <= self.new_low_count_10 <= 10
        ):
            raise ValueError("new_low_count_10 must be an integer in [0, 10]")
        if self.new_low_count_20 is not None and (
            type(self.new_low_count_20) is not int
            or not 0 <= self.new_low_count_20 <= 20
        ):
            raise ValueError("new_low_count_20 must be an integer in [0, 20]")
        if (
            self.new_low_count_10 is not None
            and self.new_low_count_20 is not None
            and self.new_low_count_10 > self.new_low_count_20
        ):
            raise ValueError("new_low_count_10 cannot exceed new_low_count_20")
        _optional_range(
            "max_new_low_undercut_10",
            self.max_new_low_undercut_10,
            0.0,
            1.0,
        )
        _optional_non_negative(
            "volatility_ratio_10_to_prior_20",
            self.volatility_ratio_10_to_prior_20,
        )
        _optional_non_negative("recovery_from_10d_low", self.recovery_from_10d_low)
        _optional_bool("prior_low_reclaimed", self.prior_low_reclaimed)
        if self.reclaim_sessions is not None and (
            type(self.reclaim_sessions) is not int
            or not 0 <= self.reclaim_sessions <= 10
        ):
            raise ValueError("reclaim_sessions must be an integer in [0, 10]")
        _optional_range("close_location_20", self.close_location_20, 0.0, 1.0)
        _optional_positive("close_to_ma20_ratio", self.close_to_ma20_ratio)
        _optional_finite("ma20_slope_5", self.ma20_slope_5)
        _optional_finite("breakout_distance_20", self.breakout_distance_20)
        _optional_non_negative("volume_ratio_5_to_20", self.volume_ratio_5_to_20)
        _optional_bool("history_complete", self.history_complete)


@dataclass(frozen=True)
class S5BaseResult:
    entity_id: str
    as_of: date
    state: S5BaseState
    reasons: tuple[str, ...]
    research_only: bool = True
    performance_claim: bool = False
    broker_order_authority: bool = False


def evaluate_s5_base(
    observation: S5BaseObservation,
    config: S5BaseConfig | None = None,
) -> S5BaseResult:
    """Classify one observation without treating a recent low as failure."""

    cfg = config or S5BaseConfig()
    fields = (
        "prior_drawdown_from_120d_high",
        "recent_return_10",
        "max_drawdown_10",
        "new_low_count_10",
        "new_low_count_20",
        "max_new_low_undercut_10",
        "volatility_ratio_10_to_prior_20",
        "recovery_from_10d_low",
        "prior_low_reclaimed",
        "close_location_20",
        "close_to_ma20_ratio",
        "ma20_slope_5",
        "breakout_distance_20",
        "volume_ratio_5_to_20",
    )
    unknown: list[str] = []
    if observation.history_complete is not True:
        unknown.append(
            "history_unknown"
            if observation.history_complete is None
            else "history_incomplete"
        )
    unknown.extend(
        f"missing_{name}" for name in fields if getattr(observation, name) is None
    )
    if (
        observation.new_low_count_10 is not None
        and observation.new_low_count_10 > 0
        and observation.reclaim_sessions is None
    ):
        unknown.append("missing_reclaim_sessions")
    if unknown:
        return _result(observation, S5BaseState.UNKNOWN, unknown)

    prior_drawdown = _known(observation.prior_drawdown_from_120d_high)
    recent_return = _known(observation.recent_return_10)
    recent_drawdown = _known(observation.max_drawdown_10)
    new_low_count = observation.new_low_count_10
    new_low_count_20 = observation.new_low_count_20
    undercut = _known(observation.max_new_low_undercut_10)
    volatility_ratio = _known(observation.volatility_ratio_10_to_prior_20)
    recovery = _known(observation.recovery_from_10d_low)
    reclaimed = observation.prior_low_reclaimed
    reclaim_sessions = observation.reclaim_sessions
    close_location = _known(observation.close_location_20)
    close_to_ma = _known(observation.close_to_ma20_ratio)
    ma_slope = _known(observation.ma20_slope_5)
    breakout_distance = _known(observation.breakout_distance_20)
    volume_ratio = _known(observation.volume_ratio_5_to_20)
    assert new_low_count is not None and new_low_count_20 is not None
    assert reclaimed is not None

    failures: list[str] = []
    if prior_drawdown > cfg.max_prior_drawdown:
        failures.append("prior_drawdown_not_deep_enough")
    if undercut > cfg.max_undercut_depth_10:
        failures.append("new_low_undercut_too_deep")
    if recent_return < cfg.min_recent_return_10:
        failures.append("recent_decline_too_fast")
    if recent_drawdown > cfg.max_recent_drawdown_10:
        failures.append("recent_drawdown_too_large")
    if volatility_ratio > cfg.max_volatility_ratio:
        failures.append("downside_volatility_expanding")
    if failures:
        return _result(observation, S5BaseState.FAILED, failures)

    building: list[str] = []
    if new_low_count > cfg.max_new_low_count_ready:
        building.append("new_low_frequency_still_high")
    if new_low_count_20 > cfg.max_new_low_count_20_ready:
        building.append("new_low_frequency_20_still_high")
    if new_low_count > 0 and not reclaimed:
        building.append("prior_low_not_reclaimed")
    if (
        new_low_count > 0
        and reclaimed
        and reclaim_sessions is not None
        and reclaim_sessions > cfg.max_reclaim_sessions
    ):
        building.append("prior_low_reclaim_too_slow")
    if recovery < cfg.min_recovery_from_low:
        building.append("recovery_from_low_insufficient")
    if close_location < cfg.min_close_location_20:
        building.append("close_location_still_low")
    if close_to_ma < cfg.min_close_to_ma20_ratio:
        building.append("short_ma_not_reclaimed")
    if ma_slope < cfg.min_ma20_slope_5:
        building.append("short_ma_still_falling")
    if building:
        return _result(observation, S5BaseState.BASE_BUILDING, building)

    if (
        breakout_distance > cfg.min_breakout_distance_20
        and volume_ratio >= cfg.min_breakout_volume_ratio
    ):
        return _result(
            observation,
            S5BaseState.BREAKOUT_CONFIRMED,
            ("price_breakout_confirmed", "breakout_volume_confirmed"),
        )

    pending: list[str] = []
    if breakout_distance <= cfg.min_breakout_distance_20:
        pending.append("price_breakout_not_confirmed")
    if volume_ratio < cfg.min_breakout_volume_ratio:
        pending.append("breakout_volume_not_confirmed")
    return _result(observation, S5BaseState.BASE_READY, pending)


def _result(
    observation: S5BaseObservation,
    state: S5BaseState,
    reasons: list[str] | tuple[str, ...],
) -> S5BaseResult:
    return S5BaseResult(
        entity_id=observation.entity_id,
        as_of=observation.as_of,
        state=state,
        reasons=tuple(reasons),
    )


def _known(value: float | None) -> float:
    if value is None:
        raise AssertionError("validated S5-B observation unexpectedly missing")
    return value


def _optional_bool(name: str, value: bool | None) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{name} must be bool or None")


def _optional_finite(name: str, value: float | None) -> None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{name} must be finite when present")


def _optional_range(name: str, value: float | None, low: float, high: float) -> None:
    _optional_finite(name, value)
    if value is not None and not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")


def _optional_non_negative(name: str, value: float | None) -> None:
    _optional_finite(name, value)
    if value is not None and value < 0.0:
        raise ValueError(f"{name} must be non-negative when present")


def _optional_positive(name: str, value: float | None) -> None:
    _optional_finite(name, value)
    if value is not None and value <= 0.0:
        raise ValueError(f"{name} must be positive when present")
