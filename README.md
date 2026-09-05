# QuantLab

A-share quantitative research and trading system — covering point-in-time (PIT)
market data, alpha research, portfolio construction, backtesting, and eventual
live execution.

## Project Goals

The end goal is not a "factor library". QuantLab is built around a single
pipeline:

```
Data → Research → Alpha → Portfolio → Execution → Feedback
```

The objective is long-term risk-adjusted return, with every research and
trading step reproducible and free of look-ahead bias.

## Current Status

**Data Foundation**

- [x] Security master (list/delist dates, board, list status)
- [x] Security code history (old → new instrument codes)
- [x] Trading calendar (SSE / SZSE)
- [x] Daily OHLCV bars
- [x] Cumulative adjustment factors
- [x] DailyBasic / market characteristics (turnover, market cap)
- [x] Point-in-time validity

**Research**

- [x] Adjusted close prices
- [x] Historical returns on the global market calendar
- [x] Forward return labels
- [x] Research Dataset Builder (with session padding)
- [x] V1 SH/SZ A-share universe
- [x] RankIC / quantile-return evaluation
- [x] Experiment runner (pre-registered config + metadata)
- [x] Momentum 20D baseline
- [x] Size / turnover robustness infrastructure

**Portfolio**

- [x] Target portfolio representation (signed weights, NAV-balanced cash)
- [x] Derived net / gross exposure (not a stored field)
- [x] Portfolio construction (long-only rank-based, equal-weight v0)

**Backtest**

- [x] Research backtest (dual gross/net ledgers, self-financing cost, T+1-close)
- [x] Freeze held positions missing a price (`freeze_held_no_price`)
- [ ] A-share execution constraints

**Live**

- [ ] Paper trading
- [ ] Broker / QMT execution

## Architecture

```
Tushare
   │
   ▼
Provider  (data provider abstraction)
   │
   ▼
Canonical Data  ── Security / Calendar / DailyBar / AdjFactor / DailyBasic
   │
   ▼
Research Dataset
   │
   ▼
Alpha
   │
   ▼
Evaluation
   │
   ▼
Portfolio
   │
   ▼
Backtest / Execution
```

Two hard rules:

- **Strategy / Research never calls the Provider directly.** Research only reads
  canonical data.
- **Canonical and Research are separate layers.** Canonical holds raw facts;
  Research derives adjusted prices, returns, and samples.

See [docs/architecture.md](docs/architecture.md) for layer responsibilities and
future concepts.

## Data Model

Canonical types (see `src/quantlab/data/models.py`):

| Type | Purpose |
|---|---|
| `Security` | instrument master (exchange, board, list/delist dates) |
| `TradingCalendar` | open/closed sessions per exchange |
| `DailyBar` | OHLCV bar for one instrument/date |
| `AdjFactor` | raw cumulative adjustment factor |
| `DailyBasic` | turnover rate, total/circulating market cap |
| `SecurityCodeChange` | old → new instrument code lineage |

Canonical units (provider conversions happen at the provider boundary):

- `DailyBar.volume` — shares
- `DailyBar.amount` — CNY
- `DailyBasic.turnover_rate` — decimal fraction (5.2% → 0.052)
- `DailyBasic.total_mv` / `circ_mv` — CNY
- `AdjFactor.adj_factor` — provider raw cumulative factor (no forward adjustment)

## Point-in-Time Correctness

QuantLab treats PIT validity as a first-class property, not an afterthought:

- Historical universes use `list_date` / `delist_date`; current `list_status` is
  never used to filter history.
- Security code changes keep the historical instrument identity (a delisted code
  is not confused with its successor).
- `return_Nd` is defined on the global market session calendar, not on a stock's
  own row sequence.
- A suspended target session yields `NaN` — never forward/backward fill, never
  fall back to the nearest valid bar.
- `future_return_*` columns are labels only and never feed a feature.
- Discovery / Validation / Test use period-contained label policy (a label must
  fall entirely within its own period).
- Once Test has been observed, it is no longer claimed as an untouched holdout.

See [docs/research_protocol.md](docs/research_protocol.md).

## Research V1

**Universe** — SH/SZ A-shares:

