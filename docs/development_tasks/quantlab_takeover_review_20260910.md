# QuantLab takeover review — 2026-09-10

## Verdict and verified baseline

**Architecture: GOOD_WITH_GAPS.** Preserve the modular monolith and its frozen
financial semantics. The product is a research/evidence system with reference
portfolios and manually reported account facts, not a live execution system or
performance-grade account-feedback engine.

The independent review was communicated to the user before implementation.
This record distinguishes code inspection, synthetic reproduction and local file
inventory from unverified historical evidence. It is not a line-by-line proof of
every module, a rerun of historical research, or an investment performance claim.

- Initial local HEAD: `cb32311bf6df4dde3226703c422124bda50dc8bb` on clean `master`.
- Initial remote-tracking HEAD was also `cb32311`; live SSH `ls-remote` and fetch
  showed master at `41dd5ee3dd41db2b5b262d263194d01dc19bd09c`.
- Local master had no unique commits and was 22 commits behind. It was advanced
  by fast-forward only. No conflict, discarded work, reset or force push.
- Implementation branch: `codex/forward-shadow-temporal-admission`, created from
  the exact clean, pushed `41dd5ee` baseline.
- `chatgpt/manual-fill-effective-time-v2` exists at
  `e5ea7c1641d26034f9bb2240ff35daddb99ba59f`, two commits above master. Its entire
  net change is a 71-line `personal/fill_fact.py`; no parser, journal/replay or
  test integration. The earlier commit was `032cb43`. Preserve that branch.
- Existing desktop writer task was idle, reviewer automation was PAUSED, and
  process inspection found no active WSL coding executor/reviewer. Old loop
  state was observed but not claimed, resumed or edited; this direct commission
  is the authority.
- Baseline checks: 1,099 passed, 2 optional-runtime skips; Ruff passed.

SSH exposes branch/commit and PR-head refs but does not establish whether a PR
is currently open or what its CI result is. Initial unauthenticated GitHub REST
access returned 404, GitHub CLI was absent/unlogged, and browser connection failed.
The current online Issue, PR and Actions status must be populated from an
authenticated GitHub response, not inferred from commit titles or the handoff.

## Financial architecture findings

| Boundary | Finding |
| --- | --- |
| Data → Research | Canonical storage owns facts; research derives prices and labels. No direct provider dependency found in research/alpha. Data remains mutable by sync, so content hashes alone are insufficient for long-term input retention. |
| Historical identity | Dataset builder overlays code-change predecessor list/end dates and fails on unknown identities; historical `list_status` is not an eligibility filter. Static current board attributes are not board-history authority. |
| Signal timing | `run_backtest` rejects zero/nonpositive session lag; signal t is applied no earlier than t+1 close. This adjusted-value research book is explicitly distinct from executable raw-price share accounting. |
| Features and labels | Momentum reads historical returns only. Exact global-session lookup preserves missing endpoints as NaN. Sparse/default factor sampling does not replace explicit split-end containment checks. |
| Portfolio truth | Daily, Forward Shadow and product audit share the fixed-count Portfolio adapter, deterministic identifier tie-break, caps and cash residual. Fractional historical construction is a different declared contract. |
| Historical product alignment | Exact-count alignment is implemented. The product audit is still weekly and cannot establish daily-rebalance execution/performance. Full historical board scope is required; narrowed scope fails closed. |
| Backtest | Gross/net books, self-financing fees, atomic daily accounting, frozen holdings and lifecycle gates are substantial correctness assets. Explicit delisting recoveries remain assumptions; unsupported code changes do not become invented settlements. |
| Execution | Raw-price basis, typed PIT rule/fee authority, T+1 lots, availability fingerprints, reservation and submission checks exist. Unknown evidence blocks. Submission is local ledger preparation, not broker acknowledgement. No broker gateway exists. |
| Account | Money is represented in fen, fills retain explicit fees, and manual tracking delegates share/cash accounting to ExecutionLedger. Cash flows are separate economic facts. Mixing fill report time with cash-flow effective time remains incorrect for precise chronology. |
| Valuation | Checkpoints embed quantities and same-session raw closes, reconcile NAV and forbid performance/order authority. Corporate-action postings, historical state cutoffs and full timing eligibility are still missing prerequisites for returns. |
| Strategy governance | USER_APPROVED requires explicit user decision and remains separate from broker authority. Readiness consumes forward counts, so temporal admission must be enforced before aggregation. |
| Workflow ownership | Research orchestration imports Daily/Backtest, Daily imports Research/Personal and Personal imports Daily. This departs from the simplified one-way architecture diagram but is chiefly orchestration coupling, not a reason for a rewrite. |

Facade splits such as `daily/service.py` → `_service_impl.py` and
`personal/tracking.py` → `tracking_core.py` are not themselves duplicate truth.
The important shared implementations are Portfolio construction and Execution
accounting. Time admission now has one implementation used by publisher and readers.

## Five highest-priority risks

No P0 incident was demonstrated in the inspected scope. Ranked P1 risks:

1. **Forward evidence can be retrospective.** At baseline, a synthetic January 26
   prediction created March 1 generated two complete evaluations and entered
   forward statistics. Replacing its `created_at` with a year-2000 timestamp also
   passed integrity validation. Local same-identity duplicate predictions further
   caused the existing analytics to raise. This is the selected bounded fix.
   Evidence: `research/forward_shadow.py`, `forward_shadow_analytics.py` and the
   preserved regression cases in `tests/research/test_shadow_timing.py`.
