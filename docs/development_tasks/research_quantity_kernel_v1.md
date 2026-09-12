# Isolated hypothetical stock quantity/cash transition kernel v1

Authority: approved research program v3 and the completed S4 execution prerequisite
audit. This is one bounded implementation part. Start from the clean pushed merge
of PR100 after its exact master CI passes; bind that HEAD in the run receipt.
Confirmed base: e8d9a06216598b8387cac54924a21101afec09e6; exact master CI34713369117 passed.
Confirm no other writer/heavy job, then commit and push this card before coding.

Purpose: remove one concrete gap between signals and a later honest S4 net-cost
research replay. Implement a pure isolated stock-order transition function and
synthetic accounting tests. Do not alter the frozen lifecycle backtest engine,
live execution planner, fee-component contract, old experiment results or mailbox.

Maximum45 minutes useful implementation/test segment,2GiB worker RSS,256MiB new
ignored output,8GiB physical D reserve. Full project checks may run during closure.
No provider requests, canonical data reads/writes, actual historical portfolio
paths, model fits, new signals or candidate identities. Only tracked source and
synthetic test inputs. No broker/account writes, order submission or promotion.

The kernel is explicitly hypothetical research, not order/fill evidence. It must
never emit a broker fill, execution authority, strategy return or NAV. Inputs are
raw integer-fen prices, integer-share lots, dates, explicit research rules, complete
declared cost-scenario components, evidence states and finite capacity assumptions.
Do not interpret the existing cost component subtotal as a complete trading cost.
Additional fees/slippage cannot be silently zero: every modeled component must be
explicit; unknown input blocks the transition. Numeric values in synthetic tests
are fixtures, not validated historical rule or fee evidence.

Required invariants and cases:
- Unknown raw bar, price limits, status, calendar, rule scope, corporate-action
  processing or fee scenario blocks. Failed sells keep every share and no cash
  appears. A bar is only input to a declared model, never proof of real fill.
- Directional close-at-limit blocks buying at the upper bound or selling at the
  lower bound; modeled adverse slippage may not exceed known daily/limit bounds.
- Fees aggregate once per simulated order and round explicit components to fen.
  Buy cash sizing includes all declared modeled fees; no negative cash or leverage.
  Sales can finance later calls only after their simulated state transition.
- Sell from dated lots only after the supplied verified trading-calendar T+1
  availability. Preserve lots not sold; do not erase locked positions or assume
  same-day purchases are sellable. FIFO applies to eligible synthetic lots only;
  corporate tax is not calculated by this kernel.
- Explicit buy/sell minimum and increments; full-position odd-lot exit only when
  the entire position is sellable and capacity allows it. STAR-style minimum200,
  increment1 is a fixture option, not an inferred board rule.
- Capacity uses both prior20-session amount known by signal time and realized
  session amount/volume solely at simulated execution. Participation fractions
  are explicit assumptions. Aggregate used capacity across multiple calls on the
  same instrument/session; no reset by splitting one intended order into requests.
- Require signal date before execution date, prior20 amount as-of no later than
  signal date, scoped rules/fees, chronological state and verified next session.
  Invalid amounts, booleans masquerading as integers, nonfinite rates, duplicate
  lots and malformed intervals are errors, not favorable defaults.

Deliver module, focused tests and concise boundary documentation. Tests should
exercise accounting identities, cash-constrained sizing, partial/rejected sales,
T+1, STAR/main-board synthetic rounding, fee minima, unknowns, slippage, capacity
reuse and temporal validity. Add no UI or NAV path in this part. Full `uv run pytest`,
`uv run ruff check .`, `git diff --check`, self-review, commit/push/PR and exact
master CI. Seal a local receipt with timing, checks, source/final HEAD and unchanged
budgets. If time expires, close a coherent tested subpart and state the unfinished
requirements; do not pad, widen the card or run a historical replay prematurely.

After this part, smallest next data task: an immutable31-code dividend intake only
for codes absent from the existing225-code cohort coverage, followed by cashflow
schema/duplicate/version checks. Its own finite card must be committed before any
request. Separately extend2020–2022 rule scope without changing existingv2 IDs.
