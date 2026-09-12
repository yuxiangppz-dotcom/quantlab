# Stock research quantity/cash primitives — Issue88

Direct single-agent continuation. Start clean pushed
`1ef9bb8d73b13b782c3232bab53cf7d549f74793`, exact master CI34693983365 passed.
No concurrent writer/heavy worker; both research locks checked available.
The original60 economic paths and98 annual fit slots remain consumed and sealed.
This card authorizes implementation and synthetic tests only, with zero actual
research scenarios, model fits, provider calls or canonical/account writes.

Implement the smallest pure stock quantity/cash arithmetic needed for a later
versioned replay. Reuse existing historical stock-rule contracts and the dated,
user-reported research fee profile. Raw unadjusted Decimal currency marks,
identity, rule, source fingerprints, availability cutoff and calendar must bind
each result. Do not change frozen backtest/execution/account semantics.

One hypothetical quantity transfer corresponds to one declared aggregate for
commission calculation. Respect minimum/step/maximum quantities without silent
order splitting. Buy sizing includes declared fee components within its integer-
fen budget. Sales add the separately dated stamp component. Hypothetical lots
provide only the reviewed T+1 subset and full odd-lot exit, with explicitly
declared FIFO reductions that do not establish dividend-tax authority.

The returned cash field is `declared_components_cash_fen`; complete cash, complete
cost, fillability and corporate cash remain unknown. No fee-cap quote, broker
fill, account journal, return metric, portfolio runner, UI enablement or execution
authority is created. ETFs are outside this first stock-only primitive.

Validate independent integer cash/quantity arithmetic, minimum-fee boundaries,
sale stamp asymmetry, fen rounding, main/STAR quantities, no splitting, T+1 with
holidays, full odd-lot exit, insufficient cash, source/date/scope mismatches,
adjusted-price rejection, immutable inputs and stable source fingerprints.
Run full pytest/Ruff/diff, self-review, commit/push, exact PR CI, merge and master CI.

Self-review must retain unknown lifecycle, price-limit, suspension, liquidity,
corporate-action and historical-identity evidence. Before any actual quantity
replay, a separate bounded card must bind these inputs, all saved target policies,
allocation order, capital and stop rules. The000851.SZ/600355.SH gaps are not
resolved by arithmetic and must not be silently skipped or retroactively sold.

Validation:25 focused synthetic cases and the full1,966-test suite passed
(10 optional-runtime skips in the default suite); Ruff and diff checks passed.
Self-review removed an unordered calendar cache from evidence fingerprints,
rejected mutable nested rule fields, and made fen/fee arithmetic independent of
the caller's Decimal precision. A future-source test fixture initially failed
at the inherited rule constructor's provenance/duplicate-URL checks; the
synthetic fixture was corrected to exercise the intended context guard. The
original failed full-test log remains under ignored runtime output; production
research runs remain zero. No frozen source, prior result or fee profile changed.
