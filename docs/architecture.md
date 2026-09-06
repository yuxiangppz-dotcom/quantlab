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
position reconciliation, asset identity, and frozen-position invariance. These
checks reconcile against the **actual book state** (not intermediate summary
values), and reject non-finite participants, residuals or scales. The solver
residual is kept separate; a residual exceeding a normalized positive-NAV
tolerance (or a non-finite / over-tolerance negative cash or position) raises a
structured `accounting_error`. Each session's records are committed atomically
only after both books validate; a failing session is kept in a separate
`failed_attempts` structure and never enters the valid prefix.

Unsupported lifecycle events are detected by a separate `LifecycleMonitor` from
canonical `Security` / `SecurityCodeChange` data, independently for both books
and decoupled from price availability:

- `delist`: a held position blocks once the instrument is invalid under the
  active boundary interpretation (see *Lifecycle Date Semantics* below).
- `code_change`: the old instrument is invalid from `effective_date`
  (`trade_date >= effective_date`).
- A static conflict (an instrument having both a delist date and a code-change
  date) is reported as a structured diagnostic but only blocks once one of the
  two invalidation conditions actually fires.

Lifecycle constraints persist independently of event-log deduplication: a
blocked held position stays frozen (and is not re-marked by later quotes), and
a rejected new target is rejected again on every later execution. Accounting
statistics are reported per book (gross and net), and absolute vs. relative
residual maxima are tracked independently with their own date/book. A
centralized `build_report` function gates performance publication so that
`metrics` are produced only when the strict run completed, had no unsupported
event or accounting error, and the inputs were reproducible.

Two run modes exist: `strict` stops before the first unsupported event and
reports `valid_through`, `first_blocking_event`, and `metrics = null`;
`diagnostic` continues with `diagnostic_only` marking and never returns a valid
completed result. It is a research simulator, not an execution simulator — a
blocked run never claims a completed full-period performance.

### Lifecycle Date Semantics

Four time concepts are kept distinct: `termination_decision_date` (the decision
does not immediately stop trading — a delisting-arrangement period may follow),
`last_trading_date` (only when evidence exists, never `delist_date - 1`),
`delisting_effective_date` (when the instrument actually becomes invalid), and
`available_from` (when the strategy may use the announcement). A position's last
mark is only `last_observed_price_date`.

Two delisting boundary interpretations are supported:

- `legacy_delist_date_inclusive` is the **compatibility baseline**:
  `delist_date` is valid through its date, so the instrument is invalid on
  `trade_date > delist_date`.
- `delist_date_is_first_invalid_v1` is the **candidate interpretation**:
  `delist_date` is the delisting effective date, so the instrument is invalid on
  `trade_date >= delist_date`.

Code-change events keep their own `effective_date` rule in both modes, and every
`LifecycleMonitor` is constructed with an explicit mode. The legacy/v1 date
comparison lives in exactly one place —
`is_instrument_invalid_on_delist_boundary(trade_date, delist_date, mode)` —
which both `LifecycleMonitor` and the theoretical boundary table delegate to.

### Lifecycle Date Semantics — frozen

The lifecycle date-semantics experiment is **frozen after closure**. The
legacy/v1 × baseline/admission_v2 × strict/diagnostic comparison is sufficient
to compare the two boundary interpretations, so no further paths, fact-batch
expansion, or date-interpretation modes are added:

- `legacy_delist_date_inclusive` remains the compatibility baseline;
  `delist_date_is_first_invalid_v1` remains the candidate interpretation and is
  **not** promoted to canonical truth in this round.
- The research backtest can clearly report a `blocked_by_unsupported_event`
  lifecycle event; full delisting settlement and corporate-action handling are
  left to a future Execution / Corporate Action layer.
- Unverified facts stay `unknown` and do not block engineering progress; fact
  coverage is expanded only when a later real feature needs it.

### Lifecycle Admission (shadow)

