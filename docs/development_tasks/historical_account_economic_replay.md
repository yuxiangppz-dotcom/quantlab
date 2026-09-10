# Historical account economic replay — Issue #44

Direct user commission. Start: clean pushed
`f0774f9b006e4e8aea15ae8e6df003e54fd75a4b`, master CI `34497116655` passed.
Branch `codex/historical-account-economic-replay`; no other WSL coding process
was running. Read-only historical economic cutoff, explicit archived opening
basis, existing journal decoding and ExecutionLedger cash/T+1 replay only.

Cutoffs are timezone aware and inclusive. T+1 sellability is evaluated at the
cutoff, not the last event date. Late reports are disclosed as economic
restatements, never historical knowledge. No opening knowledge time is invented.
Legacy fills potentially before the cutoff and all legacy cash-flow timing
block exact replay; unknown state remains null. Missing days inside the calendar
interval also block. Bind journal/opening/calendar evidence and mutate nothing.

Expose this through the account cash/valuation page with query-state binding and
JSON download. Test cutoff ordering, UTC equivalence, delayed reports, T+1 without
new events, old-basis isolation, legacy timing, tampering and no writes. Full
pytest/Ruff/diff/optional runtimes, self-review, push/PR/green CI/merge/master checks.
No provider calls, real account import, return calculation, trading or promotion.

Next: a separate performance-input eligibility gate before return calculation,
then bounded factor/model research. User constraints recorded during this task:
research maximum drawdown target 20%, holding at least one trading day, compare
fixed 5/10/20-session candidates for the upper holding horizon; user-reported
stock/ETF all-in commission quoted as 0.86 (working interpretation 0.86 per 10,000),
stock minimum CNY 5 and ETF minimum CNY 0.1, with stock-sale stamp duty explicitly
excluded from commission. These inputs do not amend frozen execution authorities
or establish a real-world maximum-loss guarantee. Fees require dated official
tax rules and clearly scoped research cost assumptions in the next research task.

Verification: 1,230 tests passed, two optional tests skipped in the default run;
both optional LightGBM/Qlib runtime tests passed separately. Ruff and whitespace
checks passed. Thirteen new tests cover the economic cutoff and the actual
Streamlit callback using a temporary real ledger. Self-review found and fixed
missing opening-timezone and journal-event account binding validation before
commit. Nine files changed. No data-provider calls, canonical writes, real account
imports, return calculations, orders or strategy promotions occurred in this task.
No scope deviations. Historical knowledge and corporate-action completeness
remain unverified; the result explicitly remains ineligible for performance.
Freeze this read-only API after green CI; continue with the separate eligibility
gate. Final commit, push and merge evidence is recorded in the linked PR.
