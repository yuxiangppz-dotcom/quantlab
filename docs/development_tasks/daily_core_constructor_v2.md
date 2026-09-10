# QuantLab Mainline Task: Daily Core Constructor v2

Status: active

## Goal

Remove the remaining duplicate Daily target-selection and weight-allocation logic from the Daily generator. The materialized Daily selection set, target weights, and cash weight must derive from the canonical fixed-count portfolio constructor already used by Forward Shadow and product-aligned historical audit.

## Constraints

- No provider calls or new Canonical writes.
- No historical backtest semantic changes.
- No strategy promotion, performance claims, execution mutation, or broker paths.
- Preserve the Daily output schema and deterministic rank ordering.
- Keep the semantic integrity validator as an independent fail-closed reconciliation layer.
- Do not weaken immutable Daily publication.

## Acceptance

- Daily config is validated through the fixed-count product contract before economic materialization.
- Selection set and target weights derive from `construct_daily_fixed_count_portfolio()`.
- Residual cash comes from the returned `TargetPortfolio`, not duplicated arithmetic.
- Existing ranking/target columns and report fields remain compatible.
- Synthetic tests cover deterministic ties, per-name-cap residual cash, all-NaN scores, and row-order invariance.
- Full repository CI, Ruff, diff-check, and optional research runtime pass before merge.
- Self-review confirms no duplicate economic selection/weight allocation remains in the Daily generator.