Delisting facts distinguish two time semantics: `effective_date` (when the
market event takes effect) and `available_from` (when the strategy may use the
information). A **trusted** fact additionally requires both content
verification and independent public-time verification; a verified public date
with no intraday time is conservatively available from the next trading day
open. A document signature date or a URL path date alone is not a verified
public time.

The shadow policy `no_new_exposure_after_termination_decision_v1` labels each
positive target `restricted` (a trusted, available termination decision exists)
or `unknown` (insufficient trusted coverage — never a claim of safety).

As of v0.2 this is also an **enforcement experiment**: a restricted instrument's
new exposure is capped at its pre-rebalance (post-mark) amount inside the
self-financing solver — buys and refills are forbidden, sells per the original
target and freeze rules are allowed, and no forced liquidation occurs. The
baseline (unrestricted) and admission paths run on identical inputs and are
compared; the same fingerprinted fact snapshot drives both.

v0.3 derives `available_from` only from the full canonical calendar (with
open/closed status and completeness checks — announcements outside the covered
range or a calendar with missing middle records are rejected), reports a fixed
10-instrument fact batch in a separate facts version, and runs three paths
(baseline, admission+original facts, admission+batch facts) with a generic
buy-rejection evaluation (`true` / `false` / `not_evaluated`).

### Lifecycle Risk Policy v0 — engineering validation

`exit_after_termination_decision_v1` is a persistent point-in-time risk
overlay. Once a trusted termination-decision fact is available, the affected
instrument has an exposure upper bound of zero. A held position is marked to
the current session price and sold at that close when a valid price exists; if
the price is missing, the position remains frozen and the exit instruction is
retried on every later session. The overlay runs before a scheduled Alpha
rebalance, prevents entry/refill/re-entry, and leaves removed target weight as
cash rather than renormalizing other names.

Gross and net books execute the same risk decision independently from their
own position values. Gross pays no fee; net pays the configured proportional
fee on its actual forced-sell notional. Forced exits and normal rebalance legs
are combined in the session-level final-ledger reconciliation.

This policy is an engineering risk control, not a complete Corporate Action
Engine. The real comparison uses the frozen
`delist_date_is_first_invalid_v1` candidate boundary; that choice remains a
candidate interpretation and is not promoted to universal Canonical truth.

### Systematic Lifecycle Event Data v0

The Canonical layer now includes a raw `anns_d` announcement index (one atomic,
resumable Parquet file per calendar date), `SecurityLifecycleEvent`, and ST /
suspension context records. Raw provider records are retained separately from
normalized events. `event_id` and content fingerprints are deterministic;
events retain source identifiers/URLs, title, classifier reason, verification
status, optional event time/effective date, and PIT `available_from`.

The only v0 risk event is `termination_decision`. The title classifier is
versioned (`termination_decision_title_v1`): clear formal decision wording is
trusted, ambiguous termination wording is `review_required`, and risk warnings,
procedures, hearings, arrangement periods and effective notices are context,
not automatic exits. Daily availability is conservatively the first market
session after `ann_date`; `rec_time` is retained but never makes same-day use
legal.

Lifecycle context v0.1.1 has a separate readiness from announcements: `stock_st`
is synchronized by open `trade_date` below its 1,000-row limit, and `suspend_d`
by open `trade_date` below its 5,000-row limit. Every response must contain only
the requested date. Suspension context stores raw `S` / `R` records and optional
intraday timing; it never derives resume dates or lifecycle intervals. The
previous unversioned suspension dataset was generated with an invalid parameter
and is retained only as untrusted historical output; v0.1.1 reads only the
versioned date partitions.

