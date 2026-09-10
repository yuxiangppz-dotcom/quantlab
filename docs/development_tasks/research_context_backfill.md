# Missing ST and suspension context — Issue #54

Direct user commission authorizes provider downloads and canonical processing.
Starting clean pushed `f3d8c049bba74089d2aa8a36e90dd7bc02ff13e8`, master CI
34507168850 passed. Branch `codex/research-context-backfill`. No concurrent
executor/reviewer; no scheduled loop activity or changes.

The local audit identified 406 missing dates for each of stock_st and suspend_d,
2025-01-02 through 2026-09-03. This job repairs only those missing partitions,
using a verified local SSE/SZSE calendar and a hard maximum of 812 provider calls.
No calendar/security/daily/index/limit/financial/dividend requests are in scope.
Calls are serial, separated by at least 0.4 seconds, and never automatically
retried. Already present files are reused without provider calls. The original
observation time of reused data remains unestablished by this job.

`quantlab.data.context_backfill` reuses `sync_lifecycle_context` and its existing
formats and date/page/duplicate checks. A job-local storage subclass publishes
new Parquet files by exclusive atomic hard link; it cannot overwrite concurrent
or previous files. Existing global storage and financial semantics are unchanged.
The 1,000-row ST guard follows the provider documentation; the existing 5,000-row
suspension guard is a conservative project guard, not a newly asserted provider
contract. Reaching either guard is incomplete, never accepted as full coverage.

Ignored `data/products/context_backfill/<run-id>/` stores a clean-code/calendar
plan, the original byte-hash manifest, durable per-request times and normalized
record hashes, durable accepted-file hashes, and a final fingerprinted report.
Partial failure remains explicit. If the process stops after publication, earlier
request/accepted receipts remain; resumption requests only absent files. A tiny
crash window between publishing a file and its acceptance receipt is covered by
the prior normalized-response receipt, but this job does not manufacture a
missing acceptance receipt or backdate unknown observation evidence on resume.
Both original and newly accepted files are rehashed at completion. Source drift
is reported; the job never deletes or rolls back canonical facts automatically.

Empty successful responses mean the provider returned no events in that request.
They do not prove every security tradable. Historical revisions, original daily
publication times, full ST/S-R coverage and corporate-action effects remain
unverified. No performance/return or execution authority is produced.

Sources: [ST daily list](https://tushare.pro/document/2?doc_id=397),
[daily suspension/resumption fields](https://tushare.pro/document/2?doc_id=214).

Run from clean pushed code under the existing exact acquisition authorization:

```text
uv run python -m quantlab.data.context_backfill --authorized-provider-write
```

Twenty new tests cover missing-only requests, budget/date boundaries, both local
calendars, partial success before same-day failure, durable receipts, safe
resumption, empty responses, invalid/truncated/duplicate rows, concurrent-file
preservation, source drift and pacing. Self-review corrected acceptance logging
to occur immediately after each publication, preserving the first successful
dataset when the second dataset fails on that day. It also added final hash
checks for newly accepted files. Pre-run verification: **1,347 passed, two default
optional skips**; both LightGBM/Qlib optional checks passed separately. Ruff and
git diff --check passed. Three files changed; no dependency additions.

Tested code will be committed and pushed before the actual authorized run;
real timestamps/counts and the report reference follow. No labels, training,
account imports, fills, orders, strategy promotions, C-drive changes or changes
to frozen accounting/execution/backtest rules. Freeze after real verification
and green CI. Next task: historical price-limit gaps with explicit special-case
handling, followed by preregistered features and bounded model diagnostics.
