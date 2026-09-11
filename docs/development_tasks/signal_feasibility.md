# Issue #60: label-independent scores and portfolio input audit

Authority: direct user commission, including immediate start on 2026-09-11.
Start: clean pushed `6b0f034f86b7ba7cbb1a041f49e9226b6d49a6b2`; master CI
34519203695 passed. No old executor/reviewer process was found. Single agent.

The immutable round-two dataset contains all research-scope price observations,
including rows whose future labels are absent. Export six fixed candidates:
three saved baseline models and the existing combination at 5/10/20 sessions,
2023-01-01 through 2026-09-10. Read only the key and feature allowlist for
prediction; require finite common features consistently for all horizons.
Retain incomplete-feature identities and the source stage's lifecycle/scope
exclusion references. Reuse original preprocessing and saved models, zero fits.
Scores are retrospective: models were trained in September 2026, even though
their contained training observations end in 2022. Do not call these historical
registrations, an untouched holdout, or a complete tradable universe.

The fixed next-evaluation contract is `config/research_signal_feasibility_v1.json`.
Use the existing fixed-count portfolio constructor to connect each daily score
cross-section to at most 20 equal targets with 80% gross exposure. These are
independent hypothetical intentions, not a continuous portfolio. Do not replace
names based on future prices or execution-context gaps. Hypothetical capital is
CNY 50,000 / 200,000 / 1,000,000, never an asserted user account balance.
The next evaluation will use nonoverlapping sleeves with intended 5/10/20-session
holds after next-session entry, minimum one session, and retain unexitable lots.
This exit date differs from the model's close-to-close label endpoint; it is a
predeclared implementation scenario, not a change to the frozen label.

Audit raw next-session and intended-exit price presence, ST and S/R raw context,
limits, code/lifecycle, effective board/lot rules, corporate actions, cash and
fees. Missing S/R rows do not certify open trading. Daily prices and limits do
not certify fills. Use the existing rule resolver only for declared board/date
coverage inventory, never promote today's board label to historical identity.
Use existing research fee-component calculations, with full cost remaining
unknown. No net returns or 20% drawdown acceptance are produced by this audit.

Implementation and tests are committed and pushed before the real run so that
it binds clean code. A second part adds the actual findings and Chinese review.
Verify label mutations/tail missingness cannot change scores or targets; no fit
call is possible; model features, training bounds, code changes and all immutable
hashes are checked; unknown evidence cannot acquire trading/performance authority.
Complete full pytest, Ruff, diff checks and both optional runtime tests, self
review, PR, green CI, merge and master CI. Preserve failures and run evidence.

Only local research products: no providers, canonical writes, orders, account or
fill changes, registrations, C-drive cleanup, old agent-loop changes or promotion.

The first real preflight on commit `3a8582f` stopped before creating the export:
round-two presentation files had legitimately changed since staging. Verification
now checks source code against the historical Git tree and explicitly versioned
checkout endings, while preserving and checking the exact original bytes of five
older test files with mixed Windows endings. Those files must ALSO normalize to
the historical Git blob. Canonical data is never normalized or substituted.
The original 292 source files and 5,232 data/other input files were verified.
No export or model fit was produced by the failed preflight; its log is retained
under ignored `data/runtime/research/signal_feasibility_20260911.log`.
