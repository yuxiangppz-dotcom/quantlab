"""Point-in-time market risk cap rules; no data access, execution or guarantees.

Every price, return and NAV value is a caller-supplied observation with its own
provenance label. This layer only turns those observations into target exposure
caps for the seven user-approved rules; it never certifies inputs, rebuilds a
historical portfolio, submits orders or promises any drawdown outcome. Unknown
inputs stay unknown: missing data is never treated as cap zero or as risk
cleared. Targets derived from these decisions are intentions — actual
limit-down, suspension and T+1 constraints remain the execution ledger's
authority, so realized exposure may stay above a cap.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

MARKET_RISK_LAYER_VERSION = "v1"

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"

CONSTANT_CAP_80_ID = "C80"
CONSTANT_CAP_50_ID = "C50"
VOL_TARGET_ID = "V"
INDEX_MA_REGIME_ID = "M"
DRAWDOWN_GOVERNOR_ID = "D"
COMPOSITE_VM_ID = "VM"
COMPOSITE_VMD_ID = "VMD"
RULE_IDS = (
    CONSTANT_CAP_80_ID,
    CONSTANT_CAP_50_ID,
    VOL_TARGET_ID,
    INDEX_MA_REGIME_ID,
    DRAWDOWN_GOVERNOR_ID,
    COMPOSITE_VM_ID,
    COMPOSITE_VMD_ID,
)

# Frozen V parameters: 60-session sample stdev (ddof=1), annualized over 252
# sessions against a 15% target, capped at 0.8. Returns must come from the
# portfolio before this layer's own scaling; the source label records the claim.
VOL_WINDOW = 60
VOL_ANNUALIZATION_DAYS = 252
VOL_TARGET_ANNUAL = Decimal("0.15")
VOL_CAP = Decimal("0.8")

# Frozen M parameters: CSI All Share 000985 is the only approved A-share equity
# proxy. The 200-session mean includes the decision session itself; the close
# must be strictly above it. Equality or below means cap zero.
APPROVED_EQUITY_PROXY_INDEX_ID = "000985"
MA_WINDOW = 200
MA_CAP_ON = Decimal("0.8")
MA_CAP_OFF = Decimal("0.0")

# Frozen D parameters. "Reach" is >=, "within" is <=. Deterioration may jump
# tiers; recovery moves at most one tier per decision. The high-water mark
# never decreases and is never reset by empty positions, restarts or years.
DRAWDOWN_BASE_CAP = Decimal("0.8")
COEFFICIENT_NORMAL = Decimal("1")
COEFFICIENT_TIER1 = Decimal("0.5")
COEFFICIENT_TIER2 = Decimal("0.25")
DRAWDOWN_TIER2_ENTER = Decimal("0.15")
DRAWDOWN_TIER1_ENTER = Decimal("0.10")
DRAWDOWN_TIER2_RECOVER = Decimal("0.12")
DRAWDOWN_TIER1_RECOVER = Decimal("0.08")
VALID_COEFFICIENTS = (COEFFICIENT_NORMAL, COEFFICIENT_TIER1, COEFFICIENT_TIER2)


def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


RULE_CONFIG_FINGERPRINTS: dict[str, str] = {
    CONSTANT_CAP_80_ID: _fingerprint(
        {"rule": CONSTANT_CAP_80_ID, "version": MARKET_RISK_LAYER_VERSION, "cap": "0.8"}
    ),
    CONSTANT_CAP_50_ID: _fingerprint(
        {"rule": CONSTANT_CAP_50_ID, "version": MARKET_RISK_LAYER_VERSION, "cap": "0.5"}
    ),
    VOL_TARGET_ID: _fingerprint(
        {
            "rule": VOL_TARGET_ID,
            "version": MARKET_RISK_LAYER_VERSION,
            "window": VOL_WINDOW,
            "ddof": 1,
            "annualization_days": VOL_ANNUALIZATION_DAYS,
            "target_annual": str(VOL_TARGET_ANNUAL),
            "cap": str(VOL_CAP),
        }
    ),
    INDEX_MA_REGIME_ID: _fingerprint(
        {
            "rule": INDEX_MA_REGIME_ID,
            "version": MARKET_RISK_LAYER_VERSION,
            "index_id": APPROVED_EQUITY_PROXY_INDEX_ID,
            "window": MA_WINDOW,
            "cap_on": str(MA_CAP_ON),
            "cap_off": str(MA_CAP_OFF),
            "comparison": "strictly_above_inclusive_window_mean",
        }
    ),
    DRAWDOWN_GOVERNOR_ID: _fingerprint(
        {
            "rule": DRAWDOWN_GOVERNOR_ID,
            "version": MARKET_RISK_LAYER_VERSION,
            "base_cap": str(DRAWDOWN_BASE_CAP),
            "tier2_enter_ge": str(DRAWDOWN_TIER2_ENTER),
            "tier1_enter_ge": str(DRAWDOWN_TIER1_ENTER),
            "tier2_recover_le": str(DRAWDOWN_TIER2_RECOVER),
            "tier1_recover_le": str(DRAWDOWN_TIER1_RECOVER),
            "recovery": "at_most_one_tier_per_decision",
        }
    ),
    COMPOSITE_VM_ID: _fingerprint(
        {
            "rule": COMPOSITE_VM_ID,
            "version": MARKET_RISK_LAYER_VERSION,
            "components": [VOL_TARGET_ID, INDEX_MA_REGIME_ID],
            "combination": "min",
        }
    ),
    COMPOSITE_VMD_ID: _fingerprint(
        {
            "rule": COMPOSITE_VMD_ID,
            "version": MARKET_RISK_LAYER_VERSION,
            "components": [VOL_TARGET_ID, INDEX_MA_REGIME_ID, DRAWDOWN_GOVERNOR_ID],
            "combination": "min",
        }
    ),
}


def _day(value: object, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")


def _identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty identifier")


def _decimal_positive(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal")
    return value


@dataclass(frozen=True)
class UnscaledReturn:
    """One portfolio risk return observation before this layer's own scaling.

    Non-finite values are representable so evaluation can report them as
    explicit unknowns instead of hiding them behind construction failures.
    """

    session: date
    value: float

    def __post_init__(self) -> None:
        _day(self.session, "return session")
        if type(self.value) is not float:
            raise ValueError("return value must be a float")


@dataclass(frozen=True)
class IndexClose:
    """One approved-index close observation. Positivity is checked at use."""

    session: date
    close: Decimal

    def __post_init__(self) -> None:
        _day(self.session, "index close session")
        if not isinstance(self.close, Decimal) or not self.close.is_finite():
            raise ValueError("index close must be a finite Decimal")


@dataclass(frozen=True)
class StrategyNav:
    """Fee-inclusive auditable strategy NAV as of one session.

    A NAV carrying unhandled external cash flows is refused: this layer does
    not model subscriptions/redemptions in its first phase.
    """

    as_of: date
    nav: Decimal
    unhandled_external_flow: bool = False

    def __post_init__(self) -> None:
        _day(self.as_of, "nav as_of")
        _decimal_positive(self.nav, "nav")
        if type(self.unhandled_external_flow) is not bool:
            raise ValueError("unhandled_external_flow must be boolean")


@dataclass(frozen=True)
class RiskState:
    """Persistent drawdown-governor state the caller must carry forward.

    Carries the NAV-series identity, the last decision date it already
    advanced, and a contract version. First use must go through
    :meth:`initial`; a running decision that finds no state reports unknown
    instead of silently restarting, and a state that already advanced on the
    decision date rejects so same-day retries cannot stack recoveries.
    """

    high_water_mark: Decimal
    coefficient: Decimal
    series_id: str
    last_decision_date: date | None = None
    version: str = MARKET_RISK_LAYER_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.high_water_mark) is not Decimal
            or not self.high_water_mark.is_finite()
            or self.high_water_mark <= 0
        ):
            raise ValueError("high_water_mark must be a finite positive Decimal")
        if type(self.coefficient) is not Decimal or self.coefficient not in (
            COEFFICIENT_NORMAL,
            COEFFICIENT_TIER1,
            COEFFICIENT_TIER2,
        ):
            raise ValueError("coefficient must be a Decimal of 1, 0.5 or 0.25")
        _identifier(self.series_id, "series_id")
        if self.last_decision_date is not None:
            _day(self.last_decision_date, "last_decision_date")
        elif self.coefficient != COEFFICIENT_NORMAL:
            raise ValueError(
                "a state without a prior decision must sit at the normal coefficient"
            )
        if self.version != MARKET_RISK_LAYER_VERSION:
            raise ValueError(
                "risk state version mismatch; reinitialize the series explicitly"
            )

    @classmethod
    def initial(cls, nav: Decimal, series_id: str) -> RiskState:
        """Explicit first-use state: watermark pinned to the first audited NAV."""
        _decimal_positive(nav, "initial nav")
        return cls(
            high_water_mark=nav,
            coefficient=COEFFICIENT_NORMAL,
            series_id=series_id,
        )


@dataclass(frozen=True)
class RiskInputs:
    """Everything one decision may read; nothing after ``decision_date``.

    ``sessions`` is the shared trading calendar. It may extend beyond the
    decision date only to derive the next execution date. Observation tuples
    must be uniquely dated and cannot carry future sessions.
    """

    decision_date: date
    sessions: tuple[date, ...]
    unscaled_risk_returns: tuple[UnscaledReturn, ...] = ()
    index_closes: tuple[IndexClose, ...] = ()
    index_id: str = ""
    strategy_nav: StrategyNav | None = None
    drawdown_state: RiskState | None = None
    return_source: str = ""
    index_source: str = ""
    nav_source: str = ""
    nav_series_id: str = ""

    def __post_init__(self) -> None:
        _day(self.decision_date, "decision_date")
        if type(self.sessions) is not tuple or any(
            type(s) is not date for s in self.sessions
        ):
            raise ValueError("sessions must be an immutable tuple of dates")
        if len(self.sessions) < 3 or tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("sessions must be unique and strictly increasing")
        if self.decision_date not in self.sessions:
            raise ValueError("decision_date must be one of the sessions")
        self._check_observations(
            self.unscaled_risk_returns, "unscaled_risk_returns", self.decision_date
        )
        if self.unscaled_risk_returns and not self.return_source:
            raise ValueError("return_source is required with risk returns")
        self._check_observations(
            self.index_closes, "index_closes", self.decision_date
        )
        if self.index_closes and not self.index_source:
            raise ValueError("index_source is required with index closes")
        if self.index_closes and not self.index_id:
            raise ValueError("index_id is required with index closes")
        if self.strategy_nav is not None:
            if not isinstance(self.strategy_nav, StrategyNav):
                raise ValueError("strategy_nav must be a StrategyNav")
            if self.strategy_nav.unhandled_external_flow:
                raise ValueError(
                    "refusing NAV with unhandled external flows; model them first"
                )
            if self.strategy_nav.as_of != self.decision_date:
                raise ValueError("strategy_nav must be as of the decision_date")
            if not self.nav_source:
                raise ValueError("nav_source is required with a strategy NAV")
        if self.drawdown_state is not None and not isinstance(
            self.drawdown_state, RiskState
        ):
            raise ValueError("drawdown_state must be a RiskState")
        if (
            self.strategy_nav is not None or self.drawdown_state is not None
        ) and not self.nav_series_id:
            raise ValueError(
                "nav_series_id is required with a strategy NAV or drawdown state"
            )

    @staticmethod
    def _check_observations(values: tuple, name: str, decision_date: date) -> None:
        if type(values) is not tuple:
            raise ValueError(f"{name} must be an immutable tuple")
        seen: set[date] = set()
        for item in values:
            session = item.session
            if session in seen:
                raise ValueError(f"duplicate session in {name}")
            if session > decision_date:
                raise ValueError(f"{name} contains a future session {session}")
            seen.add(session)


@dataclass(frozen=True)
class RiskDecision:
    """One rule's cap decision with its evidence and provenance."""

    rule_id: str
    decision_date: date
    next_execution_date: date | None
    status: str
    cap: Decimal | None
    reasons: tuple[str, ...]
    config_fingerprint: str
    sources: tuple[str, ...] = ()
    vol_estimate: float | None = None
    ma_above: bool | None = None
    ma_mean: Decimal | None = None
    ma_close: Decimal | None = None
    high_water_mark: Decimal | None = None
    drawdown: Decimal | None = None
    coefficient: Decimal | None = None
    drawdown_state: RiskState | None = None

    def __post_init__(self) -> None:
        if self.rule_id not in RULE_IDS:
            raise ValueError(f"unknown rule_id {self.rule_id!r}")
        _day(self.decision_date, "decision_date")
        if self.next_execution_date is not None:
            _day(self.next_execution_date, "next_execution_date")
            if self.next_execution_date <= self.decision_date:
                raise ValueError("next_execution_date must follow the decision_date")
        if self.status not in (STATUS_OK, STATUS_UNKNOWN):
            raise ValueError("status must be ok or unknown")
        if self.status == STATUS_OK:
            if self.cap is None:
                raise ValueError("an ok decision must carry a cap")
            if type(self.cap) is not Decimal or not self.cap.is_finite():
                raise ValueError("cap must be a finite Decimal")
            if not (Decimal(0) <= self.cap <= VOL_CAP):
                raise ValueError("cap outside the approved 0-0.8 range")
        elif self.cap is not None:
            raise ValueError("an unknown decision must not carry a cap")
        if self.config_fingerprint != RULE_CONFIG_FINGERPRINTS[self.rule_id]:
            raise ValueError("config_fingerprint does not match the frozen rule")
        if self.drawdown_state is not None and not isinstance(
            self.drawdown_state, RiskState
        ):
            raise ValueError("drawdown_state must be a RiskState")


