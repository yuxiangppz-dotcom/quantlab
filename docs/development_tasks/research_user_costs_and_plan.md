# Research user costs and preregistration — Issue #48

Direct user commission, including correction that stamp duty is excluded from
the reported commission. Start clean pushed HEAD
`39913f0b7bc38434d8c2f77bbe364b11e934f669`, master CI 34502036737 passed.
Branch `codex/research-user-costs-and-plan`. No concurrent coding executor/reviewer.

Version a user-reported research fee profile, deliberately separate from frozen
backtest cost models and execution fee-cap authority. Commission is interpreted
as 0.86 per 10,000, stock minimum CNY 5, ETF minimum CNY 0.1. Dated stock sell-side
stamp duty is added separately; ordinary ETF secondary-market units are exempt.
Official sources and reviewed date limits are retained in profile/preregistration.
Historical commission is a constant comparison scenario, not historical broker
fact. One hypothetical order aggregates notionals then applies a minimum;
component rounding is half-up to fen, both declared assumptions.

Reject invalid/unsupported dates, asset types, notionals, rates and false
authority upgrades. Additional fees, slippage, spread, impact and dividend tax
remain unknown; the complete cost stays null. The UI displays only components
and explicitly says previous results have not been recomputed using this profile.

Preregister the drawdown target, minimum holding, 5/10/20-session comparison,
seven fixed candidate groups and at most six fixed-parameter LightGBM fits.
Old observed periods remain retrospective; diagnostics are not realizable
portfolio returns. Do not run outcome inspection, train, call providers or mutate
canonical data in this part. Next separate part: source-data quality/lineage audit
and bounded feature implementation before authorized acquisition/model training.

Verification: 1,274 tests passed, two optional default skips; optional LightGBM
and Qlib runtime checks both passed separately. Thirty-two new tests cover dated
stamp duty, stock/ETF minimums, single-order aggregation, zero filled notional,
half-up rounding, invalid inputs, prohibited authority upgrades, profile identity
and UI date changes. Ruff and whitespace checks passed. Actual Edge acceptance
changed the amount to CNY 10,000, displayed the component table and download,
with no page/browser exceptions; screenshot was visually reviewed. Self-review
made the fee text derive from the profile and reject mixed-profile calculations.
Eight files changed; no scope deviations. No providers, canonical writes, model
training, new outcome inspection, real imports/orders or promotion. Final feature
commit, push, PR and CI evidence is recorded in the linked PR. Freeze this component
calculator and the preregistered budget, then begin the bounded input audit.
