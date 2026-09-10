# Manual fill economic/report time integration — Issue #32

Direct commission: continued autonomous development toward a usable local tool.
Exact clean, pushed starting HEAD: `c85a9df349f460fe157e0f407bcd6a8292d8e879`.
Branch: `codex/manual-fill-time-integration`. PR #36 and its post-merge CI passed.
The old executor remains inactive and the reviewer automation is paused.

Preserve and integrate the existing `ManualFillFact` type from remote branch
`chatgpt/manual-fill-effective-time-v2` at `e5ea7c1`; do not overwrite that branch.
Strict new CSVs require executed_at and reported_at, both aware, with report >=
execution and trade_date equal to the Shanghai execution date. Replay exact fills
by executed_at with cash flows by effective_at and event-id ties. ExecutionLedger
continues to own gross/fee/share/T+1 accounting without generic event changes.

New fill-containing journals use v4. Existing v1/v2/v3 fill journals keep their
legacy ordering and explicitly unverified quality. No read-time migration.
Expose timing quality and watermarks in tracking/effective-account output, and
bind new timing to tracking identity. Update the UI CSV and preview fields.

Use synthetic tests only: delayed reporting, wrong/naive times, opening cutoff,
mixed funding order, duplicate timing, schema downgrade, legacy replay, and T+1.
Run full pytest, Ruff, diff-check and optional runtimes. No provider calls,
canonical writes, real account mutations, returns, strategy promotion or orders.
Historical state cutoffs, full performance readiness and corporate-action posting
remain separate prerequisites, not capabilities asserted by this timing change.

Record test results, commits/push, final HEAD, CI and limitations in the PR handoff.
