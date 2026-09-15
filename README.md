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
- [x] Lifecycle Date Semantics (done / frozen; v1 remains a candidate)
- [x] Lifecycle Risk Policy v0 (implemented / engineering validation)
- [x] Systematic Lifecycle Event Data v0 foundation (raw/canonical/PIT/audit)
- [x] Systematic Lifecycle Context Data (v0.1.1: `stock_st` + `suspend_d` complete for 2020-01-01 ~ 2024-12-31)
- [x] Delisting settlement as an explicit held-delist scenario (recovery assumptions, delist-only)
- [x] True equal-weight control as primary benchmark (PIT security-master eligibility)
- [x] Index attribution + benchmark coverage audit (price-index close basis)
- [x] Performance baseline v0.1.1 correctness closure (relative-wealth active metrics, formal clean-git reproducibility, full run-spec symmetry audit)
- [ ] Systematic termination announcement coverage (BLOCKED: current Tushare account lacks `anns_d`)
- [x] Execution framework v0.1 (typed orders, append-only ledger, PIT rule
  decisions, fail-closed target-to-share handoff, formal readiness audit)
- [ ] Historical execution replay readiness (blocked by PIT identity/rule gaps,
  price-limit/auction/fill evidence, exact fees, and corporate actions)

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
    execution/   # handoff, order planning, ledger reservations, constraints, readiness
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

## QuantLab Daily v1.1 local tool

The product entry point now exposes real commands rather than a placeholder:

```bash
uv run quantlab doctor
uv run quantlab update
uv run quantlab update --through 2026-09-09 --no-context --enrichment \
  --financial-period 2024-12-31 --dividend-instrument 000001.SZ
uv run quantlab daily --as-of 2026-09-10
uv run quantlab shadow
uv run quantlab research-status
uv run quantlab research-status --json
uv run quantlab portfolio demo
uv run quantlab portfolio plan --account-id demo_200k
uv run quantlab portfolio fills-preview --account-id my_account --file fills.csv
uv run quantlab portfolio fills-import --account-id my_account --file fills.csv
uv run quantlab portfolio track --account-id my_account
uv run quantlab accept --account-id demo_200k
uv run quantlab ui
```

`update` is the only command above that calls Tushare and writes Canonical
partitions. `daily` is read-only with respect to Canonical data: it selects the
latest common complete date required by the configured model, reads only its
lookback, and writes an idempotent local cache under `data/products/daily/`.
If the requested date is newer than local calendar/data coverage, both CLI and
UI show the older `effective_as_of` as stale instead of calling it today's
result.

The opt-in `--enrichment` path stores provider-reported signal-date price
limits plus explicitly scoped, first-observed financial/dividend snapshots.
Financial versions are prospective-only because the endpoint does not expose a
revision timestamp; they are not retroactively joined to historical research.
`shadow` freezes immutable forward-only scores and targets for the baseline and
transparent candidate, without creating orders or fills.

`research-status` is read-only and does not require Canonical market data. It combines the
strategy registry, content-bound experiment catalog and Forward Shadow diagnostics; missing
optional evidence directories remain visibly missing, while malformed existing evidence stops
the command. Its label summaries are overlapping diagnostics, not NAV, CAGR or executable PnL.

Personal accounts can be imported with:

```bash
uv run quantlab portfolio import --file /path/to/complete_account_snapshot.csv
```

The CSV requires one row per position and repeats the account-level fields on
every row: `account_id`, `account_mode`, timezone-aware `as_of`, `cash_cny`,
`instrument_id`, `quantity`, `sellable_quantity`, `reference_cost_cny`, and
`open_orders_declaration`. Generated plans are reference-only. They use the
signal day's raw close, current cash, imported sellable quantity, and a visibly
incomplete user-reported commission estimate; they are not executable orders.

