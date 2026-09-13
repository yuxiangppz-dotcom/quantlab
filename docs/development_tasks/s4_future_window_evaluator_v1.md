# Pure evaluator for the already registered weekend observation

Direct approved v3 commission. Exact clean pushed start/expected_head:
177701699b42eb3aeb8f9f3949c674fb4e7c1bd8. Heavy lock free, no old coordination
writer found, shared loop paused. Push this card before implementation.

Implement and test a pure, in-memory evaluation component for the one existing
2026-09-13 S4-A weekend observation. No actual evaluator run, future-price read,
provider/public-source request, new observation, score recomputation, fitted model,
label artifact, canonical write, economic path or old writer rerun is authorized
by this card. Tests use synthetic observations and explicit synthetic future times.

Preserve the original fixed256 cohort, Sept11 price-as-of, actual weekend creation
semantics, six exact future sessions Sept14,15,16,17,18,21 and minimum30 joint pairs.
Validate aware timestamps, source availability, actual creation before the future
window and every future session's source observation at/after its close and no
later than evaluation. Reject premature evaluation before inspecting future frames.
The real future evidence still needs a separate finite acquisition/qualification
card after the six sessions mature; in-memory dates do not certify source files.

Label=P(Sept21)/P(Sept14)-1 on original adjusted research prices. Every one of the
six bars must satisfy the unchanged positive finite OHLCV/amount/adjustment rule.
Retain missing calendar positions and all256 stocks; no compressed windows, new
survivor selection, signal changes, alternative horizon, rank selection or weights.
Use the SAME joint finite sample of label, S4-A and negative20-day-change reference
for both average-tie Spearman correlations and their difference. Fewer than30
pairs yields unknown metrics; constant ranks yield unknown corresponding metrics
and difference, not zero or a different fallback sample. One date cannot produce
stability, significance, portfolio return, net profitability or promotion evidence.

Return inspectable per-stock labels/masks and summary metrics only to the caller;
no I/O/CLI/writer/automatic maturity promotion. Source-file certification, net-return
qualification and execution authority remain false. Old Forward Shadow API/policy,
the saved observation/proof/UI state and frozen score files remain unchanged.

At most25 minutes implementation, one local test worker. Meaningful synthetic tests
cover independent hand-computable ranks/ties, paired-sample mismatch,29/30 boundary,
missing intermediate bars, timestamp/calendar/out-of-grid errors, no early frame
access, input nonmutation and false authority. Full pytest/Ruff/diff, self-review,
commit/push, PR/CI/merge/exact-master CI, then concise evidence/next-step report.
