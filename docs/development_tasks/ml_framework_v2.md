# ML framework v2 — direct user commission

Starting HEAD: `ad5e16147929e8f6a35ede10936086c1adf691d6`.
Authority: the user directly requested framework/logic changes and a local completion prompt.
Single isolated workspace, with no scheduled executor/reviewer writing it.

## Delivered

- New `quantlab ml export-history/check/train/replay` entry and `config/ml_daily_v2.json`.
- Exact next-close labels, PIT feature cutoff, partial missingness, purged monthly folds,
  rank/zscore targets, equal-session sample weights and train-only Ridge preprocessing.
- Ridge and native LightGBM rank-MSE baselines; optional binary/LambdaRank, validation
  early stopping, serialized models, immutable experiment intent and source hashes.
- Buffered rank portfolio from actual holdings, holding age, replacement/turnover budget,
  industry/name/gross limits, cash, small-order filter and explicit risk-reduction path.
- Integration with existing quantity kernel/scheduler: raw prices, integer shares,
  dated fees, minimum commission, T+1, limits, capacity, failed/partial attempts and stops.
- Independent capital ledgers, signal diagnostics separated from scenario NAV, preserved
  missing-label coverage, raw return tails and explicitly named membership churn.
- Read-only bridge to existing sealed history with caller-supplied PIT context.
- README migration route, full Chinese design/input documentation and local completion prompt.
- CI optional-runtime job now also exercises all new ML tests.

## Validation and fixes during review

- Locked `uv sync --frozen --extra research --extra qlib` succeeded.
- Qlib/LightGBM + new workflow suite: **32 passed** (22 new tests, 10 existing runtime tests).
- Full initial default run: 3526 passed, 10 skipped, 1 failed (existing local UI ownership test).
- Investigated the UI failure without weakening ownership checks: the cloud subprocess PID
  is namespace-local while the exposed `/proc` cannot resolve it (`start_ticks=None`).
  A directly probed Streamlit health endpoint returned HTTP 200 and `ok`.
- Full run including optional runtimes, deselecting only that environment-incompatible test:
  **3542 passed, 1 deselected**. The GitHub CI workflow still runs this UI test normally.
- `uv run ruff check .` and `git diff --check` passed.
- Synthetic checks cover label endpoint holes, label maturity/purge, future-label perturbation
  invariance, partial missing features, native objectives, immutable bundles and history export,
  rank buffers, holding age, turnover limits, minimum fees, non-linear capital effects,
  failed sales, unknown fee stops, missing held marks and execution price-gap risk controls.
- Review tightened availability timezone validation, prevented future/unknown fit cutoffs,
  separated membership changes from traded turnover, corrected rank-bin boundaries, and
  added saved-LightGBM prediction parity plus code/runtime/input rechecks.

## Deliberate limits / local follow-up

No local market data was available. No real IC, PnL, capacity or strategy superiority was
claimed. The workflow remains retrospective research and hypothetical quantity accounting.
Historical industry/universe evidence, dated fees/rules, market constraints and corporate
cash/share processing must be bound and validated locally. Missing evidence must not be
replaced with true/zero. The local prompt describes those inputs, integration seams and gates.

Legacy frozen experiments, lifecycle accounting, strategy promotion rules, account state and
broker authority were preserved. No provider calls, Canonical writes, external orders, secrets
or generated experiment data are included in this change.

Recommendation: review this isolated code change, then complete local evidence admission and
one preregistered baseline comparison before choosing a model for forward observation.

## Direct follow-up commission (2026-09-16)

The user authorized completion of the data-independent framework after comparing
Qlib, LEAN/RQAlpha, NautilusTrader, Zipline and vnpy.alpha. No concurrent executor
was present in this isolated workspace. Starting follow-up HEAD: d80613b.

Completed first correction: causal prior-close admission (no assumed same-auction
sale credit), diagnostic-only post-fill exposures, score-frozen quantile membership.
Completed accounting/operations increment: explicit net-cash dividend receivables,
record-date entitlements, bonus share locks and integral split scenarios; stale
ex-date orders cancel rather than guessing exchange adjustments. Rights, fractional
settlement, delisting and historical tax verification remain explicit data/adapter
boundaries. Date-filtered training, immutable monthly completion, daily full-account
checkpoints, input-bound resume, atomic JSON and streaming market-day reads are now
implemented. Frozen legacy schedulers remain unchanged.

Synthetic verification: 31 ML tests passed before the next integration increment.
No provider calls, canonical writes, live orders or performance certification.

### Completed integration and verification

The unified CLI now includes prepare-inputs, lineage-aware check/train, resumable
replay, report, model register/activate, label-free predict, archived-signal shadow,
init-study and status. Actual inference and activation times prevent retroactive
signals from being called forward. Source lineage, revision selection, common-period
benchmarks/style coverage and block-bootstrap IC uncertainty have dedicated tests.

A final execution review added an OPTIONAL pre-funded buy cash budget to the shared
scheduler. Existing callers retain original semantics. ML explicitly uses the budget,
so even price gaps cannot turn same-close sale proceeds into extra purchase funding.

Validation on the final integration source:
- 41 ML tests (including CLI train/replay/report/status, inference parity, both kinds
  of interruption recovery, source revision timing and pre-funded cash regression).
- Full locked optional-runtime suite: 3561 passed, 1 deselected; 60 existing warnings.
- The unfiltered run reproduced only the known cloud PID-namespace local-UI failure
  (3550 passed, 10 optional tests skipped before the final additional cash test).
  No UI process-ownership guard was weakened. GitHub CI runs the unfiltered gate.
- Ruff and whitespace checks passed. No real market dataset was available or used.

The delivery documents are docs/ml_framework_completion_zh.md,
docs/ml_daily_v2_zh.md, and docs/ml_v2_local_completion_prompt_zh.md. Remaining local
work is evidence binding, special-event/tax adapters justified by actual sources,
real-data accounting checks, model/cost/capacity experiments and forward observation.
No provider calls, canonical writes, broker orders or performance claims were made.