The account capability audit on 2026-09-05 found `anns_d` unavailable at the
declared 5,000-point tier. Consequently the source is explicitly
`blocked_by_missing_anns_d_permission`: no scraped or manual announcement data
is inserted. The existing verified manual facts remain golden regression
evidence and compare automatically against systematic events once source access
exists. With the corrected `trade_date` parameter, the `suspend_d` probe
returned a scope-valid, below-limit response, and `lifecycle_context_readiness`
is `complete_for_2020_2024` (1,212/1,212 open sessions for both `stock_st` and
`suspend_d`) — tracked separately from the announcement-source block.
ST/suspension absence is unknown context, never evidence of tradability.

### Performance Baseline & Benchmark Correctness v0.1.1

The formal 2020–2024 baseline (`performance_baseline_benchmark_correctness_v0_1_1`)
answers "does stock selection add value over the same universe held
equal-weight under identical execution/risk/settlement?":

- **Risk policy first; settlement second.** The primary path runs
  `exit_after_termination_decision_v1` over a validated, fingerprinted fact
  snapshot. Delisting settlement is an **explicit scenario** applied only to
  *held* instruments that cross a `delist` boundary (settlement is
  delist-only; code_change/conflict still strictly block). The scenario
  models recovery as an assumption over `[0, last_mark]`
  (`recovery_assumption_1` / `recovery_assumption_0`) — it is never a claim
  of actual fills, actual liquidation economics, or guaranteed recovery.
- **The true equal-weight control is the primary benchmark.**
  `equal_weight_v1_control` is a real self-financing portfolio whose
  eligibility is built from the PIT security master (`list_date`, frozen
  delist/code-change boundary semantics, the V1 SH/SZ A-share predicate) —
  never from the price-backed research universe and never from current
  `list_status`. A signal-date-eligible but unpriced instrument stays in the
  1/N denominator; the engine leaves the unfilled weight in cash and keeps
  held suspended names frozen at their stale mark. The control receives its
  own formal report (validity gates, full metrics, settlement disclosure),
  and a control that fails any gate blocks the primary comparison.
- **Index attribution is implemented** as a secondary layer vs
  000300.SH / 000905.SH / 000852.SH on raw price-index closes
  (`index_return_basis = price_index_close`), with per-instrument
  session-coverage audits that fail the attribution on missing sessions.
- **Active metrics share one relative-wealth path**: cumulative active
  return, active CAGR and active MDD are all derived from strategy NAV /
  control NAV. `prod(1 + s - b)` is retained only as
  `arithmetic_active_nav_diagnostic` (it is not even a valid wealth process
  under large benchmark moves).
- **Formal reproducibility requires a clean git workspace** before and after
  the run, an unchanged HEAD, and stable code/data content manifests. Dirty
  or moved-HEAD runs can never publish formally reproducible,
  performance-valid metrics; `data/experiments/` output is git-ignored and
  never counts as workspace dirt.
- **Strategy/control symmetry is audited on the full run specification**
  (calendar, signal schedule, execution lag, cost, missing-price policy, run
  mode, lifecycle boundary mode, lifecycle monitor snapshot, risk policy id,
  fact snapshot, settlement recovery and fee, initial NAV, requested period,
  annualization); only target construction may differ.
- **Lifecycle date semantics remain frozen**: the formal baseline runs the
  disclosed `legacy_delist_date_inclusive` boundary for both strategy and
  control (chosen, disclosed baseline mode — not re-selected from results);
  `delist_date_is_first_invalid_v1` remains a candidate. Forced-exit
  reporting uses deduplicated `risk_policy_statistics` (unique instruments,
  decision occurrences, executed exits, pending exits, prevented entries)
  instead of raw audit-row counts.

### PIT Code-Lineage Identity (v0.1.2)

A `SecurityCodeChange` fact defines an explicit PIT identity interval for one
instrument lineage:

- **Before the `effective_date`**: only the OLD instrument id exists. If the
  old id is absent from the security master (the vendor dropped it when
  creating the successor), it is still synthesized as PIT-eligible from the
  lineage fact (`original_list_date <= as_of < effective_date`) — an
  eligible-but-unpriced instrument handled by the engine's missing-price
  rules (unfilled weight stays cash). It is never silently dropped and never
  silently aliased to the successor's backfilled prices without a frozen
  alias policy.