def _next_execution_date(sessions: tuple[date, ...], decision_date: date) -> date | None:
    for session in sessions:
        if session > decision_date:
            return session
    return None


def _trailing_window(
    sessions: tuple[date, ...], decision_date: date, size: int
) -> tuple[date, ...] | None:
    end = sessions.index(decision_date)
    start = end + 1 - size
    if start < 0:
        return None
    return sessions[start : end + 1]


def _scheduling_reasons(
    sessions: tuple[date, ...], decision_date: date
) -> tuple[date | None, tuple[str, ...]]:
    following = _next_execution_date(sessions, decision_date)
    reasons: tuple[str, ...] = () if following else ("no_future_session",)
    return following, reasons


def _unknown(
    rule_id: str,
    inputs: RiskInputs,
    reasons: tuple[str, ...],
    **diagnostics: object,
) -> RiskDecision:
    following, schedule = _scheduling_reasons(inputs.sessions, inputs.decision_date)
    return RiskDecision(
        rule_id=rule_id,
        decision_date=inputs.decision_date,
        next_execution_date=following,
        status=STATUS_UNKNOWN,
        cap=None,
        reasons=reasons + schedule,
        config_fingerprint=RULE_CONFIG_FINGERPRINTS[rule_id],
        **diagnostics,
    )


