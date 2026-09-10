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
from quantlab.research.evidence_catalog import (
    EvidenceCatalog,
    EvidenceCatalogEntry,
    EvidenceCatalogIssue,
    build_evidence_catalog,
    evidence_by_id,
    summarize_evidence_catalog,
)
from quantlab.research.forward_shadow_analytics import (
    ShadowDiagnosticSummary,
    paired_shadow_diagnostics,
    summarize_forward_shadow,
)
from quantlab.research.models import ResearchDailyPrice
from quantlab.research.price import build_prices, filter_point_in_time, prices_to_frame
from quantlab.research.returns import calculate_forward_returns, calculate_returns
from quantlab.research.strategy_forward_binding import (
    StrategyForwardBinding,
    StrategyForwardBindingAudit,
    validate_strategy_forward_binding,
)
from quantlab.research.strategy_readiness import (
    StrategyReadinessReport,
    build_registered_strategy_readiness,
    build_strategy_readiness,
    readiness_summary,
)
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
    FORWARD_EVIDENCE_ACCUMULATING,
    FORWARD_SHADOW,
    IDEA,
    PORTFOLIO_AUDITED,
    REJECTED,
    RESEARCHED,
    USER_APPROVED,
    StrategyRegistryEntry,
    load_strategy_registry,
    transition_strategy,
)
from quantlab.research.universe import filter_v1_universe

__all__ = [
    "ALPHA158_EXACT_SUBSET",
    "ELIGIBLE_FOR_USER_REVIEW",
    "EvidenceCatalog",
    "EvidenceCatalogEntry",
    "EvidenceCatalogIssue",
    "FORWARD_EVIDENCE_ACCUMULATING",
    "FORWARD_SHADOW",
    "IDEA",
    "PORTFOLIO_AUDITED",
    "REJECTED",
    "RESEARCHED",
    "ResearchDailyPrice",
    "ShadowDiagnosticSummary",
    "StrategyForwardBinding",
    "StrategyForwardBindingAudit",
    "StrategyReadinessReport",
    "StrategyRegistryEntry",
    "USER_APPROVED",
    "alpha158_subset_rows",
    "build_evidence_catalog",
    "build_prices",
    "build_registered_strategy_readiness",
    "build_research_dataset",
    "build_strategy_readiness",
    "calculate_alpha158_exact_subset",
    "calculate_forward_returns",
    "calculate_returns",
    "daily_rank_ic",
    "evidence_by_id",
    "filter_point_in_time",
    "filter_v1_universe",
    "load_strategy_registry",
    "paired_shadow_diagnostics",
    "prices_to_frame",
    "quantile_returns",
    "readiness_summary",
    "summarize_evidence_catalog",
    "summarize_forward_shadow",
    "summarize_ic",
    "summarize_quantiles",
    "transition_strategy",
    "validate_strategy_forward_binding",
]
