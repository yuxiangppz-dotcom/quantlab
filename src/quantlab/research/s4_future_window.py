"""Pure single-window price association; real input admission needs its own later card."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.s4_pilot import adjusted_price, exact_grid

CHINA = ZoneInfo("Asia/Shanghai")
PRICE_AS_OF = date(2026, 9, 11)
LABEL_DATES = tuple(date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21))
MINIMUM_PAIRS = 30


@dataclass(frozen=True)
class S4StockOutcome:
    instrument_id: str
    valid_future_bars: int
    adjusted_price_change: float | None
    common_pair: bool


@dataclass(frozen=True)
class S4WindowComparison:
    status: str
    evaluated_at: datetime
    rows: tuple[S4StockOutcome, ...]
    labels_known: int
    common_pairs: int
    s4_rank_ic: float | None
    reference_rank_ic: float | None
    paired_ic_difference: float | None
    scope: str = field(default="one_future_price_window_only", init=False)
    source_files_certified: bool = field(default=False, init=False)
    net_return_evidence: bool = field(default=False, init=False)
    stability_or_significance_established: bool = field(default=False, init=False)
    promotion_authority: bool = field(default=False, init=False)
    execution_authority: bool = field(default=False, init=False)


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DataValidationError("future evaluation requires explicit aware timestamps")


def validate_evaluation_times(
    created_at, signal_inputs_available_at, future_sources_available_at, evaluated_at
):
    """Check chronology before inspecting future frames; timestamps do not certify files."""
    for value in (created_at, signal_inputs_available_at, evaluated_at):
        _aware(value)
    close = datetime.combine(LABEL_DATES[-1], time(15), CHINA)
    if evaluated_at < close:
        raise DataValidationError("future comparison window has not closed")
    if (
        created_at.astimezone(CHINA).date() != date(2026, 9, 13)
        or not datetime(2026, 9, 11, 16, tzinfo=CHINA) <= signal_inputs_available_at <= created_at
        or created_at >= datetime.combine(LABEL_DATES[0], time(9, 30), CHINA)
    ):
        raise DataValidationError("original weekend registration chronology changed")
    if (
        type(future_sources_available_at) is not tuple
        or any(type(x) is not tuple or len(x) != 2 for x in future_sources_available_at)
        or tuple(x[0] for x in future_sources_available_at) != LABEL_DATES
        or any(type(x[0]) is not date for x in future_sources_available_at)
    ):
        raise DataValidationError("all six exact future source availability bounds are required")
    for day, observed_at in future_sources_available_at:
        _aware(observed_at)
        if not datetime.combine(day, time(15), CHINA) <= observed_at <= evaluated_at:
            raise DataValidationError("future source predates its close or postdates evaluation")


def _scores(frame, codes):
    if (
        type(codes) is not tuple
        or len(codes) != 256
        or any(type(code) is not str or not code.strip() or code.strip() != code for code in codes)
        or tuple(sorted(set(codes))) != codes
    ):
        raise DataValidationError("fixed original ordered 256-stock cohort is required")
    g = frame.copy()
    if (
        g.instrument_id.isna().any()
        or g.instrument_id.duplicated().any()
        or len(g) != 256
        or set(g.instrument_id) != set(codes)
        or g.price_as_of.isna().any()
        or not g.price_as_of.eq(PRICE_AS_OF.isoformat()).all()
    ):
        raise DataValidationError("saved score cohort or price-as-of changed")
    g = g.set_index("instrument_id").reindex(codes)
    for value, mask in (("S4-A", "S4_A_known"), ("reference_reversal20", "reference20_known")):
        if (
            not pd.api.types.is_numeric_dtype(g[value])
            or pd.api.types.is_bool_dtype(g[value])
            or not pd.api.types.is_bool_dtype(g[mask])
            or g[mask].isna().any()
        ):
            raise DataValidationError(
                "saved scores and known masks must retain their numeric types"
            )
        values = g[value].to_numpy(dtype=float, na_value=np.nan)
        if np.isinf(values).any() or not np.array_equal(np.isfinite(values), g[mask].to_numpy()):
            raise DataValidationError("saved score known masks conflict with values")
        g[value] = values
    return g


def _rho(x, y):
    if x.nunique() < 2 or y.nunique() < 2:
        return None
    result = x.rank(method="average").corr(y.rank(method="average"))
    return float(result) if np.isfinite(result) else None


def evaluate_s4_price_window(
    scores,
    future_prices,
    codes,
    *,
    created_at,
    signal_inputs_available_at,
    future_sources_available_at,
    evaluated_at,
):
    """Evaluate the original horizon on one shared sample, without any I/O or promotion.

    The future acquisition wrapper must separately bind the original sealed
    scores, observation/proof and future raw responses. This core certifies none
    of those files, never substitutes for that later admission, and writes nothing.
    """
    validate_evaluation_times(
        created_at, signal_inputs_available_at, future_sources_available_at, evaluated_at
    )
    saved = _scores(scores, codes)
    grid = exact_grid(future_prices, LABEL_DATES, codes, allow_missing=True)
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        price = adjusted_price(grid).to_numpy().reshape(256, 6)
        complete = np.isfinite(price).all(axis=1)
        ratio = price[:, -1] / price[:, 0]
        valid = complete & np.isfinite(ratio) & (ratio > 0)
        labels = np.where(valid, ratio - 1, np.nan)
    saved["label"] = labels
    common = np.isfinite(saved[["S4-A", "reference_reversal20", "label"]]).all(axis=1)
    paired = saved[common]
    s4 = reference = difference = None
    status = "insufficient_common_pairs"
    if len(paired) >= MINIMUM_PAIRS:
        s4 = _rho(paired["S4-A"], paired.label)
        reference = _rho(paired.reference_reversal20, paired.label)
        if s4 is not None and reference is not None:
            difference, status = s4 - reference, "single_window_price_association"
        else:
            status = "constant_common_ranks"
    rows = tuple(
        S4StockOutcome(
            code,
            int(np.isfinite(p).sum()),
            float(label) if np.isfinite(label) else None,
            bool(pair),
        )
        for code, p, label, pair in zip(codes, price, labels, common, strict=True)
    )
    return S4WindowComparison(
        status,
        evaluated_at,
        rows,
        int(np.isfinite(labels).sum()),
        len(paired),
        s4,
        reference,
        difference,
    )