CONSTANT_RULE_IDS = (CONSTANT_CAP_80_ID, CONSTANT_CAP_50_ID)


def decide_constant_cap(inputs: RiskInputs, rule_id: str) -> RiskDecision:
    """Constant upper bound; only C80/C50, never a stand-in for another rule."""
    if rule_id not in CONSTANT_RULE_IDS:
        raise ValueError(
            f"decide_constant_cap only accepts {CONSTANT_RULE_IDS}, got {rule_id!r}"
        )
    following, reasons = _scheduling_reasons(inputs.sessions, inputs.decision_date)
    cap = Decimal("0.8") if rule_id == CONSTANT_CAP_80_ID else Decimal("0.5")
    return RiskDecision(
        rule_id=rule_id,
        decision_date=inputs.decision_date,
        next_execution_date=following,
        status=STATUS_OK,
        cap=cap,
        reasons=reasons,
        config_fingerprint=RULE_CONFIG_FINGERPRINTS[rule_id],
    )


def decide_vol_target(inputs: RiskInputs) -> RiskDecision:
    """V: 60-session unscaled sample volatility against a 15% annual target."""
    window = _trailing_window(inputs.sessions, inputs.decision_date, VOL_WINDOW)
    if window is None:
        return _unknown(VOL_TARGET_ID, inputs, ("insufficient_warmup",))
    by_session = {item.session: item.value for item in inputs.unscaled_risk_returns}
    values: list[float] = []
    for session in window:
        value = by_session.get(session)
        if value is None:
            return _unknown(VOL_TARGET_ID, inputs, ("missing_return",))
        values.append(value)
    if any(not math.isfinite(value) for value in values):
        return _unknown(VOL_TARGET_ID, inputs, ("invalid_return",))
    try:
        mean = math.fsum(values) / len(values)
        sum_of_squares = math.fsum((value - mean) ** 2 for value in values)
    except OverflowError:
        # Individually finite inputs can still be jointly unrepresentable.
        return _unknown(VOL_TARGET_ID, inputs, ("nonfinite_volatility",))
    if not math.isfinite(mean) or not math.isfinite(sum_of_squares):
        return _unknown(VOL_TARGET_ID, inputs, ("nonfinite_volatility",))
    variance = sum_of_squares / (len(values) - 1)
    annualized = math.sqrt(variance) * math.sqrt(VOL_ANNUALIZATION_DAYS)
    if not math.isfinite(variance) or variance < 0.0 or not math.isfinite(annualized):
        return _unknown(VOL_TARGET_ID, inputs, ("nonfinite_volatility",))
    if annualized == 0.0:
        cap = VOL_CAP
    else:
        cap = min(VOL_CAP, VOL_TARGET_ANNUAL / Decimal(str(annualized)))
    if not cap.is_finite() or cap < 0 or cap > VOL_CAP:
        # Defensive: an unrepresentable estimate must never look like a cap.
        return _unknown(VOL_TARGET_ID, inputs, ("nonfinite_volatility",))
    following, reasons = _scheduling_reasons(inputs.sessions, inputs.decision_date)
    return RiskDecision(
        rule_id=VOL_TARGET_ID,
        decision_date=inputs.decision_date,
        next_execution_date=following,
        status=STATUS_OK,
        cap=cap,
        reasons=reasons,
        config_fingerprint=RULE_CONFIG_FINGERPRINTS[VOL_TARGET_ID],
        sources=(inputs.return_source,) if inputs.return_source else (),
        vol_estimate=annualized,
    )


