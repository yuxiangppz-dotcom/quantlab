# Research Protocol

1. **Feature / Information**: any alpha feature may only use information available
   at (and before) time `t`. No future data in features.

2. **Label**: `future_return_*` columns are labels only. They are used to evaluate
   a signal at time `t` and must never feed into a feature.

3. **Discovery first**: hypotheses and parameters (e.g. lookback) are formed on the
   Discovery period only.

4. **Validation confirms**: the Validation period confirms a hypothesis before the
   Test period is inspected.

5. **Test is observed**: once the Test period has been inspected, any new hypothesis
   derived from those results can no longer claim Test as an untouched holdout.
   (`test_observed = true` is recorded in experiment metadata.)

6. **New out-of-sample**: genuine forward evidence is specific to a frozen model
   and its timely generated and registered predictions. No fixed calendar date
   makes all subsequent data untouched. Data already inspected remains observed;
   a model trained later cannot backfill historical predictions as forward evidence.

7. **No hindsight lookback search**: do not scan the full history, pick the best
   lookback, and report a single best result as if it were pre-registered.

8. **Period isolation**: a future label used in an evaluation period must fall
   entirely within that same period. A Discovery label must not use Validation
   data, and a Validation label must not use Test data (historical feature
   lookback may still cross `period_start`, since that information was already
   available at signal time `t`).
