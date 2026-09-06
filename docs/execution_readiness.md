# Execution Framework and Readiness v0.1

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
- T+1 lots block same-day sales, but exact next-session sellability is still
  supplied by the caller rather than derived and cross-bound to the calendar;
- corporate-action cash/share postings are not implemented;
- minute, quote, queue, auction, and order-book evidence is absent;
- broker gateways, account reconciliation, kill switches, approvals, and live
  monitoring are absent.

Until these are closed, historical, paper, and live execution readiness remain
false by construction.
