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
