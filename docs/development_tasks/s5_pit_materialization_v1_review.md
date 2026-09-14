# S5 v1.1 Self-Review

Scope: Issue #131, branch `chatgpt/s5-pit-materialization-v1`.

## Correctness review

- The S5 v1 kernel remains unchanged: this branch only materializes its declared inputs.
- All calculations use a common session calendar ending exactly at `as_of`; no future point can enter a feature window.
- Required windows are explicit: 120 closes for drawdown, 79 for 20 trailing-60 low endpoints, 272 for 252 rolling 20-return volatility endpoints, 21 for 20-session return and volume-return alignment, 20 for MA20, 25 for MA20 slope.
- The new-low loop was independently checked to use exactly offsets 59..78, i.e. exactly the final 20 eligible endpoints. Ties count as lows as frozen in the task card.
- Volatility uses simple returns and sample standard deviation (`statistics.stdev`, ddof=1). With 272 closes there are 271 daily returns and exactly 252 rolling 20-return volatility endpoints.
- No price forward fill is performed. Missing/nonpositive required prices, missing/invalid volume, or one-sided up/down windows remain unknown.
- Stock volume is aligned to each daily-return endpoint, not the starting price date.

## PIT / lineage review

- Industry membership is resolved only from evidence active on the decision date. Missing, unverified or conflicting active evidence yields unknown rather than an inferred current classification.
- Research eligibility requires the exact decision date and `pit_verified=True`.
- The materialization fingerprint binds the actual consumed price/volume prefix, source identities, calendar contract, historical membership evidence and decision-date eligibility.
- Future series points and membership records starting after `as_of` are excluded from the earlier fingerprint.
- A membership end date after `as_of` is normalized to `after_as_of_or_open`, so later knowledge of a future end date cannot rewrite an earlier fingerprint; an end date already effective by `as_of` remains bound.
- Historical source or membership changes alter the fingerprint.

## Determinism / authority review

- Caller row order is normalized; duplicate identities/dates fail instead of last-write-wins behavior.
- Outputs are immutable dataclasses and do not mutate caller inputs.
- Output remains `research_only=True`, `performance_claim=False`, `broker_order_authority=False`.
- No provider calls, Canonical writes, historical S5 returns, model fits, Forward registration, strategy promotion, account mutation or orders are introduced.

## Research-validity boundary

This PR intentionally does not claim that S5 works. A trustworthy retrospective diagnostic still requires a defensible historical industry taxonomy/membership source and a separately frozen evaluation protocol before any S5 outcome is inspected. Threshold rescue or post-result parameter search remains prohibited.
