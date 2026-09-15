"""Strict S5-B target adapter for the hypothetical risk-aware ledger.

The adapter does not invent execution evidence, fees, risk facts or fills. It
validates the completed base-breakout signal materialization, preserves each
target until the next signal date, and delegates execution to the existing
risk-ledger loop.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.base_breakout_strategy import STRATEGY_ID
from quantlab.research.market_risk import IndexClose, UnscaledReturn
from quantlab.research.risk_ledger_loop import (
    LedgerSessionEvidence,
    RiskLedgerCheckpoint,
    RiskLedgerConfig,
    RiskLedgerLoopResult,
    run_risk_ledger_loop,
)

_WEIGHT_TOLERANCE = 1e-9
_REQUIRED_COLUMNS = {
    "instrument_id",
    "trade_date",
    "eligible",
    "selected",
    "target_weight",
    "cash_weight",
    "strategy_id",
    "research_only",
    "performance_claim",
    "broker_order_authority",
}


@dataclass(frozen=True)
class BaseBreakoutRiskLedgerRun:
    """Bound S5-B result; still an uncertified hypothetical scenario."""

    targets: tuple[TargetPortfolio, ...]
    ledger: RiskLedgerLoopResult
    strategy_id: str = field(default=STRATEGY_ID, init=False)
    scope: str = field(default="s5b_hypothetical_risk_ledger_scenario_only", init=False)
    historical_data_certified: bool = field(default=False, init=False)
    performance_eligible: bool = field(default=False, init=False)
    performance_claim: bool = field(default=False, init=False)
    broker_order_authority: bool = field(default=False, init=False)

    @property
    def status(self) -> str:
        return self.ledger.status

    @property
    def valid_through(self) -> date:
        return self.ledger.valid_through


def materialize_base_breakout_targets(
    signals: pd.DataFrame,
    decision_sessions: tuple[date, ...],
) -> dict[date, TargetPortfolio]:
    """Validate S5-B rows and carry each target until the next signal date.

    Carrying a target preserves signal cadence; it is not a holding lock. The
    risk layer still reevaluates its cap daily, and a new strategy target may
    request an exit on the next decision session.
    """

    missing = _REQUIRED_COLUMNS - set(signals.columns)
    if missing:
        raise DataValidationError(f"base breakout target input missing columns: {sorted(missing)}")
    if signals.empty:
        raise DataValidationError("base breakout target input cannot be empty")
    if not decision_sessions or any(left >= right for left, right in pairwise(decision_sessions)):
        raise ValueError("decision_sessions must be nonempty and strictly increasing")

    frame = signals[list(_REQUIRED_COLUMNS)].copy()
    parsed_dates = pd.to_datetime(frame["trade_date"], errors="raise")
    if parsed_dates.dt.tz is not None or not parsed_dates.dt.normalize().eq(parsed_dates).all():
        raise DataValidationError("trade_date must contain timezone-naive calendar dates")
    frame["trade_date"] = parsed_dates.dt.date
    if frame.duplicated(["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicate base breakout target keys")
    invalid_ids = frame["instrument_id"].isna() | (
        frame["instrument_id"].astype(str).str.strip().eq("")
    )
    if invalid_ids.any():
        raise DataValidationError("instrument_id must be nonempty")
    if not frame["strategy_id"].eq(STRATEGY_ID).all():
        raise DataValidationError("strategy_id does not identify the frozen S5-B strategy")
    for column, expected in (
        ("research_only", True),
        ("performance_claim", False),
        ("broker_order_authority", False),
    ):
        if frame[column].isna().any() or not frame[column].eq(expected).all():
            raise DataValidationError(f"{column} violates the S5-B authority boundary")
    for column in ("eligible", "selected"):
        if frame[column].isna().any() or not pd.api.types.is_bool_dtype(frame[column]):
            raise DataValidationError(f"{column} must contain explicit booleans")

    frame["target_weight"] = pd.to_numeric(frame["target_weight"], errors="coerce")
    frame["cash_weight"] = pd.to_numeric(frame["cash_weight"], errors="coerce")
    numeric = frame[["target_weight", "cash_weight"]].to_numpy(dtype=float)
    if not all(math.isfinite(value) for value in numeric.ravel()):
        raise DataValidationError("target and cash weights must be finite")
    if (frame["target_weight"] < 0).any() or (frame["cash_weight"] < 0).any():
        raise DataValidationError("S5-B is long-only and cannot have negative weights")
    if (frame["selected"] & ~frame["eligible"]).any():
        raise DataValidationError("an ineligible S5-B row cannot be selected")
    inconsistent = (frame["selected"] & frame["target_weight"].le(0)) | (
        ~frame["selected"] & frame["target_weight"].ne(0)
    )
    if inconsistent.any():
        raise DataValidationError("selected flags and target weights disagree")

    future = sorted(value for value in set(frame["trade_date"]) if value > decision_sessions[-1])
    if future:
        raise DataValidationError(f"signal dates exceed the requested decision interval: {future}")

    observed: dict[date, TargetPortfolio] = {}
    for signal_date, rows in frame.groupby("trade_date", sort=True):
        cash_values = rows["cash_weight"].unique()
        if len(cash_values) != 1:
            raise DataValidationError(f"cash weight is inconsistent on {signal_date}")
        cash_weight = float(cash_values[0])
        selected = rows.loc[rows["selected"]].sort_values("instrument_id", kind="stable")
        positions = tuple(
            TargetWeight(str(row.instrument_id), float(row.target_weight))
            for row in selected.itertuples(index=False)
        )
        total = sum(item.target_weight for item in positions)
        if abs(total + cash_weight - 1.0) > _WEIGHT_TOLERANCE:
            raise DataValidationError(f"target budget does not sum to one on {signal_date}")
        observed[signal_date] = TargetPortfolio(signal_date, positions, cash_weight)

    targets: dict[date, TargetPortfolio] = {}
    current = next(
        (
            observed[value]
            for value in sorted(observed, reverse=True)
            if value <= decision_sessions[0]
        ),
        None,
    )
    for session in decision_sessions:
        if session in observed:
            current = observed[session]
        if current is None:
            raise DataValidationError("first decision session has no S5-B target")
        targets[session] = TargetPortfolio(session, current.positions, current.cash_weight)
    return targets


def run_base_breakout_risk_ledger(
    *,
    signals: pd.DataFrame,
    checkpoint: RiskLedgerCheckpoint,
    calendar: tuple[date, ...],
    requested_end: date,
    evidence: Mapping[date, LedgerSessionEvidence],
    config: RiskLedgerConfig,
    unscaled_risk_returns: tuple[UnscaledReturn, ...] = (),
    index_closes: tuple[IndexClose, ...] = (),
) -> BaseBreakoutRiskLedgerRun:
    """Run validated S5-B targets through the existing risk-aware ledger."""

    if checkpoint.book.asof_date not in calendar or requested_end not in calendar:
        raise ValueError("checkpoint/end dates must lie in the supplied calendar")
    first = calendar.index(checkpoint.book.asof_date) + 1
    last = calendar.index(requested_end)
    if first > last:
        raise ValueError("requested_end must follow the checkpoint")
    targets = materialize_base_breakout_targets(signals, calendar[first : last + 1])
    signal_dates = set(pd.to_datetime(signals["trade_date"], errors="raise").dt.date)
    outside_calendar = sorted(signal_dates - set(calendar))
    if outside_calendar:
        raise DataValidationError(f"signal dates are absent from the supplied calendar: {outside_calendar}")
    ledger = run_risk_ledger_loop(
        checkpoint=checkpoint,
        calendar=calendar,
        requested_end=requested_end,
        base_targets=targets,
        evidence=evidence,
        config=config,
        unscaled_risk_returns=unscaled_risk_returns,
        index_closes=index_closes,
    )
    return BaseBreakoutRiskLedgerRun(tuple(targets.values()), ledger)
