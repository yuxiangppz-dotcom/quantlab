# Execution Framework and Readiness

## v0.2.1 — Transaction, Evidence, and Lineage Closure (current)

v0.2.1 fixes the correctness gaps found in v0.2 (marked below as a
superseded candidate). Its formal artifacts publish under
`data/experiments/execution_readiness_v0_2_1/<run_id>/` with schema
`execution_readiness_v0_2_1`; the v0.1 and v0.2 artifacts and verifiers
remain valid and unchanged.

- **Transactional ledger.** Every `append` and `submit_orders` call is one
  transaction with strong exception safety: any Exception or
  BaseException — at the first, middle, or last batch submission, after a
  reservation, after an order-status change, around the event index, or
  inside the invariant check — restores cash, lots, orders, reservations,
  events, event ids, fill ids, and request ids exactly. Replay
  determinism and duplicate-event idempotence are unchanged.
- **Reservation-aware execution state (TOCTOU).** The unique
  execution-state fingerprint binds account id, settled cash, position
  lots, every active cash/share reservation, and the live order states.
  Assessments and submissions bind it; stale ones are rejected before
  mutation, and every batch member must bind the same explicit pre-batch
  state. Planning consumes available (unreserved) cash and available
  sellable shares via `ExecutionStateView`, never the settled snapshot
  alone.
- **Order-lifetime fee budget.** The fee cap is frozen as the cumulative
  fee ceiling over one order's whole lifetime. Quotes are typed
  provenance (instrument, account, trade date, schedule evidence id,
  SHA-256 fingerprint, synthetic flag) and never degrade to a bare
  integer at the submission boundary. Reservations independently track
  remaining worst-case limit notional and remaining fee capacity; after
  every partial fill the reservation equals unfilled shares × limit +
  remaining fee capacity, price improvement releases the excess, full
  fills leave no residual, and cancel/expire releases the rest.
- **Limit-price protection.** A BUY fill above its limit (a SELL below
  its limit) is rejected before any ledger mutation.
- **Plan → Intent → Assessment → Submission lineage.** A pure adapter
  materializes only ORDERABLE legs of a SUBMIT_READY plan into
  deterministic intents and requests bound to instruction fingerprint,
  plan id, leg id, execution-state fingerprint, price-source
  fingerprint, fee-quote fingerprint, intended trade date, and DAY TIF;
  `verify_lineage` invalidates reuse after any drift. The planner
  rejects cross-instrument price evidence, mismatched fee quotes, and
  pre-dated price evidence. The formal smoke walks the REAL chain from a
  positive TargetPortfolio through the handoff, planner, adapter,
  execution-state-bound assessments, and a transactional submission;
  `external_broker_submission = false` is stated explicitly.
- **Canonical row audits.** Daily, stock_st, and suspensions each get
  their own storage-contract primary key, required non-null fields, and
  finite numeric fields; `suspend_timing` is nullable and multi-event
  rows are never duplicates. Raw file integrity and negative
  market-access coverage are disclosed separately, and the suspension
  check is capped at partial.
- **Composite READY re-binding.** Every composite readiness decision is
  re-derived from its exact disclosed sub-conditions (never a
  pre-computed boolean) at preflight, post-promotion verification, and
  in the independent verifier; the performance-field scan covers every
  JSON file; and any flipped, removed, extended, or retyped smoke
  condition, or a smoke/CSV/summary contradiction, can never reach a
  COMPLETED marker.

## v0.2 — Executable Order Path (superseded candidate)

v0.2 closes the engineering-correctness chain from an instruction to a
reservable, broker-facing order, without simulating fills or connecting to a
broker:

```
TargetPortfolio
  → fail-closed share planning
  → RebalanceInstruction
  → account-aware OrderPlan (planning.py)
  → constraint assessment (submission eligibility vs fillability)
  → atomic cash/share reservations (append-only ledger)
  → broker-facing OrderRequest (DAY, intended-trade-date bound)
  → fill/cancel/expire ledger accounting
```

Key v0.2 semantics:

- **Submission eligibility is not fillability.** Unknown fill probability or
  queue position keeps an otherwise fully qualified limit order `VALIDATED`
  with the uncertainty on its audit row. Order admissibility, market
  accessibility, fee determinability, and (for sells) position sellability
  remain fail-closed gates.
- **Account-aware planning.** `RebalanceInstruction.targets` is the complete
  desired position set: a held name omitted from the instruction has an
  explicit zero target (an exit, not an omission). Deltas are
  `target_shares - current_shares`; a delta that cannot satisfy the board's
  unit rules blocks the leg with a structured reason and never silently
  rounds the target. Buy limits come only from separate raw order-price
  evidence with its own `available_at` and source fingerprint — never from
  handoff planning prices. Buys are funded only from currently available
  cash at the worst case (limit notional plus an explicit fee cap); expected
  sell proceeds never fund a buy.
- **Atomic reservations.** Buy submissions reserve worst-case cash; sell
  submissions reserve sellable shares. Batch submission is all-or-nothing;
  partial fills draw the reservation down; cancel/expire releases the
  remainder; availability is always settled minus reserved, with invariants
  re-checked after every event. Constraint assessments bind an account-state
  fingerprint; stale ones are rejected.