- Included: main board, STAR market (科创板), ChiNext (创业板)
- Excluded: BJ (北交所), SH B-shares (900xxx), SZ B-shares (200xxx)

**Current baseline — Momentum 20D**: `alpha_score = return_20d`.

Objective finding (not a performance claim):

> The 20D positive-momentum hypothesis was rejected; the signal showed
> consistently negative cross-sectional RankIC against 5D/20D forward returns,
> suggesting short-horizon reversal behavior.

`RankIC` is a signal-quality diagnostic, not tradable PnL. Portfolio / backtest
validation is the next step before any profitability claim.

## Repository Structure

```
src/quantlab/
    data/        # canonical models, provider, storage, sync, security history
    research/    # prices, returns, dataset, universe, evaluation, characteristics
    alpha/       # alpha factors (currently momentum)
    portfolio/   # target portfolio model and rank-based constructor
    backtest/    # idealized research backtest engine + metrics
scripts/         # data update + experiment runners
config/          # security_code_changes.csv (version-controlled reference)
docs/            # research protocol + architecture
tests/
data/            # canonical + experiment output (git-ignored)
```

## Setup

Requirements: Python >= 3.12, [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check .
```

Tushare token (never committed to the repository):

```bash
export TUSHARE_TOKEN=...
```

## Data Commands

`scripts/update_market_data.py` downloads canonical data by date range:

```bash
uv run python scripts/update_market_data.py \
    --start 2010-01-01 --end 2026-09-04 \
    --securities --calendar

uv run python scripts/update_market_data.py \
    --start 2010-01-01 --end 2026-09-04 --daily-all

uv run python scripts/update_market_data.py \
    --start 2010-01-01 --end 2026-09-04 --adj-factor

uv run python scripts/update_market_data.py \
    --start 2010-01-01 --end 2026-09-04 --daily-basic
```

`--daily-all` / `--adj-factor` / `--daily-basic` are resumable: existing files
are skipped unless `--force` is passed. `--securities` and `--calendar` upsert
(merge) rather than replace.

The `data/` directory is git-ignored; canonical data is never committed.

## Research Commands

Run the pre-registered Momentum 20D experiment:

```bash
uv run python scripts/run_alpha_research.py --alpha momentum_20d
```

Run the size / turnover robustness analysis:

```bash
uv run python scripts/run_momentum_robustness.py
```

Results are written under `data/experiments/` (git-ignored), including
`summary.json` (metadata + git SHA), `yearly_rank_ic.csv`, and bucket metrics.

## Backtest Commands

Run the fixed-config research backtest (2020–2024, momentum 20D lower-is-better,
weekly rebalance, 20% selection, 10 bps cost):

```bash
uv run python scripts/run_research_backtest.py
```

Results are written under `data/experiments/research_backtest_v0_2/`, including
`summary.json` (metadata + metrics + provenance), `manifest.json` (input data
content fingerprint), `daily_records.csv`, `daily_books.csv`,
`daily_positions.csv`, `rebalance_log.csv`, and `trade_details.csv`.

The engine simulates two independent ledgers (gross = zero-cost counterfactual,
net = actual cost) with self-financing transaction cost and a
`freeze_held_no_price` policy for held positions missing a price. This is an
idealized portfolio simulation (`test_observed = true`,
`performance_claim = false`), not an execution simulator. A run that encounters
unsupported delisting / code-change events is reported as
`blocked_by_unsupported_event` rather than as a completed performance result.

## Research Philosophy

- Hypothesis before optimization.
- PIT correctness before speed.
- No look-ahead, ever.
- Reproducible, versioned experiments.
- Signal quality is separate from portfolio profitability.
- Prefer incremental alpha over redundant alpha.

## Roadmap

| Phase | Status |
|---|---|
| 1. Data + Alpha Research | ✅ |
| 2. Portfolio Engine | ✅ |
| 3. Research Backtest | ✅ |
| 4. A-share Execution Engine | — |
| 5. Risk / Attribution | — |
| 6. Alpha Research Factory | — |
| 7. Paper / Live Trading | — |

The Alpha Research Factory will let agents generate and screen candidate alphas,
with promotion criteria enforced by the system's research protocol.