def decide_index_ma_regime(inputs: RiskInputs) -> RiskDecision:
    """M: CSI All Share 000985 close strictly above its own 200-session mean."""
    if inputs.index_closes and inputs.index_id != APPROVED_EQUITY_PROXY_INDEX_ID:
        raise ValueError(
            f"M accepts only index {APPROVED_EQUITY_PROXY_INDEX_ID}, got "
            f"{inputs.index_id!r}"
        )
    window = _trailing_window(inputs.sessions, inputs.decision_date, MA_WINDOW)
    if window is None:
        return _unknown(INDEX_MA_REGIME_ID, inputs, ("insufficient_warmup",))
    by_session = {item.session: item.close for item in inputs.index_closes}
    closes: list[Decimal] = []
    for session in window:
        close = by_session.get(session)
        if close is None:
            return _unknown(INDEX_MA_REGIME_ID, inputs, ("missing_index_close",))
        if close <= 0:
            return _unknown(INDEX_MA_REGIME_ID, inputs, ("invalid_index_close",))
        closes.append(close)
    mean = sum(closes) / Decimal(MA_WINDOW)
    today = closes[-1]
    above = today > mean
    following, reasons = _scheduling_reasons(inputs.sessions, inputs.decision_date)
    return RiskDecision(
        rule_id=INDEX_MA_REGIME_ID,
        decision_date=inputs.decision_date,
        next_execution_date=following,
        status=STATUS_OK,
        cap=MA_CAP_ON if above else MA_CAP_OFF,
        reasons=reasons,
        config_fingerprint=RULE_CONFIG_FINGERPRINTS[INDEX_MA_REGIME_ID],
        sources=(inputs.index_source,) if inputs.index_source else (),
        ma_above=above,
        ma_mean=mean,
        ma_close=today,
    )


