"""Research layer: adjusted prices and returns."""

from quantlab.research.dataset import build_research_dataset
from quantlab.research.evaluation import (
    daily_rank_ic,
    quantile_returns,
    summarize_ic,
    summarize_quantiles,
)
from quantlab.research.models import ResearchDailyPrice
from quantlab.research.price import build_prices, filter_point_in_time, prices_to_frame
from quantlab.research.returns import calculate_forward_returns, calculate_returns
from quantlab.research.universe import filter_v1_universe

__all__ = [
    "ResearchDailyPrice",
    "build_prices",
    "build_research_dataset",
    "calculate_forward_returns",
    "calculate_returns",
    "daily_rank_ic",
    "filter_point_in_time",
    "filter_v1_universe",
    "prices_to_frame",
    "quantile_returns",
    "summarize_ic",
    "summarize_quantiles",
]
