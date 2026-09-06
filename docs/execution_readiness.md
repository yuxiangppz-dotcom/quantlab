# Execution Framework and Readiness

## v0.2 — Executable Order Path

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
