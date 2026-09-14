"""S5 sector-bottom / accumulation-like reversal research kernel.

The kernel is deliberately data-source agnostic.  Callers must materialize every
feature with point-in-time evidence before constructing these observations.  The
word "accumulation" describes an observable price/volume pattern only; it does
not identify investor accounts or prove institutional buying.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from quantlab.portfolio.models import TargetPortfolio, TargetWeight


class S5State(StrEnum):
    """Research eligibility state for one S5 observation."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class S5Config:
    """Frozen v1 thresholds and portfolio sizing contract."""

    max_sector_drawdown: float = -0.25
    max_new_low_rate_20: float = 0.10
    max_volatility_percentile_252: float = 0.40
    min_sector_relative_return_20: float = 0.0
    min_stock_relative_return_20: float = 0.0
    min_close_to_ma20_ratio: float = 1.0
    min_ma20_slope_5: float = 0.0
    min_up_down_volume_ratio_20: float = 1.0
    max_names: int = 20
    weight_per_name: float = 0.04

    def __post_init__(self) -> None:
        numeric = {
            "max_sector_drawdown": self.max_sector_drawdown,
            "max_new_low_rate_20": self.max_new_low_rate_20,
            "max_volatility_percentile_252": self.max_volatility_percentile_252,
            "min_sector_relative_return_20": self.min_sector_relative_return_20,
            "min_stock_relative_return_20": self.min_stock_relative_return_20,
            "min_close_to_ma20_ratio": self.min_close_to_ma20_ratio,
            "min_ma20_slope_5": self.min_ma20_slope_5,
            "min_up_down_volume_ratio_20": self.min_up_down_volume_ratio_20,
            "weight_per_name": self.weight_per_name,
        }
        for name, value in numeric.items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not -1.0 <= self.max_sector_drawdown <= 0.0:
            raise ValueError("max_sector_drawdown must be in [-1, 0]")
        if not 0.0 <= self.max_new_low_rate_20 <= 1.0:
            raise ValueError("max_new_low_rate_20 must be in [0, 1]")
        if not 0.0 <= self.max_volatility_percentile_252 <= 1.0:
            raise ValueError("max_volatility_percentile_252 must be in [0, 1]")
        if self.min_close_to_ma20_ratio <= 0.0:
            raise ValueError("min_close_to_ma20_ratio must be positive")
        if self.min_up_down_volume_ratio_20 < 0.0:
            raise ValueError("min_up_down_volume_ratio_20 must be non-negative")
        if self.max_names <= 0:
            raise ValueError("max_names must be positive")
        if not 0.0 < self.weight_per_name <= 1.0:
            raise ValueError("weight_per_name must be in (0, 1]")
        if self.max_names * self.weight_per_name > 1.0 + 1e-12:
            raise ValueError("max_names * weight_per_name cannot exceed 1")


@dataclass(frozen=True)
class S5SectorObservation:
    """One sector's precomputed, PIT-bound S5 features for a decision date."""

    sector_id: str
    as_of: date
    drawdown_from_120d_high: float | None
    new_low_rate_20: float | None
    volatility_percentile_252: float | None
    relative_return_20: float | None
    history_complete: bool | None

    def __post_init__(self) -> None:
        _require_id("sector_id", self.sector_id)
        _validate_optional_range(
            "drawdown_from_120d_high", self.drawdown_from_120d_high, -1.0, 0.0
        )
        _validate_optional_range("new_low_rate_20", self.new_low_rate_20, 0.0, 1.0)
        _validate_optional_range(
            "volatility_percentile_252", self.volatility_percentile_252, 0.0, 1.0
        )
        _validate_optional_finite("relative_return_20", self.relative_return_20)
        _validate_optional_bool("history_complete", self.history_complete)


@dataclass(frozen=True)
class S5StockObservation:
    """One stock's precomputed, PIT-bound S5 features for a decision date."""

    instrument_id: str
    sector_id: str
    as_of: date
    membership_verified: bool | None
    relative_return_20_vs_sector: float | None
    close_to_ma20_ratio: float | None
    ma20_slope_5: float | None
    up_down_volume_ratio_20: float | None
    research_eligible: bool | None

    def __post_init__(self) -> None:
        _require_id("instrument_id", self.instrument_id)
        _require_id("sector_id", self.sector_id)
        _validate_optional_bool("membership_verified", self.membership_verified)
        _validate_optional_finite(
            "relative_return_20_vs_sector", self.relative_return_20_vs_sector
        )
        _validate_optional_positive("close_to_ma20_ratio", self.close_to_ma20_ratio)
        _validate_optional_finite("ma20_slope_5", self.ma20_slope_5)
        _validate_optional_non_negative(
            "up_down_volume_ratio_20", self.up_down_volume_ratio_20
        )
        _validate_optional_bool("research_eligible", self.research_eligible)


