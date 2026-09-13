# Conservative liquidity amount projection for the fixed first replay session

Direct v3 commission. Start clean pushed d78f4ff6e47aad562ba109cbdb42515d7193ffb6;
PR112/master CI34731297081 passed. Shared loop paused; no executor writer and the
heavy lock is free. Push card before implementation or actual computation.

PR110 retained169 exact-fen ADV20 values. Its precision note separated142 unknown
code/dates into56 absent rows and86 finite fractional-fen values, many consistent
with floating-point unit-conversion tails. Do not rerun or alter that exact result.
Create one separate, explicitly conservative liquidity-capacity projection of the
same256 cohort and same20 sessions,2021-12-06 through2021-12-31. No different window,
fresh signal, provider/public-source request, canonical write or economic path.

For finite nonnegative canonical CNY amounts with observed scaled value at most
10^15 fen, use floor(Decimal(str(amount_CNY))*100). This is a lower bound relative
to the saved numeric observation, not a certified reconstruction of exchange raw
turnover. Never round upward. Missing, invalid, boolean, negative or out-of-range
data remains unknown. Exact daily integer-fen values remain unchanged. Require all
20 explicit calendar observations, then floor the sum of daily lower bounds/20.
Never compress missing sessions or apply this policy to prices, fees, account cash,
volume, corporate actions or executable fills. Source unit correctness still matters.

Bind the20 original canonical hashes plus old config, observations, report/proof
and precision-note receipts. Copy old exact ADV20 values solely for comparison.
Save all256 new daily masks/bounds and aggregate known counts, including the number
of fractional-fen rows floored. Keep all old inputs/results byte-identical and all
trading, cashflow and performance flags false. This projection is not automatically
installed in the historical controller; future consumption must name this policy.

At most20 minutes implementation; one actual and one separate Fraction-based proof
after clean implementation commit/push.300 seconds actual,2GiB RSS,8MiB output,
8GiB physical D reserve,one heavy lock. Test monotonic/downward bounds, exact values,
invalid/large/tiny values,dates,gap preservation and separation from the old exact
conversion. Full pytest/Ruff/diff, self-review, PR/CI/merge/exact-master CI and concise
findings. New view failure is terminal; do not overwrite it or retry old writers.