def _next_coefficient(current: Decimal, drawdown: Decimal) -> Decimal:
    if drawdown >= DRAWDOWN_TIER2_ENTER:
        return COEFFICIENT_TIER2
    if current == COEFFICIENT_TIER2 and drawdown <= DRAWDOWN_TIER2_RECOVER:
        # The severe-tier recovery band includes 10-12% drawdowns; checked
        # before the 10% entry rule so the approved 12% threshold stays live.
        return COEFFICIENT_TIER1
    if drawdown >= DRAWDOWN_TIER1_ENTER:
        return min(current, COEFFICIENT_TIER1)
    if current == COEFFICIENT_TIER1 and drawdown <= DRAWDOWN_TIER1_RECOVER:
        return COEFFICIENT_NORMAL
    return current


def decide_drawdown_governor(inputs: RiskInputs) -> RiskDecision:
    """D: tiered de-risking on the auditable strategy NAV drawdown."""
    if inputs.strategy_nav is None:
        return _unknown(
            DRAWDOWN_GOVERNOR_ID,
            inputs,
            ("nav_unavailable",),
            drawdown_state=inputs.drawdown_state,
        )
    state = inputs.drawdown_state
    if state is None:
        # A restart that lost its state is not a fresh start: callers must
        # restore the persisted state or explicitly reinitialize the series.
        return _unknown(DRAWDOWN_GOVERNOR_ID, inputs, ("state_unavailable",))
    if state.series_id != inputs.nav_series_id:
        raise ValueError(
            f"drawdown state belongs to NAV series {state.series_id!r}, "
            f"inputs declare {inputs.nav_series_id!r}"
        )
    if state.last_decision_date is not None:
        if state.last_decision_date > inputs.decision_date:
            raise ValueError("drawdown state carries a future last decision date")
        if state.last_decision_date == inputs.decision_date:
            raise ValueError(
                "drawdown state already advanced on this decision date; rewind "
                "to the persisted pre-decision state to re-evaluate"
            )
    nav = inputs.strategy_nav.nav
    high_water_mark = max(state.high_water_mark, nav)
    drawdown = (high_water_mark - nav) / high_water_mark
    coefficient = _next_coefficient(state.coefficient, drawdown)
    new_state = RiskState(
        high_water_mark=high_water_mark,
        coefficient=coefficient,
        series_id=state.series_id,
        last_decision_date=inputs.decision_date,
    )
    following, reasons = _scheduling_reasons(inputs.sessions, inputs.decision_date)
    return RiskDecision(
        rule_id=DRAWDOWN_GOVERNOR_ID,
        decision_date=inputs.decision_date,
        next_execution_date=following,
        status=STATUS_OK,
        cap=DRAWDOWN_BASE_CAP * coefficient,
        reasons=reasons,
        config_fingerprint=RULE_CONFIG_FINGERPRINTS[DRAWDOWN_GOVERNOR_ID],
        sources=(inputs.nav_source,),
        high_water_mark=high_water_mark,
        drawdown=drawdown,
        coefficient=coefficient,
        drawdown_state=new_state,
    )