@dataclass(frozen=True)
class S5SectorResult:
    sector_id: str
    state: S5State
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class S5StockResult:
    instrument_id: str
    sector_id: str
    state: S5State
    reasons: tuple[str, ...]
    rank: int | None = None
    selected: bool = False


@dataclass(frozen=True)
class S5Decision:
    """Claim-neutral S5 research decision and dynamic-N target."""

    as_of: date
    sector_results: tuple[S5SectorResult, ...]
    stock_results: tuple[S5StockResult, ...]
    selected_ids: tuple[str, ...]
    target: TargetPortfolio
    research_only: bool = True
    performance_claim: bool = False
    broker_order_authority: bool = False


def evaluate_s5_sector(
    observation: S5SectorObservation,
    config: S5Config | None = None,
) -> S5SectorResult:
    """Evaluate one sector without reading data or future outcomes."""

    cfg = config or S5Config()
    missing = tuple(
        name
        for name in (
            "drawdown_from_120d_high",
            "new_low_rate_20",
            "volatility_percentile_252",
            "relative_return_20",
        )
        if getattr(observation, name) is None
    )
    if observation.history_complete is not True or missing:
        reasons: list[str] = []
        if observation.history_complete is not True:
            reasons.append(
                "sector_history_unknown"
                if observation.history_complete is None
                else "sector_history_incomplete"
            )
        reasons.extend(f"missing_{name}" for name in missing)
        return S5SectorResult(observation.sector_id, S5State.UNKNOWN, tuple(reasons))

    assert observation.drawdown_from_120d_high is not None
    assert observation.new_low_rate_20 is not None
    assert observation.volatility_percentile_252 is not None
    assert observation.relative_return_20 is not None

    failures: list[str] = []
    if observation.drawdown_from_120d_high > cfg.max_sector_drawdown:
        failures.append("drawdown_not_deep_enough")
    if observation.new_low_rate_20 > cfg.max_new_low_rate_20:
        failures.append("still_making_too_many_new_lows")
    if observation.volatility_percentile_252 > cfg.max_volatility_percentile_252:
        failures.append("volatility_not_compressed")
    if observation.relative_return_20 <= cfg.min_sector_relative_return_20:
        failures.append("sector_relative_strength_not_positive")

    return S5SectorResult(
        observation.sector_id,
        S5State.INELIGIBLE if failures else S5State.ELIGIBLE,
        tuple(failures),
    )


def evaluate_s5_stock(
    observation: S5StockObservation,
    sector_result: S5SectorResult | None,
    config: S5Config | None = None,
) -> S5StockResult:
    """Evaluate one stock after the sector gate."""

    cfg = config or S5Config()
    if sector_result is None:
        return S5StockResult(
            observation.instrument_id,
            observation.sector_id,
            S5State.UNKNOWN,
            ("sector_observation_missing",),
        )
    if sector_result.sector_id != observation.sector_id:
        raise ValueError("sector_result does not match stock sector_id")
    if sector_result.state is S5State.UNKNOWN:
        return S5StockResult(
            observation.instrument_id,
            observation.sector_id,
            S5State.UNKNOWN,
            ("sector_state_unknown",),
        )
    if sector_result.state is S5State.INELIGIBLE:
        return S5StockResult(
            observation.instrument_id,
            observation.sector_id,
            S5State.INELIGIBLE,
            ("sector_ineligible",),
        )

    missing = tuple(
        name
        for name in (
            "relative_return_20_vs_sector",
            "close_to_ma20_ratio",
            "ma20_slope_5",
            "up_down_volume_ratio_20",
        )
        if getattr(observation, name) is None
    )
    unknown_reasons: list[str] = []
    if observation.membership_verified is None:
        unknown_reasons.append("sector_membership_unknown")
    if observation.research_eligible is None:
        unknown_reasons.append("research_eligibility_unknown")
    unknown_reasons.extend(f"missing_{name}" for name in missing)
    if unknown_reasons:
        return S5StockResult(
            observation.instrument_id,
            observation.sector_id,
            S5State.UNKNOWN,
            tuple(unknown_reasons),
        )

    failures: list[str] = []
    if observation.membership_verified is not True:
        failures.append("sector_membership_not_verified")
    if observation.research_eligible is not True:
        failures.append("research_ineligible")

    assert observation.relative_return_20_vs_sector is not None
    assert observation.close_to_ma20_ratio is not None
    assert observation.ma20_slope_5 is not None
    assert observation.up_down_volume_ratio_20 is not None

    if observation.relative_return_20_vs_sector <= cfg.min_stock_relative_return_20:
        failures.append("stock_relative_strength_not_positive")
    if observation.close_to_ma20_ratio <= cfg.min_close_to_ma20_ratio:
        failures.append("stock_not_above_ma20")
    if observation.ma20_slope_5 <= cfg.min_ma20_slope_5:
        failures.append("ma20_slope_not_positive")
    if observation.up_down_volume_ratio_20 < cfg.min_up_down_volume_ratio_20:
        failures.append("up_down_volume_ratio_too_low")

    return S5StockResult(
        observation.instrument_id,
        observation.sector_id,
        S5State.INELIGIBLE if failures else S5State.ELIGIBLE,
        tuple(failures),
    )


