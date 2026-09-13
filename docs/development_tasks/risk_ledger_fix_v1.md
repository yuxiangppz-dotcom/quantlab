# Risk-ledger loop fix card v1 (PR #124 review R1-R5)

Direct commission continuation on `codex/risk-ledger-integration-v1` in the
isolated clone. Reviewer mainline `2a5eae7`, reviewed commit
`650efbb267afbb9596fa5912ce896044808460d6` (3027+50 tests, CI green — the
defects below are behavioral, found by additional synthetic probes). Commits
append; nothing handed to review is rewritten; the Codex directory and its
review outputs stay read-only; automation stays paused.

Fix base re-verified before this card: pending reset at the loop head,
risk_unknown continuation, `int(weight * equity)`, unconditional capacity
pruning in `advance_research_day`, and the admission-report claims all match
the probe file `pr124_review_20260913/review_pr124_probe_v2.json`.

## R1 — atomic, resumable checkpoints

New `RiskLedgerCheckpoint` binds: the completed book, the pending intents
(all signed on the book's session, so a 2021-12-31 signal executes on the
next common session 2022-01-04), the drawdown state, the attempted order-id
identity, the original starting capital and the run id. `run_risk_ledger_loop`
starts from a checkpoint and exposes the last committed one on the result;
resume never re-seeds an empty pending list and never reports residual cash
as the original capital. `RiskLedgerConfig` gains the run id and resumes
with a mismatched id reject. Regression tests resume at every split day —
before a pending buy, before a pending sell, after a partial fill and after
a blocked order — comparing daily orders, fees, cash, shares, risk state and
the original capital against the uninterrupted run.

## R2 — necessary unknowns stop the path

The original commission's stop semantics are restored and the card text that
invented a "risk unknown continues" policy is void. A session commits only
atomically (trades + risk state + next intents together); any necessary
unknown during the session — missing evidence for that day, missing base
target, risk decision unknown (V/M warmup or gaps, D NAV unavailable),
a target instrument without a raw mark, nonpositive equity — stops the path
at that session, returns the previous checkpoint, the failure date and the
specific reason, and discards the session's intermediate book mutation.
V/M warmup history must pre-date the evaluation window (caller-supplied); a
warmup gap is a stop, never a comparable all-cash segment. Missing evidence
for a later session is discovered per day and keeps the completed prefix
instead of raising up front; corporate-processing `None` is representable and
stops through the existing preflight reason. The attempted-id set is not
polluted by a discarded session.

## R3 — single-day advance guards calendar order and capacity

`advance_research_day` requires `book.asof_date == calendar[index-1]`,
rejecting same-day re-entry, reverse and skipped sessions, before any
capacity pruning. It works on a local copy of the attempted set and merges
into the caller's set only on success, so an exception mid-batch leaves the
caller untouched. The whole-schedule entry keeps its frozen behavior.

## R4 — exact weight-to-budget arithmetic

The target budget is `Decimal(str(weight)) * Decimal(equity_fen)` with exact
decimal floor division by the raw mark price, then the declared quantity
grid — no float multiplication, no epsilon, no price rounding. Exactly-one-
lot, boundary and post-scaling weight cases are regression-tested
(0.57 × 20,000,000 fen at 11,400 fen must be 1000 shares, not 900).

## R5 — admission report corrected against current sealed facts

Rewritten from the sealed files (not memory): distinguish "field present",
"value admitted" and "proven financial fact". Corrections: the seven
execution-day volumes are a necessary admission condition
(`_missing_reason` requires `session_volume_shares`); all 20 fixed proposals
have raw dividend occurrences per `cohort_dividend_readiness/profiles.json`
(raw presence does not certify corporate processing — stays unadmitted);
`plan.json` shows `calendar_verified` True and both limit numbers for all 20
(tradability, identity and corporate eligibility remain separate checks).
The six empty raw dates stay unknown with zero downloads this round; locally
checkable facts are listed without equating the exhausted old request budget
with absent user authorization. The no-historical-return conclusion stands.

## Acceptance

All original assertions are kept or strengthened; key financial paths get a
manual scenario with explicit nonzero fees (commission, minimum and sell
stamp). Full `uv run pytest`, `uv run ruff check .`, `git diff --check`.
No factors, models, downloads, historical experiments, orders, promotions
or automation changes. The single declared `generation_rules` scenario is
explicitly not a claim of real per-security/day rule coverage.
