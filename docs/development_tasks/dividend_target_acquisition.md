# Sealed target dividend raw acquisition — Issue #74

Direct commission, starting at clean pushed master
`6a2e15626f56682b8c5f84e650b21976bb20b600`; exact master CI 34616179332 passed.
The user has already authorized Tushare acquisition. Old shared reviewer remains
paused; preflight found no concurrent writer or heavy job. Active automation is
fixed every two hours with about 90 minutes useful work and 30 minutes for closure
and delays. A long run must checkpoint; an occupied worker lock prevents duplication.

Bind the cost audit's exact 4,516 codes, its report and separately sealed request
in `config/dividend_acquisition_v1.json`. The original 97 canonical dividend rows,
models, scores, rule catalogues and previous reports are read-only. All new data
goes to ignored `data/products/corporate_action_staging/dividend_targets_20260911`.

Official dividend documentation requires at least 2,000 points and limits one
response to 2,000 rows. The current general 2,000+/5,000+ tiers specify 200/500
requests per minute; this contract stays at 120 with one request in flight.
Query each exact code's returned history using all 16 documented fields, including
nondefault base date and base shares. No date filter substitutes announcement/year
for record/ex/pay/list dates. Keep proposals, nulls, duplicate rows, and possible
version conflicts, including records outside the fixed 2023-01-04–2026-09-10 window.

The protocol document specifies the `api.tushare.pro` REST JSON envelope. The
installed SDK uses a different `api.waditu.com/dataapi` address and converts the
response into a DataFrame. This raw adapter uses the documented host via HTTPS,
with no HTTP downgrade, redirects, automatic retries or deserialization by SDK.
TLS/API availability is established only by the counted real request after the
implementation has passed tests and been committed/pushed; failure remains a result.

Before I/O, publish a durable intent with exact parameters excluding the token.
After I/O, retain body bytes, wire/stored hashes, HTTP/server status, observation
time, row counts and null/date/unit checks. Exact token echoes are redacted and
stop the batch; exception text is never logged. An interrupted request without
a result consumes one call and its full reserved body allowance. It is not
automatically replayed. Prior successful responses are never fetched again.

Across all wakes: 4,516 initial requests, at most 100 transient retries and one
retry per code, only after initial requests. Authentication/entitlement failures
stop immediately; protocol/schema errors stop rather than broadening the request.
Three consecutive transport/rate failures stop the segment. Saturation or body
caps retain partial evidence for a separate task, without claiming completeness.

Limits: 512 MiB cumulative body bytes (including failed/unknown reservations),
4 MiB/body, 1 GiB all staging outputs, 50 MiB new public documents, RSS 4 GiB,
host D reserve 8 GiB, compute/Arrow pools two. Stop new requests at 40 minutes and
checkpoint by 45 minutes of the acquisition segment. The larger development turn
also includes implementation, tests, independent verification, UI and delivery.
Budget checks reserve space ahead; worker locks are OS-managed and not force-reset.

Progress and latest pointers are explicitly replaceable derived views. Request
intents, raw results and per-run reports are exclusive and content-fingerprinted.
Raw nonempty and empty responses do not certify a complete event history, past
knowledge, entitlement, cash/share receipts, dividend tax, return or execution.

Required acceptance: focused parser/journal/transport/reader and orchestration
tests; full pytest, Ruff, diff, all five optional synthetic runtime tests; one
actual bounded acquisition segment with independent raw/hash/count/date/unit
verification; Chinese browser status and download acceptance; PR, green CI,
merge, exact master CI, delivery and a next/resume card. No real model fit,
factor/score recomputation, performance backtest, canonical/account write, order,
fill synthesis, forward registration or strategy promotion.
