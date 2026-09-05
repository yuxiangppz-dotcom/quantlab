# Architecture

## Layer Responsibilities

QuantLab separates concerns into layers with a strict dependency direction:

```
data (canonical)  ←  research  ←  alpha  ←  portfolio  ←  backtest/execution
```

Higher layers may read lower layers, never the reverse. In particular:

- **`data`** owns canonical facts and never knows about factors or portfolios.
- **`research`** derives adjusted prices, returns, samples, and evaluation
  metrics from canonical data only.
- **`alpha`** produces signals (`alpha_score`) from a research dataset and
  metadata at time `t` only.
- **`portfolio`** turns signals into target weights.
- **`backtest` / `execution`** simulate or route those weights.

## Canonical vs Research

**Canonical data** is the raw, provider-independent record:

- `Security`, `TradingCalendar`, `DailyBar`, `AdjFactor`, `DailyBasic`
- stored one Parquet file per trading date under `data/canonical/`
- units normalized at the provider boundary (shares, CNY, decimal fractions)
- never mutated by research code

**Research data** is derived:

- `ResearchDailyPrice` (adjusted close = close × adj_factor)
- historical / forward returns on the global market calendar
- a research sample DataFrame with universe filtering and session padding

Research never writes back to canonical storage.

## Data Flow

```
Provider (Tushare)
   → canonical sync (validate → atomic Parquet write)
   → research dataset builder (session-padded read)
   → alpha (score)
   → portfolio (target weights)
   → backtest (idealized PnL + metrics)
```

The dataset builder pads the internal read range by `max(return_horizons)`
sessions before `start_date` and `max(forward_horizons)` sessions after
`end_date`, then trims back to the requested window — so boundary rows have
valid features and labels.

## Implemented (v0)

### TargetPortfolio

Implemented in `src/quantlab/portfolio/`. A `TargetPortfolio` represents desired
holdings on one `as_of` date — `(instrument_id → target_weight)` plus a residual
`cash_weight` — produced by a rank-based constructor from a single alpha
cross-section. It separates "what to hold" from "how to trade", and it is an
intention, not an order, fill, or return forecast.

Weights are relative to NAV: `sum(position weights) + cash_weight == 1.0`.
`target_weight` and `cash_weight` are signed floats (positive = long, negative =
short), so the domain model does not permanently forbid short positions; only
finiteness and non-empty unique `instrument_id` are enforced. Net and gross
exposure are derived properties (`sum` and `sum(abs)` respectively) rather than
stored fields.

The v0 constructor stays long-only and equal-weight, with a selection fraction
and an optional per-name weight cap. It is direction-agnostic
(`higher_is_better` / `lower_is_better`) so any alpha can reuse it, and it
balances cash as `1.0 - n*weight` (all-NaN input → `cash_weight = 1.0`).

### Research Backtest

Implemented in `src/quantlab/backtest/`. An idealized value-based engine
(`cash_value` + per-instrument `positions_value`) that simulates T-signal →
T+1-close execution and lets the portfolio drift between rebalances. PnL is
derived only from chronological adjusted closes — `future_return_*` labels are
never read. Held positions with a missing bar mark return 0; a new target with
no execution-date bar is not opened and stays in cash. Transaction cost is a
symmetric proportional charge that reduces net NAV but not gross NAV. It is a
research simulator, not an execution simulator.

## Future Concepts

These are intended directions, not implemented yet.

### AlphaSource

A declarative description of an alpha — universe, feature definition, label,
horizons, and discovery protocol. The experiment runner currently encodes one
such source (`momentum_20d`); a future Alpha Research Factory would generate and
screen `AlphaSource` candidates, with promotion governed by the research
protocol, not by ad-hoc parameter search.

### Trading Ledger

An append-only record of intended vs. executed orders and fills. The ledger is
the boundary between research simulation and real trading — it makes execution
attributable and audit-able.

### Research vs Execution Separation

Research produces signals and evaluates them on historical data. Execution
turns live signals into orders under real-world constraints (liquidity, price
limits, fees). A signal that scores well in RankIC is not automatically a
tradable strategy; the two are validated separately and linked through the
trading ledger.
