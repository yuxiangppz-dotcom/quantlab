# Approved v3 launch: bounded ETF inventory and funding observations

Direct single-agent commission, starting from clean pushed
`f52f545cee31848216fd933f5d4e38754a70ceec`. User explicitly approved v3 and asked
to start on 2026-09-12. No concurrent writer/heavy process was present. The
two-hour heartbeat is active; the old shared executor/reviewer loop stays paused.
The exact approved proposal is preserved in `docs/research_program_v3.md`.

This independently reviewable part acquires raw inputs only, after implementation,
tests, commit and push. It authorizes exactly the requests generated from
`config/research_program_intake_v1.json`: three `fund_basic(market=E)` requests
for D/I/L statuses, then one `moneyflow` request for each of 256 fixed codes,
2019-10-01 through 2024-12-31. No automatic retries, pagination or substitutions.
Selection uses the smallest SHA256(seed|code) from the observed 2019-12-31 daily
partition. It does not use present-day survivors or future performance. This is
a fixed historical cohort diagnostic pilot, not the complete A-share universe.
Its source partition and exact selection are fingerprinted before any request.

Fund metadata includes terminated products as well as live products. Current
names, classifications, benchmarks and dates are observations, not certification
of their historical versions or evidence that every exchange product is an ETF.
An ETF universe will be fixed separately from listing/tracking evidence, before
its return comparison. No 8000-point ETF metadata endpoint or paid permission is
requested. `moneyflow` active large/extra-large classifications are not institution
identities; its total net field is not assumed to equal category subtraction.

Only ignored `data/products/research_program/launch_20260912/intake` receives raw
responses, durable request intents, hashes and reports. Use existing OS heavy and
output locks; no lock breaking. Canonical sources are read-only. The 259-call
budget is cumulative across wakes, each request attempted once, with intent
written before I/O. A missing result consumes the attempt and full body reserve;
it blocks recovery instead of silently being replayed. Permission, schema,
saturation or uncertain transport outcomes stop this first pilot. Re-running a
completed scope returns existing evidence without a new provider request.

Use reviewed `WireClient`, official HTTPS, no redirects or SDK retry behavior,
and token-free receipts. Keep nulls, duplicates and empty responses distinct;
preserve raw values and profile anomalies without fabricating clean rows.
Unknown publication/revision history remains unknown. Acquiring old observations
now does not certify historical PIT correctness. No return or performance claim.

Resources: 60 requests/minute, one request in flight, 4 MiB per response,
256 MiB cumulative body/reservations, 512 MiB output, 2 GiB RSS, host D reserve
8 GiB, two compute/Arrow threads. New requests stop by 18 minutes; a 20-minute
watchdog bounds the segment. This limit does not cap the entire development turn.

Validation: request bounds and fixed selection, malformed JSON/schema/identity,
dates outside the request, duplicate/null retention, truncation/empty distinction,
intent/result/source tampering, interrupted request accounting, no automatic
retry, completion reuse, permission failure and resource limits. Run full
`uv run pytest`, `uv run ruff check .`, `git diff --check`; independent raw
receipt/count/hash verification after the actual run. Commit/push each part,
PR/CI/merge and exact master handoff. Historical input and code hashes retained.

Next independently scoped part: F1--F4 fixed funding diagnostics, no economic
paths or fits. Predeclare formulas, calendar-aligned missingness, 5-session
next-close label, period containment and retrospective evidence labels before
calculation. S1 ETF data feasibility proceeds alongside it. Old Issue90 stays at
zero actual attempts and does not block this independent route. The 80-path,
6-fit, 8-funding-feature and 20-formula program budgets do not reset.
