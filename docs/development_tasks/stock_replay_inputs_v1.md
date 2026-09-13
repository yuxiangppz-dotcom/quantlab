# Fixed S4 first-session input bridge v1

Direct v3 authority. Clean pushed base d4f9f536cf60ea976a78dfb1562ed21de7a2efa1,
PR109 and exact-master CI34728930455 passed; no concurrent writer/heavy worker.
Freeze and push this card before source materialization or implementation.

Build a reusable strict adapter from canonical DailyBar values (already normalized
to shares and CNY) to the integer-share/fen inputs of the new research scheduler.
Handle a complete prior 20-session amount mean ending at the decision date; do not
compress gaps, substitute execution-day liquidity into decisions, multiply canonical
amount/volume by provider units again, round off-grid prices or invent missing data.
Finite nonnegative amount observations must be exact to fen and volume to shares;
unrepresentable values stay unknown. Average amount may be conservatively floored
to integer fen with the rule explicit. Validate full OHLC relationships.

Bind one fixed actual materialization for all the existing 256 cohort codes:
decision 2021-12-31, intended execution 2022-01-04, next session 2022-01-05. Read
only the previous 20 daily partitions ending at the decision date, the execution
daily/price-limit/ST/suspension partitions, the saved S4-A signal row on that decision
date and already sealed calendar/selection/parent reports and proofs. The prior20
dates come from the frozen funding calendar. This is 24 canonical Parquet partitions
(20 prior daily, one execution daily, three execution context) plus one saved S4
Parquet; at most 32 MiB new ignored output, 2 GiB worker RSS, 600 seconds actual worker,
8 GiB physical D reserve. Implementation/check segment maximum35 minutes.

All exact file hashes/bytes and source/code versions must be sealed before the actual
materialization. Reuse saved original source evidence, do not rerun old diagnostics,
cohort/terms/proof writers or scan other historical dates. No source website or
provider request, canonical write, model, formula, strategy identity, economic path,
order, portfolio allocation, marked equity, promotion, mailbox or real account write.

Retain the complete 256-row grid. Signal values are copied from the sealed S4-A
output, not recomputed, reranked or selected using execution-date facts. Duplicate
keys/date mismatch are hard errors; missing individual rows remain explicit.
Expose parsed raw bar/limit values and calendar-based ADV20 through ResearchSession
objects with market-open status, corporate processing, order-purpose rule admission,
participation and full fee scenario explicitly unknown. ST/suspension records are
exception evidence and provenance only; absence must never set market_open=True.
Do not force a continuous-auction rule into an unreviewed close-auction context.
Existing minimum corporate fields and rule-v3 coverage remain available evidence,
but do not by themselves certify the new session's complete execution/cash context.

Deliver adapter, meaningful unit tests and one immutable 256-row input table/report.
Separately recompute integer conversions, exact prior20 mean and row/key counts
without calling the adapter, then seal one independent arithmetic proof; same agent,
not independent personnel. Do not run the scheduler on these historical rows or
publish a stopped price path under an input-audit label. The bridge materializes
necessary available inputs without claiming that every remaining gate is cleared.

Full pytest/Ruff/diff, self-review, commit/push before actual/proof; PR, merge and exact
master CI close this part. Keep all attempts and failures. If historical corporate,
market-status or fee evidence still prevents an admissible path, freeze the concrete
gap and use the independent approved prospective S4 signal route under its own
finite card; do not repeat the entire prerequisites audit or wait for QMT.