def build_s5_decision(
    *,
    as_of: date,
    sectors: tuple[S5SectorObservation, ...],
    stocks: tuple[S5StockObservation, ...],
    config: S5Config | None = None,
) -> S5Decision:
    """Build a deterministic dynamic-N S5 target with residual cash."""

    cfg = config or S5Config()
    sector_by_id: dict[str, S5SectorObservation] = {}
    for sector in sectors:
        if sector.as_of != as_of:
            raise ValueError("sector observation as_of must equal decision as_of")
        if sector.sector_id in sector_by_id:
            raise ValueError(f"duplicate sector_id: {sector.sector_id}")
        sector_by_id[sector.sector_id] = sector

    stock_by_id: dict[str, S5StockObservation] = {}
    for stock in stocks:
        if stock.as_of != as_of:
            raise ValueError("stock observation as_of must equal decision as_of")
        if stock.instrument_id in stock_by_id:
            raise ValueError(f"duplicate instrument_id: {stock.instrument_id}")
        stock_by_id[stock.instrument_id] = stock

    sector_results = tuple(
        evaluate_s5_sector(sector_by_id[sector_id], cfg)
        for sector_id in sorted(sector_by_id)
    )
    sector_result_by_id = {result.sector_id: result for result in sector_results}

    raw_stock_results = {
        instrument_id: evaluate_s5_stock(
            stock,
            sector_result_by_id.get(stock.sector_id),
            cfg,
        )
        for instrument_id, stock in stock_by_id.items()
    }
    eligible = [
        stock_by_id[instrument_id]
        for instrument_id, result in raw_stock_results.items()
        if result.state is S5State.ELIGIBLE
    ]
    eligible.sort(
        key=lambda item: (
            -_known(item.relative_return_20_vs_sector),
            -_known(item.up_down_volume_ratio_20),
            item.instrument_id,
        )
    )
    ranked_ids = [stock.instrument_id for stock in eligible]
    selected_ids = tuple(ranked_ids[: cfg.max_names])
    selected_set = set(selected_ids)
    rank_by_id = {instrument_id: rank for rank, instrument_id in enumerate(ranked_ids, 1)}

    stock_results = tuple(
        replace(
            raw_stock_results[instrument_id],
            rank=rank_by_id.get(instrument_id),
            selected=instrument_id in selected_set,
        )
        for instrument_id in sorted(raw_stock_results)
    )

    positions = tuple(
        TargetWeight(instrument_id=instrument_id, target_weight=cfg.weight_per_name)
        for instrument_id in selected_ids
    )
    cash_weight = 1.0 - cfg.weight_per_name * len(positions)
    target = TargetPortfolio(as_of=as_of, positions=positions, cash_weight=cash_weight)

    return S5Decision(
        as_of=as_of,
        sector_results=sector_results,
        stock_results=stock_results,
        selected_ids=selected_ids,
        target=target,
    )


def _known(value: float | None) -> float:
    if value is None:
        raise AssertionError("eligible S5 observations cannot contain missing ranking values")
    return value


def _require_id(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _validate_optional_bool(name: str, value: bool | None) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{name} must be bool or None")


def _validate_optional_finite(name: str, value: float | None) -> None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{name} must be finite when present")


def _validate_optional_range(
    name: str,
    value: float | None,
    minimum: float,
    maximum: float,
) -> None:
    _validate_optional_finite(name, value)
    if value is not None and not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")


def _validate_optional_positive(name: str, value: float | None) -> None:
    _validate_optional_finite(name, value)
    if value is not None and value <= 0.0:
        raise ValueError(f"{name} must be positive when present")


def _validate_optional_non_negative(name: str, value: float | None) -> None:
    _validate_optional_finite(name, value)
    if value is not None and value < 0.0:
        raise ValueError(f"{name} must be non-negative when present")
