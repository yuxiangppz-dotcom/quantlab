# Fixed-cohort corporate-event readiness experiment

Direct user approval of research program v3 is the authority. Expected base HEAD:
9a41d5e1b917e319a7b17c5b2186258f68e94d61, clean and pushed; exact master CI
34715202161 passed. No old executor/reviewer or heavy worker is active. Preserve
the paused shared mailbox. Commit/push this card before implementation and the
tested implementation before the single actual run.

Question: which already acquired corporate-action observations for the fixed
256-stock S4 cohort can be structurally reconciled, and which concrete gaps
prevent a historical quantity/cash replay? This is prerequisite evidence, not a
return test. Reuse dividend_events.observations; do not rerun its older full-market
window experiment or change its historical authority.

Scope: exactly 225 successful responses from the old dividend acquisition and
31 from dividend_gap_intake, 15190 raw row occurrences in total. Bind the frozen
selection, both acquisition reports/proofs, all 256 body/intent/result triples
and this card by byte hashes before actual computation. Retain all dates/stages;
flag observations with any record/ex/pay/list date in 2020-01-01..2024-12-31.
No valid date means unknown relevance; dates observed only outside that range
do not prove no event inside it. Retain raw duplicates and proposal stages.

Reconcile intent/receipt/body identity, instrument, row counts and timestamps.
Old receipts expose observed_at. New journal receipts expose only at, recorded
after receipt processing: use that later timestamp as a conservative local
availability bound, explicitly label it receipt_recorded_at, and never claim an
exact wire-observation time or rewrite the sealed source receipt.

Preserve parser candidate-key conflicts. Additionally group implementation
observations with known record/ex dates by code/end_date/record_date/ex_date;
compare cash/share terms and payment/list dates across versions. This is a
possible event collision, not proof those rows describe one economic event.
Do not select the latest announcement, sum proposals, or automatically resolve
conflicting terms. Exact duplicates stay in the occurrence table.

Structural readiness requires implementation status, known ordered record/ex
dates, known nonnegative cash-before-tax and stock distributions, consistent
share components, payment/list dates when positive, and no malformed fields,
unknown status or candidate/possible-event conflict. Base-share metadata remains
separately reported; it is not required for checking per-share term structure.
Ready means fields internally consistent only. Historical signal knowledge,
complete event coverage, verified cash/tax entitlement and execution authority
remain false for every row. Do not manufacture payments or backtest NAV.

Outputs: one lossless occurrence table, one possible-event collision table,
one per-code/year readiness summary and sealed report; separate raw-byte checker
must verify all rows, dates, stages, counts and collision memberships without
calling the production observation/readiness functions. Retain stopped partials;
one actual attempt, no overwrite/restart. Report source/final HEAD and counters.

Bounds: 768 raw-source files, at most 12 control files, 64 MiB source bodies,
256 MiB ignored output, 600 seconds actual computation, 2 GiB peak RSS,
8 GiB physical D reserve, one existing OS heavy-job lock. No provider/public-web
requests, canonical writes, new formulas/candidates, model fits, economic paths,
account writes, orders, promotions, paid permissions or C-drive cleanup.

Tests cover conservative timestamp provenance, cross-code/tampered receipts,
duplicates/stages, conflicts across announcement versions, unknown/invalid and
out-of-window dates, zero/positive distributions and immutable authority flags.
Run focused tests, full uv run pytest, uv run ruff check ., git diff --check;
self-review, commit/push, one actual run, independent check, Chinese findings,
PR/CI/merge and exact master CI. If gaps remain, freeze their exact scope and
continue the separately bounded historical-rule/controller route; do not repeat
this audit or count it as demonstrated strategy improvement.
