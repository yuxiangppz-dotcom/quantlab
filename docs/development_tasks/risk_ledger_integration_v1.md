# Risk-ledger integration task card v1 (direct commission)

Branch `codex/risk-ledger-integration-v1` in the isolated clone
`/home/administrator/projects/quantlab-zcode-risk`. The Codex working tree
(`/home/administrator/projects/quantlab`) is read-only for existing research
materials; new outputs go to this clone's own ignored output directory. The
automation stays paused. Start head (clean, pushed, equals the PR #122 merge):
`2a5eae70659cb3dee30ee8c127e00b79780aa557`.

Scope: (a) a minimal single-day advance extraction inside
`quantity_scheduler.py` so a chronological book can advance one session at a
time — the existing `simulate_research_schedule` entry, its tests and frozen
financial semantics stay byte-for-byte compatible; (b) a new independent
`src/quantlab/research/risk_ledger_loop.py` driving the closed loop; (c)
integration tests with hand-computable financial results; (d) Chinese docs and
a runnable example; (e) a read-only admission check of the sealed S4 first
entry materials. No new factors, models, strategy parameters, provider calls,
canonical writes, downloads, orders, or USER_APPROVED changes. The seven risk
rules stay exactly as merged in PR #122.

## Daily loop order (frozen)

For each common trading session t, in this order:

1. Execute t's pending order intents — they may only originate from the
   previous common session's decision (`signal_date = t-1`).
2. The existing kernel applies constraints, fees and lots; raw-close marks
   complete the day's valuation (cash + lots × mark).
3. The risk decision at t uses only t-and-earlier facts: the ledger's
   fee-inclusive marked equity as the D NAV, the persisted RiskState, plus
   caller-supplied unscaled V returns and 000985 closes when applicable.
4. The cap applies to the caller's still-valid base target for t (dated t or
   earlier; the adapter projects older targets and records `signal_as_of`).
   The base target is never the previous day's scaled target — no compounding
   of de-risking; recovery returns to the base strategy's current valid
   target without buying expired signals (target weights the strategy no
   longer declares are simply absent from the base target).
5. Order intents for the next common session are diffed from
   risk-adjusted target vs actual holdings using t's raw marks. One net
   intent per (instrument, side) per day — strategy expiry and risk exits
   merge into a single synthesized intent. Deterministic id
   `<signal_date>|<side>|<instrument_id>`; a next-day regenerated intent is a
   new explicit decision, not an automatic retry.
6. Next-session fills, partial fills and blocks are the kernel's authority.

Frozen sub-decisions: when the risk decision is unknown (for example V/M
inputs missing), the loop issues no new intents that session, keeps holdings,
records `risk_unknown`, and continues — unknown risk is not an empty-cash
target and not a path stop; a stop is reserved for missing execution evidence
and retains the last complete day. Buys are queued by descending target value
then instrument id; sells by instrument id. NAV enters RiskState as an exact
Decimal of integer fen. The V series must be caller-supplied unscaled returns
with a declared source — never derived from this layer's own scaled ledger.

## Financial semantics preserved

Integer fen cash, integer shares, raw (unadjusted) execution and mark prices,
T+1 lots, board quantity grids, directional close limits, daily capacity with
same-day shared use, sell-before-buy with only simulated sale proceeds
funding buys, minimum commissions and declared stamp/fees. Unsold shares stay
in the book and keep marking; nothing is deleted, zero-filled or liquidated
by assertion. Base target, risk-adjusted target and actual holdings are three
separately stored facts; target satisfaction and realized over-cap exposure
are reported separately. Each session commits atomically (book, risk state,
intents); a retry resumes from the last complete day and cannot double-count
fills, fees or recoveries.

## Acceptance

Integration tests with concrete share/cash/fee/state numbers covering:
fixed-cap parity between the old scheduler and the loop; de-risk executable
with conservation; cap zero with limit-down/suspension keeping positions and
mark-to-market; T+1 locks and partial fills; mid-cycle de-risk on a
non-selection day then recovery to the current base target; same-day retry,
year boundary and restart without HWM reset or double accounting; missing
evidence stops with the last complete book; future-input mutation leaves
earlier targets/orders/books unchanged; risk-exit/strategy-expiry conflict
produces one sell; target-vs-actual over-cap reported separately. Full
`uv run pytest`, `uv run ruff check .`, `git diff --check`.

## Historical replay admission (read-only first)

Read the sealed `s4_first_entry_plan`, `s4_entry_raw_precision` and
`stock_replay_inputs` artifacts and report itemized admission: the seven
precise-volume stocks, the two prior20 amount gaps, identity/status/
corporate-action/fee unknowns, and what each gap blocks (cash, quantity or
mark). No sealed program is rerun, nothing re-downloaded, no 21st-ranked
substitution. Only if every necessary fact passes and the run identity and
budget are explicit does a separate limited run card attempt the S4-A/C80
baseline; otherwise the deliverable is a precise stop point with a minimal
gap list (stock, date, field, why it matters, whether existing materials
resolve it). Engineering tests and any historical simulation are not live
returns and promise no 20% drawdown bound.
