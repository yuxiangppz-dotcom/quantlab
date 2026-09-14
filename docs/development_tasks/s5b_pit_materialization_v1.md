# S5-B v1.1 PIT feature materialization

Issue: #141. Starting master: `3ce7f74092b0774f014771f11798fc42e67efa9c`.

This task freezes the causal formulas that turn caller-supplied close and volume
series into the S5-B v1 observation. The preceding 60-close comparison excludes
the endpoint being tested; the breakout reference excludes the current close;
every other trailing window ends at the decision date. Data after `as_of` must
not influence values, issues or fingerprints.

Required outputs are the complete S5-B observation, deterministic field-level
issues and a content-bound source fingerprint. Missing sessions, duplicate
dates, invalid values, zero denominators and insufficient history remain
explicitly unknown.

The exact formulas and boundaries are frozen in Issue #141 before any outcome
inspection. This task may not change S5-A, S5-B states or thresholds, access a
provider, write Canonical, evaluate returns, fit/tune a model, promote a
strategy, mutate an account or create an order.
