# QuantLab Daily Product Status

This file distinguishes locally exercised product paths from planned work.
It is evidence notes, not a substitute for tests or an investment claim.

## Native Alpha158 audit (2026-09-11)

- All 158 pinned Qlib 0.9.7 expressions passed file/in-memory provider parity on
  five fixed codes, 2024-07-01 through 2025-06-30, plus 120 warm-up sessions.
  There were zero numerical or missingness mismatches. This is transport parity
  against the same native engine, supplemented by 13 independent analytic
  formulas and split/gap/future fixtures, not a second 158-formula implementation.
- Of 1,210 target code/date positions, 968 are within exact code lifecycles;
  908 have all 158 factors usable under the stricter complete-history rule.
  Canonical data and prior experiment outputs are unchanged. No research fits
  or performance claims were added. The Chinese workbench now displays per-factor
  coverage and the old/new code boundary; see [actual findings](alpha158_findings_zh.md).
- Historical milestone notes below describe their respective earlier runs.
  The earlier LightGBM system-library blocker has since been resolved; round two
  completed six fixed fits. Native Alpha158 correctness does not close existing
  portfolio costs, historical rules, corporate actions or execution blockers.

## Current delivery: guided workbench (2026-09-10)

- Seven Chinese pages now join the existing services into an explicit local
  workflow, including bounded updates, temporally admitted forward observation,
  cash-flow preview/import and immutable valuation access. See
  [中文快速上手](quickstart_zh.md) and [完整手册](user_guide.md).
- PR #36 excludes labels that cross chronological research split boundaries;
  PR #37 binds manual fill execution/report times and preserves legacy warnings;
  PR #39 resumes bounded daily updates from actual partition presence.
- The authorized five-session live update added 2026-09-10 core/index data,
  completed ST/suspension and limit context, and extended the calendar to
  2026-10-15. The seven checked preexisting 2026-09-09 partition hashes stayed
  unchanged. No older core/index partition gaps were reported in this inventory.
- LightGBM/Qlib runtime smoke now passes after the user-authorized installation
  of libgomp1. A smoke fit does not imply a validated market model.
- Old shadow records without bound generation times are retained but excluded
  from prospective counts. No retrospective artifact is relabelled as OOS.
- Existing account modes cannot be changed by reimport; opening snapshots are
  preserved by fingerprint while journals retain their original basis binding.
- Final test counts, browser/launcher evidence, clean commit and CI identifiers
  are recorded in the delivery PR and local delivery report after validation.

Corporate-action postings, arbitrary historical account cutoffs, full performance
eligibility and broker integration remain incomplete. No strategy was promoted.

## Historical milestones (not current readiness evidence)

The following notes describe earlier runs at their recorded time. In particular,
the four-page UI, old data date, LightGBM runtime blocker and old test counts below
have been superseded by the current delivery above.

| Milestone | Status | Local evidence | Next action |
|---|---|---|---|
| M1 Daily workflow + local UI | Implemented and locally verified | `quantlab doctor`, controlled Provider update, idempotent `quantlab daily`, four-page Streamlit UI, CSV/HTML cache, 926-test regression | Continue to bounded M2 research capability |
| M2 Multi-factor + optional Qlib/ML | Portfolio audit and Qlib adapter exercised; LightGBM system runtime blocked | 10-candidate batch, daily/weekly cadence audit, six-candidate Portfolio Translation Audit, exact KMID/KLEN subset mapping, real Qlib StaticDataLoader smoke | Keep candidates unpromoted; install `libgomp1` only with user system authority |
| M3 Account input + reference rebalance | Implemented and locally verified | Strict account CSV import, 200k demo, raw-close reference plan, BUY/SELL/HOLD/NO_TRADE UI + CSV export | Add manual-fill journal and portfolio tracking |
| M4 Manual fills + tracking | Implemented and locally verified | Preview/commit broker-fill CSV, durable fingerprinted journal, same ExecutionLedger cash/T+1 replay, plan-vs-fill comparison, conservative reference valuation | Run final end-to-end acceptance |
| M5 v1 acceptance | Implemented with clean-HEAD acceptance gate | `quantlab accept`, local-only UI health, versioned configs and outputs | Hand off local Daily v1; keep strategy promotion user-controlled |

## M1 exercised facts

- Canonical inventory was inspected without loading the 60 GB data tree into
  memory. `daily`, `adj_factor`, `daily_basic`, and `index_daily` each had 164
  2026 partitions through 2026-09-04 before the first update attempt.
- A real local snapshot was first generated for 2026-09-04 from 21 required
  sessions in about 2.6 seconds with approximately 391 MiB peak RSS. A
  controlled Provider update then added complete 2026-09-07 through 2026-09-09
  core/index partitions and complete ST/suspension context. The 2026-09-10
  payload was empty before market close, so no 2026-09-10 core partition was
  written and the final exercised snapshot is visibly dated 2026-09-09.
- A second run reused the identical content fingerprint and did not duplicate
  downloads, bookkeeping, or report directories.
- The status remains visibly stale when calendar coverage does not reach the
  requested Shanghai date. The tool does not call the old snapshot “today”.
- The backtest page reads compact manifest-bound daily record files from the
  completed v0.1.3 artifact; it does not load the artifact's 97 MB summary on
  every UI refresh.
- WSL and Windows launch paths both served on `127.0.0.1`; the health endpoint
  returned `ok`, all four pages passed Streamlit AppTest, and Ctrl+C stopped the
  server without a traceback.
- Milestone regression: `926 passed`, `ruff check .` clean, and no diff-check
  errors (the repository retains pre-existing CRLF normalization warnings on
  five untouched historical tests).