- **DAY + exact T+1.** `TimeInForce.DAY` is explicit (no GTC default).
  `OrderRequest` binds the intended trade date; a late broker report cannot
  drift a fill's economic trade date. A buy lot's `sellable_from` must equal
  `calendar.next_session(trade_date)`; incomplete calendar coverage is
  fail-closed.
- **Domain semantics bound to the artifact contract.** Readiness semantics
  (check inventory, status counts, gate re-derivation, prohibited
  performance fields, explicit false claims, handoff denials, input
  stability) run at preflight, post-promotion formal verification, and in
  the independent verifier — a semantic failure can never reach a valid
  `COMPLETED.json`.
- **Row-level partition audits.** The daily/ST/suspension audits now check,
  per partition: row `trade_date` equals the partition date, primary keys
  are unique, required fields are non-null, OHLC relations hold, and
  volume/amount are non-negative; anomalies and samples are written to the
  evidence.
- **Synthetic smoke suite.** The formal audit executes a synthetic,
  non-trading smoke suite against the real code: positive-weight handoff,
  buy/sell deltas, the real constraint engine's submission-eligible path,
  cash contention, share contention, partial fill + cancel release, stale
  snapshot, wrong trade date, and weekend/holiday T+1. No provider call, no
  canonical write, no order submission, no fill claim.

The v0.2 audit publishes under
`data/experiments/execution_readiness_v0_2/<run_id>/` (schema
`execution_readiness_v0_2`); the v0.1 artifact and its verifier stay valid
and unchanged. Fee readiness remains blocked (no real, effective-dated,
account-specific fee table), and historical/paper/live readiness remain
false by construction.

## v0.1 — Contracts and Gates

## Scope

The execution layer currently provides contracts and evidence gates, not a
historical execution simulator and not a broker connection. Its implemented
path is:

```
TargetPortfolio
  → fail-closed share planning
  → RebalanceInstruction
  → OrderIntent
  → five independent constraint decisions
  → append-only order/fill/accounting ledger
```

`RebalanceInstruction` is an absolute share target with a target fingerprint,
signal cutoff, execution date, planning NAV/cash policy, and raw-price source
metadata. It is not an order. A raw daily observation can only be used for
quantity planning when it was available by the signal cutoff. Adjusted prices,
future execution closes, fractional-fen prices, missing PIT identities, and
missing PIT rules cannot produce a positive share target.

The handoff is all-or-none. If one positive target is unresolved, no instruction
is emitted. This prevents a missing name from being interpreted downstream as a
zero target and accidentally liquidated.

Share planning uses the applicable buy-lot grid as an explicit floor-from-zero
rounding policy. It does not know the current account position and therefore
does not create buy/sell deltas; odd-lot exits and actual order sizing remain a
separate account-aware step.

## Independent decision dimensions

Pre-trade assessment does not collapse unlike questions into one tradable flag:

- order admissibility;
- position sellability, including T+1 lots;
- market accessibility;
- fillability;
- fee determinability.

`unknown` is preserved. A daily bar is never fill evidence. The absence of a
suspension record is not automatically evidence that a stock was open. ST data
is context-only in v0.1 and does not change targets or rules.

## Formal 2020–2024 audit

Run from a committed, clean workspace:

```bash
uv run python scripts/run_execution_readiness.py
```

The runner reads local canonical data only and publishes a small artifact under
`data/experiments/execution_readiness_v0_1/<run_id>/`. Publication uses the
generic fail-closed artifact protocol. The independent verification command is:

```bash
uv run python scripts/verify_execution_readiness.py \
  data/experiments/execution_readiness_v0_1/<run_id> \
  --expected-head <full-git-sha> --expected-run-id <run_id>
```

The audit emits five statuses: `ready`, `partial`, `blocked`, `not_modeled`, and
`not_applicable`. It separately reports:

- `framework_valid`;
- `historical_execution_ready`;
- `paper_execution_ready`;
- `live_execution_ready`.

A valid artifact and valid framework do not promote the other three gates.
The audit contains no performance fields, return series, order submission, or
fill claim.

## Known blockers

For 2020–2024, canonical SSE/SZSE calendars and raw daily/ST/suspension
partitions have complete date-partition coverage. That does not close execution
correctness. The remaining blockers include:

- security board/identity history is not fully effective-dated, and the curated
  code-lineage file is not declared complete;
- the byte-verified SZSE trading-rule history is unresolved for 421 open
  sessions from 2023-04-10 through 2024-12-31;
- historical price bands, IPO exceptions, ST bands, price cages, and auction
  mechanics are not modeled;
- exact effective-dated statutory fees and broker commission schedules are not
  loaded;
- T+1 sellability is calendar-derived and verified since v0.2 (exact
  next-session binding with fail-closed coverage gaps); same-day sale
  remains blocked by lot state;
- corporate-action cash/share postings are not implemented;
- minute, quote, queue, auction, and order-book evidence is absent;
- broker gateways, account reconciliation, kill switches, approvals, and live
  monitoring are absent.

Until these are closed, historical, paper, and live execution readiness remain
false by construction.