Manual fills use a second exact CSV schema: `account_id`, `broker_trade_id`,
`trade_date`, timezone-aware `reported_at`, `instrument_id`, `side`, `quantity`,
`price_cny`, `gross_notional_cny`, and `fee_cny`. Preview performs a full replay
without writing. Import commits the complete deduplicated journal atomically;
re-importing the same broker trade is a no-op, while altered economics under the
same trade id are rejected. These facts update the local manual-tracking ledger
only—they never submit an order or change Canonical market data.

The UI can create immutable user configuration versions for the frozen
return-20D reversal example or the unpromoted transparent multi-factor
candidate, with user-selected target count, per-name cap and board subset.
These variants are stored separately and explicitly lose direct comparability
with the frozen baseline artifact. See [the v1 user guide](docs/user_guide.md)
for the complete daily, account, plan and fill workflow.

The UI listens on `127.0.0.1:8501` and has four pages: data/report, rankings,
formal baseline comparison, and account/reference-plan status. From Windows,
the same local-only server can be started with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_quantlab_ui.ps1
```

The current model is explicitly a **test-observed research example**. Its
formal 2020–2024 strategy result underperformed the same-universe equal-weight
control; rankings are not investment advice, executable orders, or fills. See
[product status](docs/product_status.md) and
[known limitations](docs/known_limitations.md).

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

uv run python scripts/update_market_data.py \
    --start 2020-01-01 --end 2020-12-31 --lifecycle-context
```

`--daily-all` / `--adj-factor` / `--daily-basic` are resumable: existing files
are skipped unless `--force` is passed. `--securities` and `--calendar` upsert
(merge) rather than replace.

## Execution Readiness

The execution framework is intentionally separate from a claim that historical,
paper, or live trading is ready. From a committed, clean workspace, run:

```bash
uv run python scripts/run_execution_readiness.py
```

It publishes only evidence and readiness gates under
`data/experiments/execution_readiness_v0_2_2/<run_id>/` (schema
`execution_readiness_v0_2_2`; the v0.1 artifacts stay valid and the
superseded v0.2/v0.2.1 artifacts keep verifying under their own
schemas but are not frozen evidence). The audit includes the synthetic,
non-trading order-path smoke suite, transactional fault-injection
evidence, fee/reservation reconciliation, row-level daily/ST/suspension
partition audits, and fail-closed readiness gates whose composite READY
decisions are re-derived from disclosed sub-conditions at every
enforcement point. It computes no strategy performance, invents no fills,
calls no provider, writes no canonical data, and submits no orders.
See [docs/execution_readiness.md](docs/execution_readiness.md).

The `data/` directory is git-ignored; canonical data is never committed.

## ZCode + Codex Agent Loop

The optional local shared-agent loop lets a scheduled ZCode executor hand a
clean, pushed implementation to an independent Codex reviewer without copying
task cards or run reports between applications. Its transactional mailbox is
Git-ignored, binds every generation to exact Git heads and immutable artifact
hashes, and fails closed on concurrent claims or evidence drift. It changes no
financial or execution semantics. See [docs/agent_loop.md](docs/agent_loop.md).

## Systematic Lifecycle Event Data v0

`SecurityLifecycleEvent` is a separate canonical layer: a raw, date-partitioned
announcement index is classified with a versioned Chinese-title rule, then only
an unambiguous formal **termination decision** becomes a trusted event. Its
daily `available_from` is always the first open session *after* its announcement
date—even when a source retains an intraday timestamp. ST status, suspensions,
and name changes are stored only as context; none can trigger an exit.

Run the source capability/coverage audit independently from the risk-policy
experiment:

```bash
uv run python scripts/run_lifecycle_event_coverage.py
```