- **From the `effective_date` (inclusive)**: only the NEW id exists. The
  successor master row's `list_date` may be the vendor's backfilled
  `original_list_date` (production case: `300114.SZ -> 302132.SZ`, effective
  2025-02-17, successor master `list_date=2010-08-27` with 2020–2024
  backfilled bars) — that backfill is NOT a visibility fact, so the future
  successor id never enters 2020–2024 eligibility.
- Old and new identities of one lineage can never be co-eligible on the same
  session (no double counting). The 2018/2019 completed lineages therefore
  use only successor ids inside 2020–2024.

`code_change_lineage_audit` records per lineage: old/new id, effective date,
per-formal-signal-date eligibility, `future_successor_violation_count` (must
be 0), `old_new_overlap_count` (must be 0), and
`eligible_but_unpriced_predecessor_count`. The formal run fails hard on any
violation.

### Formal Artifact Staging and Verification (v0.1.2)

A formal run is published in two phases:

1. **Staging** — every file (summary, manifests, lineage/facts audits, and 14
   export groups × 13 standard files) is written into
   `data/experiments/<schema>/<run_id>.incomplete/`. Exports stream bounded
   chunks (positions/trades never materialize as a full row list) and every
   file is written to a `.tmp` sidecar first and `os.replace`d into place
   after a clean close, so a crash never leaves a half-written formal file.
2. **Promotion** — an SHA-256 `artifact_manifest.json` (registry, per-file
   hash/size/row-count/header) is built, a `COMPLETED.json` marker is written
   (schema, HEAD, manifest hash, verifier result, `formal_run_valid: true`),
   and only after `verify_formal_artifact` passes is the staging directory
   atomically renamed to `<run_id>/`.

`verify_formal_artifact` (and the standalone `scripts/verify_formal_run.py`)
fail hard on: any missing required file, hash/size/header/row-count
mismatch, a missing control recovery bound, a missing or invalid completion
marker, a manifest inconsistent with the directory, or summary schema/HEAD
disagreement. A failed or interrupted run keeps its `.incomplete/` staging
with an explicit `INCOMPLETE.json` marker and never publishes a
formal-looking final directory; `performance_valid` and `formal_run_valid`
never hold on an incomplete artifact.

### Known limitations and deferred work

- **Lifecycle fact coverage is incomplete.** Trusted termination-decision facts
  cover only part of history. For example, `002509.SZ` remains
  `searched_unresolved` / insufficient trusted fact coverage in the current
  snapshot. `unknown` must never be interpreted as safe, and the risk policy
  cannot exit an instrument for an event it did not know about.
- **Systematic announcement coverage is source-blocked.** The data model and
  audit pipeline are implemented, but current `anns_d` permission prevents
  systematic historical announcement ingestion and risk-policy source-mode
  comparison. This is a capability limitation, not evidence that missing events
  are safe.
- **Delisting settlement is a scenario, not actual economics.** The explicit
  held-delist settlement scenario models recovery as an assumption over
  `[0, last_mark]` (`recovery_assumption_1` / `recovery_assumption_0`). It is
  never a claim of actual fills, actual liquidation proceeds, or guaranteed
  coverage of real economic recovery; corporate actions beyond delist
  settlement (mergers, cash buyouts, successor joins) remain unsupported.
- **The v1 delist boundary remains a candidate.** The date-semantics experiment
  is frozen; `delist_date_is_first_invalid_v1` is not a Canonical universal
  truth and this phase does not reopen `>` / `>=` interpretation work. The
  formal baseline runs the disclosed `legacy_delist_date_inclusive` mode for
  both strategy and control.
- **Termination announcement coverage is still source-blocked.** The
  `anns_d` permission gap stands; `unknown` facts are never treated as safe,
  and the risk policy cannot exit on events it did not know about.

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