2. **Mixed economic/report time in account replay.** `_parse_fill_events` sets
   `occurred_at=reported_at`, while external flows replay by effective time.
   Delayed fill reports can incorrectly change intermediate cash availability or
   cash-flow ordering. #32's type-only branch does not close this end-to-end gap.
   Evidence: `personal/tracking_core.py`, `_economic_time`, `_replay_tracking`.
3. **Factor split containment is not enforced at every entry point.**
   `factor_experiment._period` filters only signal dates; `build_experiment_frame`
   checks the overall dataset horizon, not each discovery/validation/test end.
   Current month-first sampling may avoid many default boundary crossings; custom
   period bounds or denser sampling can expose labels outside their split. Test
   actual split-end session dates before expanding ML or factor sampling.
4. **Historical identity/data lineage is incomplete.** Daily maps historical rows
   through current security board fields and drops unmapped boards. The historical
   product audit deliberately avoids this narrower board filter, so the same
   constructor does not prove equal historical input universes. Daily lineage
   hashes current-day inputs but does not bind all lookback partitions; some
   experiment/evaluation paths also omit full calendar/security-history versions.
5. **Account NAV is not full return evidence.** Checkpoint timing qualification
   currently describes cash flows, while legacy fill timing and company-action
   cash/share changes remain unresolved. Raw prices plus unadjusted share counts
   across a corporate action can distort economic NAV. Keep performance claims
   false and do not infer complete TWR eligibility from a cash-flow timing flag.

## Local data actually inspected

Read-only filesystem and Parquet metadata inspection established:

- `daily`, `adj_factor`, `daily_basic`, `index_daily`: each 4,053 partitions,
  2010-01-04 through 2026-09-09. Partition dates exactly cover locally recorded
  open sessions in that interval; no missing/extra partition dates.
- Boundary metadata: daily rows 1,632 → 5,550; adjustment rows 1,698 → 5,560;
  daily_basic rows 1,632 → 5,550; index rows 3 → 3. Different adjustment counts
  are not silently interpreted as an error or proof of complete tradable coverage.
- Calendar has 12,194 rows, SSE and SZSE, 2010-01-01 through 2026-09-10.
  Future calendar coverage after September 10 is therefore not established.
- Security master: 5,900 rows. Historical board versions were not established.
- Versioned ST and suspension context: 1,215 partitions each, spanning
  2020-01-02 through 2026-09-09 with historical/current gaps; not continuous
  2010–2026 coverage.
- Daily price limits: one 2026-09-09 partition with 5,217 rows. Financial
  observation: one 2026-09-10 snapshot, 6,078 rows. Dividend observation: one
  snapshot, 97 rows. These are not new historical PIT financial evidence.
- Six local Forward Shadow prediction manifests: three per model/version, all
  signal date 2026-09-09 and reported generation on September 10 after 10:00.
  No evaluation JSON existed. The six original prediction artifacts were not
  modified, replaced, deleted or regenerated in this task.

This inventory does not prove row-level completeness across all years, official
data publication times, exchange tradability, or the accuracy of old research
results. No new provider request or historical strategy run was made.

## Freeze, bottleneck and chosen next work

Freeze the backtest dual-book cost/lifecycle behavior, Execution v0.2.2 authority
and reservation semantics, fixed-count Portfolio core, and agent-loop development.
Execution has enough scaffolding for the present product; more transport/event
infrastructure without broker/rule/fee evidence is lower value.

The bottleneck is trustworthy new evidence, followed by correct account chronology.
Infrastructure has advanced faster than prospective evidence; collecting valid
observations takes calendar time and cannot be replaced by more factor searches.
Finish the Forward Shadow admission closure before expanding Alpha158, ML,
turnover/rank-buffer variants or leaderboard claims. Thereafter tackle one bounded
account fill-time integration or split-containment task, with synthetic tests.

This task changes no frozen accounting, backtest, label or portfolio semantics.
The new observation window is a conservative research registration policy,
documented in `docs/forward_shadow_contract.md`, not a verified market-access rule.

## Validation and completion record

The LightGBM wheel and Qlib were present, but WSL had no installed `libgomp1`.
The Ubuntu package was downloaded and SHA-256 checked against package metadata,
then extracted only into a temporary directory. A per-command `LD_LIBRARY_PATH`
enabled real LightGBM fit/predict and Qlib StaticDataLoader smoke: **2 passed**.
`uv sync --frozen --extra research --extra qlib` succeeded. No system package
installation or elevated privileges were used, and the default WSL environment
still needs its normal OpenMP runtime setup for routine LightGBM use.

Final default suite: **1,131 passed, 2 skipped** (the two optional tests separately
passed as described above). Ruff and `git diff --check` passed. Self-review checked
the bound timestamps, UTC/Shanghai boundary, first-publication retry semantics,
legacy evaluation exclusion, source-byte capture, path identity, positive label
horizon, unchanged portfolio/label arithmetic and false trading/performance claims.
The previous positive fixture used a retrospectively generated signal; it now
uses a valid same-day observation, with its old late behavior retained as an
explicit rejection/exclusion regression. No valid test was removed or relaxed.

Commit/push and authenticated GitHub results are recorded in the task handoff/PR.
If API authentication remains unavailable, report
that blocker explicitly and leave master unmerged; never infer green CI from
local tests. Prohibited actions not taken: provider calls, canonical writes,
real account journal changes, brokerage actions, synthetic facts presented as real,
strategy promotion, performance claims, secret output, force pushes or loop edits.
