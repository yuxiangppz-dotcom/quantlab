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

Implemented in `src/quantlab/backtest/`. It simulates **two independent
ledgers** (gross and net) that share the same market inputs, target sequence and
valuation logic but keep separate `cash_value` and per-instrument position
amounts. Positions are simulated as adjusted-close value amounts, **not share
counts**.

- The **gross** book runs with `cost_rate = 0` (a zero-cost counterfactual); the
  **net** book uses `config.cost_rate`.
- Market returns mark both books to market; transaction cost is charged
  **self-financing** against actual traded value (`v + c * traded(v) = v_minus`,
  solved by bounded bisection), so cost permanently reduces only the net book.
- Held positions with no current price are **frozen** (`freeze_held_no_price`):
  they are valued at their last available mark but cannot be bought or sold. A
  new target with no execution-date bar is not opened and stays in cash.
- PnL comes only from chronological adjusted closes; `future_return_*` labels are
  never read.

After every session, a **final-ledger reconciliation** reports, per book and per
day, the max absolute and relative residual of: fee consistency
(`fee = c * Σ|signed trades|`), cash flow, rebalance NAV, daily NAV bridge,
position reconciliation, asset identity, and frozen-position invariance. The
solver residual is kept separate; exceeding the accounting tolerance raises
`accounting_error`.

Unsupported lifecycle events are detected by a separate `LifecycleMonitor` from
canonical `Security` / `SecurityCodeChange` data, independently for both books
and decoupled from price availability:

- `delist`: valid through `delist_date` (inclusive); a held position blocks from
  the first session with `trade_date > delist_date`.
- `code_change`: the old instrument is invalid from `effective_date`
  (`trade_date >= effective_date`).
- contradictory event definitions are reported as `conflict`.

Two run modes exist: `strict` stops before the first unsupported event and
reports `valid_through`, `first_blocking_event`, and `metrics = null`;
`diagnostic` continues with `diagnostic_only` marking and never returns a valid
completed result. It is a research simulator, not an execution simulator — a
blocked run never claims a completed full-period performance.

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
