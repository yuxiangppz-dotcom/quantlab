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

Results are written under `data/experiments/research_backtest_v0_2_4/`, including
`summary.json` (metadata + status + provenance + metrics), `manifest.json`
(input data content fingerprint), `strict_daily_records.csv`,
`strict_daily_books.csv`, `strict_daily_positions.csv`,
`strict_rebalance_log.csv`, `strict_trade_details.csv`,
`diagnostic_daily_books.csv`, `diagnostic_daily_positions.csv`,
lifecycle event CSVs, and `delisting_audit.csv` / `delisting_facts.json`
(a point-in-time delisting fact record keyed by instrument, with source
coverage marked `verified` or `unknown`).

The engine simulates two independent ledgers (gross = zero-cost counterfactual,
net = actual cost) with **self-financing** transaction cost solved on the
normalized interval `[frozen/NAV, 1]`, plus **final-ledger reconciliation
checks** (fee consistency, cash flow, NAV bridge, position reconciliation,
asset identity, frozen invariance). Held positions with no current price are
frozen (`freeze_held_no_price`). Unsupported delisting / code-change events are
checked per session against canonical boundaries, independently for both books
and decoupled from price availability.

Two run modes are supported:

- `strict` (default): stops before the first unsupported event, sets
  `status = blocked_by_unsupported_event`, leaves `metrics = null`, and reports
  `valid_through` plus the first blocking event.
- `diagnostic`: continues past events with `diagnostic_only` marking, and never
  returns a valid completed performance result.

This is an idealized portfolio simulation (`test_observed = true`,
`performance_claim = false`), not an execution simulator. `performance_valid`
is a separate boolean from `performance_claim`; a blocked run never claims a
completed full-period performance.

## Lifecycle Admission

The runner emits a **shadow admission audit** (`shadow_admission.csv`) and, as
of v0.2, an **enforcement experiment** (`no_new_exposure_after_termination_decision_v1`)
that caps a restricted instrument's new exposure at its pre-rebalance amount
inside the self-financing solver (forbid buys / refills, allow sells and
freeze rules, no forced liquidation). Both the baseline (no restriction) and
the admission path are run on identical inputs and compared.

It uses only *trusted* delisting facts — facts whose content is verified **and**
whose historical public time is independently evidenced (a verified public date
with no intraday time is available from the next trading day open, derived from
the full canonical calendar with completeness checks). `unknown` means
insufficient trusted fact coverage, is passed through unchanged, and is
**not** a claim of safety. The fact input is fingerprinted and the same loaded
snapshot drives the shadow decision, the admission path, and the baseline.

v0.3 adds a fixed **fact batch** (the first 10 unique delisted instruments from
the 85-event baseline, selected by blocking session order, not by returns) in a
separate facts version, and runs three paths — A baseline, B admission with the
original facts, C admission with the expanded-batch facts — plus a generic
buy-rejection evaluation (`true` / `false` / `not_evaluated`) that never
hardcodes a single instrument. Retrieval is per-instrument and recorded as
`verified` / `searched_unresolved` / `access_blocked` / `not_attempted`; as of
this round 6 of 10 batch instruments are verified (000018.SZ, 600240.SH,
600074.SH, 601558.SH, 002604.SZ, 300104.SZ) and 4 remain `searched_unresolved`
(002509.SZ, 300028.SZ, 600175.SH, 300090.SZ).

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