Lifecycle context uses a versioned `*_v1` date partition for each market open
session. `stock_st` is complete only when every requested session is validated
below Tushare's 1,000-row limit; `suspend_d` retains raw `S` / `R` daily facts
(`suspend_timing` included) and never invents a resume interval. The prior
unversioned suspension context is not a trusted input because it used an
incorrect request parameter.
As of the v0.1.1 source-correctness closure, `stock_st` and `suspend_d` are
complete for 2020-01-01 ~ 2024-12-31: 1,212/1,212 expected open sessions each,
with per-session date-scope validation and no silent truncation. Therefore
`lifecycle_context_readiness = complete_for_2020_2024` while
`termination_announcement_source_readiness` stays
`blocked_by_missing_anns_d_permission`; the two readiness states are tracked
independently.
The runner therefore writes `blocked_by_missing_anns_d_permission`, performs no
manual announcement discovery, and leaves `config/delisting_facts_v2.json` as
the regression gold reference. If `anns_d` access is later granted, the same
runner resumes daily raw-index synchronization, normalization, golden matching,
coverage, and the `002509.SZ` diagnostic automatically.

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

Results are written under
`data/experiments/performance_baseline_benchmark_correctness_v0_1_3/<run_id>/`,
including
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

## Lifecycle Risk Policy v0

Run the fixed control-vs-exit-policy experiment without reopening the frozen
date-semantics matrix:

```bash
uv run python scripts/run_lifecycle_risk_policy.py
```

The comparison uses the same 2020–2024 market data, target sequence, 10 bps
cost, fact snapshot, and candidate `delist_date_is_first_invalid_v1` boundary
for both paths:

- control: `no_new_exposure_after_termination_decision_v1`;
- new: `exit_after_termination_decision_v1`.

The new policy immediately requires zero exposure once a trusted termination
fact is available, retries across missing-price sessions, exits at the first
valid current-session close, and prevents later re-entry. Released weight stays
in cash. Results and detailed risk audits are written under
`data/experiments/lifecycle_risk_policy_v0_1/` and are never committed.

Fact coverage remains incomplete—`unknown` is not safe—and an unpriced position
that reaches lifecycle invalidation still blocks strict mode. Risk Policy is
therefore not a complete Corporate Action Engine and does not invent terminal
settlement.

## Performance Baseline & Benchmark Correctness v0.1.1

Current facts of the formal 2020–2024 baseline
(`performance_baseline_benchmark_correctness_v0_1_1`):

- **Risk policy first, settlement second.** The primary path runs
  `exit_after_termination_decision_v1` on a validated fact snapshot;
  delisting settlement is implemented as an **explicit scenario** that only
  handles *held* instruments crossing a `delist` boundary (settlement is
  delist-only). It is a recovery-assumption scenario over `[0, last_mark]` —
  never a claim of actual fills, actual liquidation, or guaranteed economic
  recovery (`recovery_assumption_1` / `recovery_assumption_0`).
- **True equal-weight control is the primary benchmark.**
  `equal_weight_v1_control` is a real self-financing portfolio over the full
  PIT-eligible V1 cross-section — eligibility built from the security master
  (historical `list_date`, frozen delist/code-change boundary semantics,
  `is_v1_a_share`), never from the price-backed research universe. An
  instrument eligible but suspended on the signal date keeps its 1/N weight;
  the engine leaves unfilled weights in cash and keeps held suspended names
  frozen at their stale mark. The control gets its own formal report
  (validity gates + full metrics) and a failing control blocks the primary
  comparison.
- **Index attribution is implemented** as a secondary layer vs
  000300.SH / 000905.SH / 000852.SH on **raw price-index closes**
  (`index_return_basis = price_index_close`); per-instrument session
  coverage is audited and incomplete coverage fails the attribution. Raw
  index closes are not dividend-adjusted total returns.
- **Active metrics use one consistent relative-wealth path**: cumulative
  active return, active CAGR and active MDD are all computed from
  strategy NAV / control NAV. `prod(1 + s - b)` is reported only as
  `arithmetic_active_nav_diagnostic`.
- **Formal reproducibility requires a clean git workspace** before and after
  the run, an unchanged HEAD, and stable code/data content manifests. A dirty
  workspace can never publish formally reproducible, performance-valid
  metrics (experiment outputs under `data/experiments/` are git-ignored and
  never count as dirt).
