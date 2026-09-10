# External Cash Flow Effective-Time Semantics v2 — self-review

Status before PR: implementation complete enough for CI review; no performance method is enabled.

## Architecture checks

- External cash flows remain personal account facts, not `ExecutionLedger` events.
- New imports require separate `effective_at` and `reported_at`; report time cannot precede economic time.
- Cash-flow replay orders by economic effective time. Broker fills retain their existing execution-ledger timestamp and T+1 semantics.
- Legacy v2 cash-flow rows remain replayable but are tagged `legacy_reported_time_used_as_effective_unverified` and are never timing-eligible for performance.
- Fill-only journals remain v1. Any journal containing cash-flow facts is materialized as v3 so timing semantics are explicit.
- The public `quantlab.personal.tracking` import path remains stable; implementation is isolated behind one core module rather than duplicated.

## Evidence and failure checks

- Exact cash-flow identity includes effective time, reported time, direction, amount, and timing quality; reused flow ids with changed timing conflict.
- New CSV imports cannot silently use the old five-column contract.
- Tracking fingerprints bind cash-flow timing fields when cash flows exist while preserving fill-only fingerprint behavior.
- Summary/effective account expose latest economic fact time separately from latest report time and expose aggregate timing quality.
- Valuation checkpoint v2 binds the account timing quality and latest economic fact. Legacy timing produces `blocked_legacy_cash_flow_timing`; no TWR/CAGR/IR is emitted.
- Existing valuation checkpoint v1 artifacts remain readable under their original schema.

## Scope held closed

- No provider calls or Canonical writes.
- No strategy promotion or historical performance recomputation.
- No broker submission or simulated fills.
- No TWR, CAGR, IR, or investment-performance claim.

CI is the next gate. Any test, Ruff, or diff-check failure must be resolved before merge.
