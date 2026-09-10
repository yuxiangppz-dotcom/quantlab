# QuantLab Mainline Task: Daily Core Constructor v2

Status: active

## Goal

Remove the remaining duplicate Daily target-selection and weight-allocation implementation from `generate_daily_snapshot()` and make the materialized Daily target derive from the canonical fixed-count portfolio constructor already used by Forward Shadow and product-aligned research audit.

## Constraints

- No provider calls or Canonical writes beyond existing Daily behavior.
- No historical backtest semantic changes.
- No strategy promotion or performance claims.
- Preserve Daily output schema and deterministic rank ordering.
- Keep the semantic integrity validator as an independent fail-closed reconciliation layer.

## Acceptance

- Daily selection set and target weights come from `construct_daily_fixed_count_portfolio()`.
- Daily config is validated against the fixed-count product contract.
- Existing output columns and report fields remain compatible.
- Synthetic tests cover ties, residual cash under per-name cap, and all-NaN scores.
- Full repository CI, Ruff, diff-check, and optional research runtime pass before merge.
- Self-review confirms no duplicated economic selection/weight logic remains in Daily service.