- **Symmetry is audited on the full run specification** — calendar, signal
  schedule, execution lag, cost, missing-price policy, run mode, lifecycle
  boundary mode, lifecycle monitor snapshot, risk policy, fact snapshot,
  settlement recovery/fee, initial NAV, period, annualization. Only target
  construction may differ.
- **Lifecycle date semantics remain frozen**: the formal baseline runs the
  disclosed `legacy_delist_date_inclusive` boundary for BOTH strategy and
  control (chosen baseline mode, not re-selected this round);
  `delist_date_is_first_invalid_v1` remains a candidate. Termination
  announcement coverage is still
  `blocked_by_missing_anns_d_permission`.
- **PIT code-lineage identity (v0.1.2)**: a code-change fact defines an
  explicit identity interval — before the `effective_date` only the old
  instrument id exists; from the effective date (inclusive) only the new id
  exists. A successor's vendor-backfilled `original_list_date` (mirrored
  into the successor master row) is NOT a visibility fact, so a future
  successor id (e.g. `302132.SZ`, effective 2025-02-17) never appears in
  2020–2024 eligibility even though the vendor backfilled history bars under
  it. A predecessor absent from the security master (e.g. `300114.SZ`)
  stays PIT-eligible (eligible-but-unpriced) until the day before the
  effective date and is never silently aliased to the successor's backfilled
  prices. `code_change_lineage_audit` proves per lineage: zero
  future-successor violations, zero old/new overlap, and counts
  eligible-but-unpriced predecessors.
- **Fail-closed artifact commit protocol (v0.1.3)**: every run is written
  into `<run_id>.incomplete/`; publication follows a strict state machine —
  manifest → preflight verification (no completion marker involved; any
  `COMPLETED.json` inside a staging directory is invalid by definition) →
  atomic promotion → completion marker written atomically INSIDE the
  promoted directory (the commit point) → formal verification. A crash at
  any point is fail-closed: staged directories never pass formal
  verification regardless of their contents, and a promoted directory
  without a valid marker is rejected for lack of it. The formal verifier
  cross-binds directory basename, run id, external HEAD/schema/run-id,
  summary, manifest and marker, and re-derives every payload hash from the
  bytes on disk. The formal CLI mode requires an explicit
  `--expected-head <full SHA>`; a non-formal diagnostic mode is available
  only under an explicit `--mode diagnostic` flag. Exports are
  bounded-memory (positions/trades stream in chunks, never a full row list)
  and per-file atomic (temp sidecar + rename).
- **v0.1.2 runs `20260906T135141.incomplete` and
  `20260906T131724.incomplete` are INCOMPLETE artifacts**: the former
  carries a stale `COMPLETED.json` (`verifier=inline_verify_pending_promotion`)
  written before verification crashed — exactly the fail-open marker the
  v0.1.3 protocol eliminates; it is retained as regression evidence and
  must NOT be cited as a formal baseline. Formal metrics come exclusively
  from verifier-passed v0.1.3 artifacts.
- **v0.1.1 run `20260906T120051` is an INCOMPLETE artifact**: it crashed
  mid-export (control recovery-1 daily positions) after writing
  `summary.json`, missing `equal_weight_v1_control_recovery_assumption_0`
  entirely and the recovery-1 control `daily_positions.csv`. It is retained
  as superseded evidence only and must NOT be cited as a formal baseline;
  formal metrics come exclusively from verifier-passed v0.1.3 artifacts.

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
| 3a. Lifecycle Date Semantics | ✅ Done / Frozen |
| 3b. Lifecycle Risk Policy | ✅ Engineering Validation |
| 3c. Systematic Lifecycle Event Data | Context ✅ (2020–2024); announcement coverage blocked (no `anns_d`) |
| 4. A-share Execution Engine | — |
| 5. Risk / Attribution | — |
| 6. Alpha Research Factory | — |
| 7. Paper / Live Trading | — |

The Alpha Research Factory will let agents generate and screen candidate alphas,
with promotion criteria enforced by the system's research protocol.
