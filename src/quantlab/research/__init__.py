"""Research layer: adjusted prices and returns."""

from quantlab.research.alpha158_subset import (
    ALPHA158_EXACT_SUBSET,
    alpha158_subset_rows,
    calculate_alpha158_exact_subset,
)
from quantlab.research.dataset import build_research_dataset
from quantlab.research.evaluation import (
    daily_rank_ic,
    quantile_returns,
    summarize_ic,
    summarize_quantiles,
)
from quantlab.research.forward_shadow_analytics import (
    ShadowDiagnosticSummary,
    paired_shadow_diagnostics,
    summarize_forward_shadow,
)
from quantlab.research.models import ResearchDailyPrice
from quantlab.research.price import build_prices, filter_point_in_time, prices_to_frame
from quantlab.research.returns import calculate_forward_returns, calculate_returns
from quantlab.research.universe import filter_v1_universe

__all__ = [
    "ALPHA158_EXACT_SUBSET",
    "ResearchDailyPrice",
    "ShadowDiagnosticSummary",
    "alpha158_subset_rows",
    "build_prices",
    "build_research_dataset",
    "calculate_alpha158_exact_subset",
    "calculate_forward_returns",
    "calculate_returns",
    "daily_rank_ic",
    "filter_point_in_time",
    "filter_v1_universe",
    "paired_shadow_diagnostics",
    "prices_to_frame",
    "quantile_returns",
    "summarize_forward_shadow",
    "summarize_ic",
    "summarize_quantiles",
]
