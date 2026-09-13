# Risk-ledger loop fix card v2 (PR #124 second-review remainder)

Direct commission continuation on `codex/risk-ledger-integration-v1` in the
isolated clone. Reviewer mainline `2a5eae7`; reviewed base
`932acc980651a6e897c5fe905c275bdf50a95c9c` (3092 passed, CI green). Commits
append; no amend, no force push, no self-merge; Codex directory and review
outputs read-only; automation paused.

All four residual defects were re-verified on that exact head before this
card: the loop hands its canonical attempted set straight into
`advance_research_day` (line 437) so a post-execution stop leaves stale ids;
the checkpoint binds only the `run_id` string (line 370); the public
`advance_research_day` never validates the whole calendar; and the admission
report's reconciliation fingerprint is one character short of the sealed
`a9e30b671ace89d59b272d5558295a1b1ee68f75e93c79309bd310d8951c3e10`.

## Fixes (bounded, no rewrites of merged semantics)

- **R2 remainder (P1)** — the loop runs each session's execution against a
  local copy of the attempted-id set and folds it into the canonical set only
  when the session commits (trades + risk state + intents together). Any
  post-execution stop returns a checkpoint field-for-field equal to the last
  complete one, so resuming after completing the missing input reproduces the
  clean-input run exactly. The scheduler's own API semantics (it commits its
  successful attempts into the set it was given) stay unchanged; the loop
  owns transaction isolation.
- **R1 remainder (P2)** — `RiskLedgerCheckpoint` now stores the full immutable
  `RiskLedgerConfig`; construction and every resume verify it, covering
  rule_id, NAV series/source, V/M sources and the actual generation-rules
  content (a changed grid under the same `scenario_id` rejects). Same-config
  resumes with a different `requested_end` remain allowed.
- **R3 remainder (P2)** — `advance_research_day` reuses the module's existing
  `_calendar` validation before any state change; reversed or duplicate
  calendars reject with inputs untouched. Normal calendars and the old
  whole-schedule entry keep their behavior.
- **R5 (P3)** — the admission report's fingerprints are corrected by reading
  the sealed JSON files programmatically, listing the reconciliation,
  plan-intent and cohort-profiles fingerprints verbatim; no re-downloads, no
  sealed reruns; raw-data presence still does not certify financial
  eligibility.

## Regression requirements

New tests: post-execution stop after real simulated attempts with nonzero
fees (mark-missing and risk-unknown variants), returned checkpoint equality
field by field, then completion of the missing input and a day-by-day match
against the clean-input run, including blocked and partial-fill worlds;
config-identity rejects for a changed rule and a changed grid under the same
scenario id, plus same-config per-split-day resume; reversed/duplicate
calendar rejection with untouched inputs. Full `uv run pytest`,
`uv run ruff check .`, `git diff --check`. No factors, models, downloads,
historical economic experiments, corporate-action postings, orders,
promotions or automation changes; G1-G5 stay open, economic paths stay 0.
