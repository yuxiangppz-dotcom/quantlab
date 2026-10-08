"""Frozen initial research parameters; none are validated profitability claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# Binary arithmetic equality only, never a relaxation of prices or eligibility.
TECHNICAL_RATIO_ABS_TOLERANCE = 1e-12


@dataclass(frozen=True)
class ArticleConfig:
    version: str = "article_shortline_v1_20261008"
    parameter_status: str = "unvalidated_initial_research_parameters"
    history_sessions: int = 120
    listing_sessions: int = 120
    tick: float = 0.01
    median_amount_min: float = 100_000_000.0
    market_coverage_min: float = 0.95
    group_coverage_min: float = 0.90
    group_members_min: int = 10
    risk_adv_max: float = 0.25
    risk_median_ret_max: float = -0.015
    risk_down_limit_min: float = 0.01
    risk_history_adv_max: float = 0.35
    risk_history_days: int = 2
    active_adv_min: float = 0.55
    active_history_adv_min: float = 0.50
    active_history_days: int = 2
    persistence_percentile_min: float = 0.80
    persistence_days_min: int = 3
    established_breadth_min: float = 0.40
    emerging_breadth_min: float = 0.65
    emerging_share_expansion_min: float = 1.20
    emerging_limit_density_multiple: float = 2.0
    emerging_limit_count_min: int = 2
    group_pause_breadth_max: float = 0.30
    role_active_amount_min: float = 1.20
    box_width_max: float = 0.15
    breakout_min: float = 0.001
    breakout_max: float = 0.03
    amount_ratio_min: float = 1.20
    amount_ratio_max: float = 2.50
    breakout_amount_sort_target: float = 1.60
    close_location_min: float = 0.65
    upper_shadow_max: float = 0.25
    ret5_max: float = 0.12
    ma20_distance_max: float = 0.10
    peak_distance_min: int = 3
    peak_distance_max: int = 9
    rise_min: float = 0.08
    rise_max: float = 0.30
    pullback_min: float = 0.03
    pullback_max: float = 0.10
    pullback_amount_ratio_max: float = 0.75
    pullback_ma20_floor: float = 0.98
    confirmation_amount_multiple: float = 1.10
    prior_peak_max_multiple: float = 1.02
    context_limit: int = 160
    deep_limit: int = 24
    routes: tuple[str, ...] = ("base_breakout", "pullback_recovery")

    def __post_init__(self) -> None:
        if self.history_sessions < 120 or self.listing_sessions < 120:
            raise ValueError("Article strategy requires at least 120 trading sessions")
        if self.tick <= 0 or self.deep_limit < 0 or self.context_limit < self.deep_limit:
            raise ValueError("Invalid price tick or research budget")


DEFAULT_CONFIG = ArticleConfig()


def config_dict(config: ArticleConfig = DEFAULT_CONFIG) -> dict:
    return asdict(config)


def config_hash(config: ArticleConfig = DEFAULT_CONFIG) -> str:
    payload = json.dumps(
        config_dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
