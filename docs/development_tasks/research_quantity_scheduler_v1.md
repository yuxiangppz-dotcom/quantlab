# Chronological stock research replay scheduler v1

Direct approved v3 commission. Start from clean pushed master
3264af2b07afad19137dd577d82fc5a12f47bee6, exact CI34726277882 passed.
No old executor/reviewer or heavy worker is active. The shared loop remains paused.
Commit/push this card before implementation; bind its HEAD in the local receipt.

This finite implementation removes the chronological scheduling gap between the
existing hypothetical quantity kernel and a later fixed S4-A two-cost replay.
It does not authorize a historical economic path. Historical input certification,
allocation, corporate event postings and the actual two cost configurations remain
separate, necessary integration work; do not label synthetic examples as strategy
performance or consume another strategy identity for this component.

Maximum 45 minutes useful implementation/check segment, 2 GiB worker RSS,
128 MiB new ignored output, 8 GiB physical D reserve. Source and synthetic fixtures
only. Zero provider/public-source requests, canonical data reads/writes, historical
paths, model fits, new signals/formulas, orders, account or mailbox writes, promotions.
Do not rerun any previously sealed actual/proof writer or modify frozen backtests.

Implement one isolated pure scheduler over an explicit complete trading calendar,
an initially flat research cash book, immutable daily order batches, typed raw-fen
closing marks and existing ResearchSession/ResearchOrder/ResearchTransition objects.
The supplied calendar includes the prior decision session and next session beyond
the requested final date. Every requested session must be represented, including
days with no orders. Day orders use the immediately previous session's decision;
there is no same-day signal execution or implicit compression across missing days.

Before any mutation on a day, validate every required input. Missing/unknown
corporate processing, marks, calendar, ordered-instrument context, fee or applicable
rule stops the entire path at the last complete day; do not quietly skip an unknown
sell and publish a complete NAV. A known non-executable order (suspension, directional
limit, capacity, insufficient cash, T+1 or grid) may remain blocked while its holdings
and marked value persist. A suspension is not permission to invent a stale mark.
Corporate processing flags are explicitly caller-declared scenario assumptions,
not source certification; the scheduler cannot manufacture distributions or taxes.

Execute sell attempts first, sorted by instrument/order ID, then buy attempts in the
explicit frozen input order. At most one order per instrument/side/day; no automatic
splitting to bypass minima or capacity. Shared instrument-day capacity must survive
sell/buy calls. Only completed modeled sales fund later buys. Order attempt IDs must
be unique even after blocked attempts. The scheduler never automatically retries,
resizes a target or drops an unsold lot; a later exit attempt needs a new explicit
order and decision date. Acquisitions retain calendar-derived T+1 sellability.

Emit daily scenario cash, raw-price position value, total marked equity, quantity
and modeled order/fee records, plus explicit stop date/reason and valid-through.
No CAGR, recommendation, broker fill, historical-performance eligibility or execution
authority. Stopped results have no invented suffix or forced liquidation; complete
results may retain holdings and disclose them. Add a pure calendar helper for the
S4 t+1 acquisition / t+6 target exit (five sessions after actual acquisition), with
unknown beyond calendar coverage. It does not guarantee an executable exit.

Tests must reconcile multi-day cash/share conservation, exact minimum fees and
sale-funded buys, blocked/partial exits, capacity reuse, T+1, no-order days, fee-date
changes, all-or-nothing preflight, missing marks/context/calendar, signal lag,
duplicate attempts, unchanged source objects and prefix stability under future
fixture changes. Use independent hand arithmetic for key scenarios. Full pytest,
Ruff and diff checks; self-review, commit/push, PR CI, merge and exact-master CI.

Deliver a concise Chinese component explanation and ignored verification/closure
receipts with HEADs, attempts, failures/fixes, timings and limitations. No new UI.
Then use a separate finite card to bind the saved S4 signals and necessary historical
inputs to this scheduler, without repeating whole prior audits or requiring live
broker/account infrastructure. Unknown original event/price/state facts stay unknown.