def decide_composite(inputs: RiskInputs, rule_id: str) -> RiskDecision:
    """VM/VMD: the minimum cap of the components; unknown blocks, never heals."""
    components = {
        COMPOSITE_VM_ID: (VOL_TARGET_ID, INDEX_MA_REGIME_ID),
        COMPOSITE_VMD_ID: (VOL_TARGET_ID, INDEX_MA_REGIME_ID, DRAWDOWN_GOVERNOR_ID),
    }[rule_id]
    decisions = [evaluate_market_risk_rule(component, inputs) for component in components]
    unknown_reasons: list[str] = []
    sources: list[str] = []
    caps: list[Decimal] = []
    diagnostics: dict[str, object] = {}
    for component_id, decision in zip(components, decisions, strict=True):
        if decision.status == STATUS_UNKNOWN:
            unknown_reasons.extend(
                f"{component_id}:{reason}" for reason in decision.reasons
            )
        elif decision.cap is not None:
            caps.append(decision.cap)
        sources.extend(decision.sources)
        if component_id == VOL_TARGET_ID:
            diagnostics["vol_estimate"] = decision.vol_estimate
        elif component_id == INDEX_MA_REGIME_ID:
            diagnostics["ma_above"] = decision.ma_above
            diagnostics["ma_mean"] = decision.ma_mean
            diagnostics["ma_close"] = decision.ma_close
        else:
            # The drawdown state advances whenever the D component evaluated,
            # so persistence is not lost when V or M is unknown.
            diagnostics["high_water_mark"] = decision.high_water_mark
            diagnostics["drawdown"] = decision.drawdown
            diagnostics["coefficient"] = decision.coefficient
            diagnostics["drawdown_state"] = decision.drawdown_state
    if unknown_reasons:
        return _unknown(
            rule_id,
            inputs,
            tuple(unknown_reasons),
            sources=tuple(sources),
            **diagnostics,
        )
    following, reasons = _scheduling_reasons(inputs.sessions, inputs.decision_date)
    return RiskDecision(
        rule_id=rule_id,
        decision_date=inputs.decision_date,
        next_execution_date=following,
        status=STATUS_OK,
        cap=min(caps),
        reasons=reasons,
        config_fingerprint=RULE_CONFIG_FINGERPRINTS[rule_id],
        sources=tuple(sources),
        **diagnostics,
    )


_RULE_EVALUATORS = {
    CONSTANT_CAP_80_ID: lambda inputs: decide_constant_cap(inputs, CONSTANT_CAP_80_ID),
    CONSTANT_CAP_50_ID: lambda inputs: decide_constant_cap(inputs, CONSTANT_CAP_50_ID),
    VOL_TARGET_ID: decide_vol_target,
    INDEX_MA_REGIME_ID: decide_index_ma_regime,
    DRAWDOWN_GOVERNOR_ID: decide_drawdown_governor,
    COMPOSITE_VM_ID: lambda inputs: decide_composite(inputs, COMPOSITE_VM_ID),
    COMPOSITE_VMD_ID: lambda inputs: decide_composite(inputs, COMPOSITE_VMD_ID),
}


def evaluate_market_risk_rule(rule_id: str, inputs: RiskInputs) -> RiskDecision:
    """Evaluate one of the seven frozen rules for one decision date."""
    evaluator = _RULE_EVALUATORS.get(rule_id)
    if evaluator is None:
        raise ValueError(f"unknown market risk rule {rule_id!r}")
    return evaluator(inputs)