Generated product files live under `data/products/` and are intentionally
Git-ignored. They are local cache/output, not Canonical truth.

## M2 exercised facts

- `daily_factor_research_v1/20260910T012031` ran on local QuantLab Canonical
  data from 2020 through signal date 2026-09-02. Runtime was about 147 seconds
  and peak RSS about 2.16 GiB.
- Ten pre-registered candidates were all retained. Four crossed the simple
  validation RankIC thresholds (`momentum_1d`, `intraday_strength`,
  `low_amplitude`, `float_ratio`); the transparent combination had validation
  mean RankIC about 0.031 across 24 monthly observations. These are research
  diagnostics, not strategy profitability or promotion evidence.
- `daily_factor_research_v1/20260910T103631` reran the fixed batch in about 145
  seconds with about 2.17 GiB peak RSS. Qlib 0.9.7 installed successfully and
  its real `StaticDataLoader(DataFrame)` accepted 1,000 QuantLab-derived rows.
  A separate 200-row smoke also passed. No Qlib sample market data or Qlib
  backtest was used.
- The explicitly named Alpha158 subset contains only two verified mappings:
  `KMID == intraday_strength` and `KLEN == -low_amplitude`. It is not presented
  as the complete Alpha158 feature set.
- LightGBM 4.7.0 is locked in the `research` extra, but its Linux wheel cannot
  load because the host lacks `libgomp.so.1`. The run records the OSError and
  produces no fake predictions or substitute model.

## Daily v1.1 Alpha & Data upgrade evidence

- The reference plan now consumes constrained cash by target alpha rank and
  only then by instrument id; it never funds buys from expected sale proceeds.
  Acceptance binds the current account fingerprint, Daily content fingerprint,
  and plan CSV hash, and includes a pure manual-tracking fixture smoke.
- Real capability probes succeeded for `stk_limit`, `fina_indicator_vip`, and
  `dividend`. Controlled enrichment stored 5,217 SH/SZ A-share limit rows for
  2026-09-09, 6,078 financial version rows for 2024-12-31, and 97 explicitly
  scoped dividend rows for `000001.SZ`. An identical rerun reused the paths.
- Financial version history is not strict historical PIT: 944 instrument-period
  groups had multiple versions and the endpoint supplies `update_flag` but no
  revision timestamp. No fundamental alpha was added. Observations may become
  prospective features only from their first local `available_from`.
- `portfolio_translation_audit_v1_1/20260910T100701` compared six fixed
  candidates with the true equal-weight control for 2020–2024. All 1,211 return
  intervals were covered and symmetry checks passed. `float_ratio` had active
  CAGR about 2.13%, while transparent combo had about 0.93%; both remain
  retrospective/test-observed and unpromoted.
- The Discovery-only daily/weekly cadence audit kept the sign of mean RankIC for
  all six fixed candidates. It did not use Validation/Test for tuning.
- Forward Shadow began at signal date 2026-09-09 for the return-20D baseline
  and transparent combo. Predictions are content-addressed and immutable; no
  20-session evaluation is mature and no broker execution is implied.

Official semantics were checked against the Tushare documentation for
[`stk_limit`](https://tushare.pro/document/2?doc_id=183),
[`fina_indicator`](https://tushare.pro/document/2?doc_id=79), and
[`dividend`](https://tushare.pro/document/2?doc_id=103). The exact Alpha158
mapping is tied to Microsoft Qlib's
[`Alpha158DL` source](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py).

## M3 exercised facts

- Account snapshots accept a complete, timezone-aware CSV and store CNY as
  integer fen. Duplicate instruments, unsafe account IDs, sub-fen currency,
  `sellable_quantity > quantity`, and partial invalid imports fail before the
  existing snapshot is replaced.
- The local `demo_200k` account is explicitly marked `demo_simulation`; it is
  never shown as the user's assets. Re-import and plan generation are
  fingerprinted and idempotent.
- The plan values the complete account using the daily report's raw signal-date
  close. A held instrument without that close blocks the complete valuation.
  Buys use current cash only; anticipated sell proceeds never fund another leg.
- The plan distinguishes BUY, SELL, HOLD and NO_TRADE. It respects imported
  sellable quantity, treats ST/suspension context conservatively, and identifies
  target budgets below minimum quantity as NO_TRADE rather than falsely saying
  the position is already at target.
- Every plan remains `reference_only_pending_review`: the signal-date close is
  not an order limit, next-session status is unknown, and the displayed fee is
  only the user-reported commission estimate—not a verified all-in charge.

## M4 exercised facts

- A `manual_tracking` account can preview a complete broker-fill CSV before any
  write. Each row carries an actual broker trade id, trade/report date, side,
  quantity, Decimal price, explicit gross amount, and actual fee. The gross
  amount must equal price × quantity exactly in integer fen.
- Imported fills are a typed external fact in the existing `ExecutionLedger`;
  no QuantLab order, broker submission, or daily-bar fill is fabricated. The
  same cash, lot, oversell, event-ordering and T+1 invariants are applied.
- The full combined event set is replayed before one atomic journal write. A
  bad row leaves no journal; the same file is an exact no-op on re-import; a
  broker trade id reused with different economics is rejected.
- The current account view is the immutable opening snapshot plus that journal.
  A new opening snapshot produces a new fingerprint namespace, preserving but
  not silently applying the old history.
- The UI displays imported fills, actual cash/positions/fees, and the difference
  from the newest plan for its intended date. Reference performance appears
  only when the daily price date covers every fill and both opening/current
  holdings can be valued; otherwise it is unavailable with a reason.
