# Account performance input diagnostic — Issue #46

Direct commission. Start clean pushed `bc09ba29463980468926f4d58cbefbec52f7b0a6`,
master CI 34500773750 passed. Branch `codex/account-performance-input-diagnostic`.
No concurrent executor/reviewer. Read-only evidence inspection and Chinese UI;
no return calculation or changes to checkpoint v1/v2, journal, accounting or
execution contracts. Missing evidence must remain unknown.

Check recorded fill/cash-flow timing independently; economic replay, checkpoint
integrity and state binding; raw partition evidence; daily-close cutoff; external
flow boundary valuations; corporate actions; broker reconciliation; validated
performance method. Current recorded absence is not certified broker completeness.
The method gate remains blocked until a separately reviewed method and interval
are implemented. This diagnostic cannot produce a performance claim or authority.

The 15:00 Shanghai daily-close cutoff is an explicit diagnostic convention,
not a denial of later account activity or a new execution rule. The SSE normal
auction schedule ends at 15:00; after-hours transactions can still exist:
https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml

Verify synthetic legacy/exact timing, later same-day facts, stale checkpoint,
price evidence drift/missingness, corruption, concurrent account drift, no writes
and the UI. Full pytest, Ruff, diff check and optional runtime smoke before
self-review, push, PR, green CI, merge and master CI. No provider calls, canonical
writes, real imports, orders or promotions in this part. Freeze after acceptance;
next: preregistered research costs and bounded factor/model experiments under the
user's max drawdown target of 20%, minimum one-day holding and stamp duty excluded
from the user-reported commission.

Acceptance: 1,242 tests passed, two default optional skips; the two optional
LightGBM/Qlib runtime tests passed separately. Twelve new tests cover the
diagnostic and Streamlit callback. Ruff and git diff --check passed. Actual
Edge headless acceptance clicked historical replay and the diagnostic on the
clearly simulated cash-only demo account; no browser/page exceptions or writes
to the journal. Self-review retained independent timing checks, explicit unknown
corporate-action/reconciliation evidence and source-drift rejection. No financial
contract changes, provider calls, canonical writes, real imports or returns.
Seven files changed; no scope deviations. Final feature/merge HEADs, push and CI
are recorded in the linked PR. Remaining performance method and reconciliation
gaps are explicit; freeze this diagnostic and move to research prerequisites.
